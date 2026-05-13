"""
Deep OSINT v2 — orquestrador aprofundado.

Diferencas do deep_osint v1:
- Inclui novas fontes: TikTok, Instagram (publico), Twitter via Nitter,
  LinkedIn company page, CNPJ socios, reverse phone, news regional,
  Reclame Aqui
- Tudo roda em paralelo (asyncio.gather)
- Retorna nao so sinais de interesse mas tambem dados estruturados:
  perfis sociais novos, CNPJ socios, telefones alternativos
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from enrichers.interest_detector import InterestSignal, detect
from enrichers.sources import (
    cnpj_socios,
    instagram,
    linkedin_company,
    news_regional,
    nitter_twitter,
    reclame_aqui,
    reverse_phone,
    tiktok,
)

log = logging.getLogger("enricher.deep_osint_v2")


@dataclass
class DeepOsintV2Result:
    sinais: list[InterestSignal] = field(default_factory=list)
    boost_score: int = 0

    # Perfis novos descobertos
    tiktok_url: str = ""
    instagram_url: str = ""
    twitter_username: str = ""
    linkedin_company_url: str = ""

    # Dados estruturados
    cnpj_data: dict = field(default_factory=dict)
    reverse_phone_data: dict = field(default_factory=dict)
    news_mentions: dict = field(default_factory=dict)
    reclame_aqui: dict = field(default_factory=dict)

    # Textos por fonte (pra auditoria)
    textos: dict = field(default_factory=dict)

    fontes_consultadas: list[str] = field(default_factory=list)


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


async def _empty_dict() -> dict:
    return {}


async def _empty_str() -> str:
    return ""


async def aprofundar_v2(
    *,
    nome: str,
    cidade: str = "",
    cidade_tag: str = "",
    cnpj: str = "",
    telefone: str = "",
    site_url: str = "",
) -> DeepOsintV2Result:
    """
    Roda TODAS as fontes em paralelo.
    Retorna sinais + dados estruturados.
    """
    res = DeepOsintV2Result()
    if not nome:
        return res

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=15.0) as client:
        # Fase 1: descobrir URLs de perfis sociais (paralelo)
        ig_url_task = instagram.buscar_perfil(client, nome, cidade)
        tt_url_task = tiktok.buscar_perfil(client, nome, cidade)
        tw_user_task = nitter_twitter.buscar_perfil_twitter(client, nome)
        li_url_task = linkedin_company.buscar_pagina_empresa(client, nome, cidade)

        ig_url, tt_url, tw_user, li_url = await asyncio.gather(
            ig_url_task, tt_url_task, tw_user_task, li_url_task,
        )

        res.instagram_url = ig_url or ""
        res.tiktok_url = tt_url or ""
        res.twitter_username = tw_user or ""
        res.linkedin_company_url = li_url or ""

        if ig_url:
            res.fontes_consultadas.append("instagram_busca")
        if tt_url:
            res.fontes_consultadas.append("tiktok_busca")
        if tw_user:
            res.fontes_consultadas.append("twitter_busca")
        if li_url:
            res.fontes_consultadas.append("linkedin_busca")

        # Fase 2: coletar dados dos perfis + outras fontes (TUDO em paralelo)
        tasks = {
            "instagram": instagram.coletar_perfil(client, ig_url) if ig_url else _empty_dict(),
            "tiktok": tiktok.coletar_perfil(client, tt_url) if tt_url else _empty_dict(),
            "twitter": nitter_twitter.coletar_tweets(client, tw_user) if tw_user else _empty_dict(),
            "linkedin": linkedin_company.coletar_pagina_empresa(client, li_url) if li_url else _empty_dict(),
            "cnpj_buscar": cnpj_socios.buscar_cnpj_por_nome(client, nome) if not cnpj else _empty_str(),
            "reverse_phone": reverse_phone.buscar_via_ddg(client, telefone) if telefone else _empty_dict(),
            "news": news_regional.buscar_mencoes(client, nome, cidade_tag),
            "reclame": reclame_aqui.buscar_reclamacoes(client, nome),
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=False)
        result_map = dict(zip(tasks.keys(), results))

        # Processa cada resultado
        if result_map["instagram"]:
            d = result_map["instagram"]
            res.textos["instagram"] = (d.get("bio") or "") + " " + " ".join(d.get("hashtags", []))
            res.fontes_consultadas.append("instagram_coleta")

        if result_map["tiktok"]:
            d = result_map["tiktok"]
            res.textos["tiktok"] = (d.get("bio") or "") + " " + " ".join(d.get("hashtags", []))
            res.fontes_consultadas.append("tiktok_coleta")

        if result_map["twitter"]:
            d = result_map["twitter"]
            res.textos["twitter"] = d.get("tweets_text", "")
            res.fontes_consultadas.append("twitter_coleta")

        if result_map["linkedin"]:
            d = result_map["linkedin"]
            res.textos["linkedin"] = (d.get("descricao") or "") + " " + (d.get("industry") or "")
            res.fontes_consultadas.append("linkedin_coleta")

        # CNPJ: se nao tinha, buscamos. Depois consultamos BrasilAPI.
        cnpj_a_consultar = cnpj or result_map.get("cnpj_buscar") or ""
        if cnpj_a_consultar:
            cnpj_data = await cnpj_socios.consultar_brasilapi(client, cnpj_a_consultar)
            res.cnpj_data = cnpj_data
            res.fontes_consultadas.append("brasilapi_cnpj")

        if result_map["reverse_phone"]:
            res.reverse_phone_data = result_map["reverse_phone"]
            res.textos["reverse_phone"] = " ".join(res.reverse_phone_data.get("snippets", []))
            res.fontes_consultadas.append("reverse_phone")

        if result_map["news"]:
            res.news_mentions = result_map["news"]
            res.textos["news"] = res.news_mentions.get("texto_concatenado", "")
            res.fontes_consultadas.append("news_regional")

        if result_map["reclame"]:
            res.reclame_aqui = result_map["reclame"]
            res.textos["reclame_aqui"] = res.reclame_aqui.get("texto_concatenado", "")
            res.fontes_consultadas.append("reclame_aqui")

    # Roda detector de keywords em TODO o texto coletado
    texto_total = " ".join(res.textos.values())
    sinais, boost = detect(texto_total)
    res.sinais = sinais
    res.boost_score = boost

    log.info(
        f"  deep_osint_v2: {len(res.fontes_consultadas)} fontes, "
        f"{len(sinais)} sinais, boost +{boost}, "
        f"ig={bool(res.instagram_url)} tt={bool(res.tiktok_url)} "
        f"tw={bool(res.twitter_username)} li={bool(res.linkedin_company_url)} "
        f"cnpj={bool(res.cnpj_data)} reclame={res.reclame_aqui.get('n_resultados', 0)}"
    )
    return res
