"""
Deep OSINT v2.2 — orquestrador aprofundado (REESCRITO).

LICOES APRENDIDAS:
- v2.0: 7 buscas DDG/lead → DDG bloqueou → 0 sinais.
- v2.1: rodava detect() na RESPOSTA do DDG → DDG ecoa a query de volta
  ("no results found for: <query>") e a query continha as keywords →
  AUTO-CONTAMINACAO, false positives em massa.

ESTRATEGIA v2.2 — PRINCIPIO FUNDAMENTAL:
  detect() SO roda em CONTEUDO REAL de PAGINAS VISITADAS.
  Cada sinal aponta pra URL REAL e VERIFICAVEL.
  DDG eh usado APENAS pra DESCOBRIR URLs — nunca pra detectar.

FONTES (todas com source_url verificavel):
  1. Site oficial (URL conhecida do enrich) — visita home + /sobre + /vagas
  2. Instagram (URL conhecida) — visita bio
  3. Facebook (URL conhecida) — visita pagina
  4. LinkedIn company (descoberto via dork → VISITA a pagina)
  5. Paginas de vaga (descobertas via dork → VISITA cada uma)
  6. CNPJ via BrasilAPI (dado estruturado, source = brasilapi)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from enrichers.interest_detector import InterestSignal, detect
from enrichers.sources import cnpj_socios

log = logging.getLogger("enricher.deep_osint_v2")


@dataclass
class DeepOsintV2Result:
    sinais: list[InterestSignal] = field(default_factory=list)
    boost_score: int = 0
    instagram_url: str = ""
    facebook_url: str = ""
    site_url: str = ""
    linkedin_url: str = ""
    vaga_urls: list = field(default_factory=list)
    cnpj_data: dict = field(default_factory=dict)
    # textos_por_url: {url: texto} — cada texto vira detect com source_url=url
    textos_por_url: dict = field(default_factory=dict)
    fontes_consultadas: list[str] = field(default_factory=list)


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ============================================================================
# Visita de paginas — retorna (url, texto). detect() roda nisso.
# ============================================================================

async def visitar(client: httpx.AsyncClient, url: str, max_chars: int = 25000) -> str:
    """Visita URL, retorna texto limpo. Timeout curto."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = await client.get(url, timeout=8.0, follow_redirects=True)
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        # Remove tags de script/style ANTES de extrair texto (evita pegar JS!)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        partes = []
        for sel in ["meta[name='description']", "meta[property='og:description']", "meta[property='og:title']"]:
            m = tree.css_first(sel)
            if m:
                c = m.attributes.get("content", "")
                if c:
                    partes.append(c)
        body = tree.body.text() if tree.body else (tree.text() or "")
        partes.append(body[:max_chars])
        return " ".join(partes)
    except Exception:
        return ""


async def coletar_site(client: httpx.AsyncClient, site_url: str) -> dict:
    """
    Visita home + /sobre + /servicos + /vagas + /trabalhe-conosco.
    Retorna {url_completa: texto} — cada pagina com sua URL exata.
    """
    if not site_url:
        return {}
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    paths = ["", "/sobre", "/servicos", "/vagas", "/trabalhe-conosco", "/carreiras", "/blog"]
    urls = [base + p for p in paths]
    textos = await asyncio.gather(*[visitar(client, u) for u in urls])
    return {u: t for u, t in zip(urls, textos) if t and len(t) > 50}


# ============================================================================
# DDG — APENAS pra DESCOBRIR URLs. Nunca rodamos detect() no resultado DDG.
# ============================================================================

async def ddg_buscar_urls(client: httpx.AsyncClient, query: str, max_urls: int = 5,
                          filtro_dominio: str = "") -> list[str]:
    """
    Roda dork DDG e retorna SO AS URLs dos resultados (nao o texto/snippet).
    filtro_dominio: se setado, so retorna URLs que contem essa string.
    """
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(query)}&kl=br-pt",
            timeout=10.0,
        )
        if r.status_code != 200:
            return []
        tree = HTMLParser(r.text)
        urls = []
        for link in tree.css("a.result__a"):
            href = link.attributes.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
            if href.startswith("http"):
                if not filtro_dominio or filtro_dominio in href:
                    urls.append(href)
            if len(urls) >= max_urls:
                break
        return urls
    except Exception as e:
        log.debug(f"ddg_buscar_urls falhou: {e}")
        return []


# ============================================================================
# Orquestrador v2.2
# ============================================================================

async def aprofundar_v2(
    *,
    nome: str,
    cidade: str = "",
    cidade_tag: str = "",
    cnpj: str = "",
    telefone: str = "",
    site_url: str = "",
    instagram_url: str = "",
    facebook_url: str = "",
    endereco_gmaps: str = "",
) -> DeepOsintV2Result:
    """
    v2.2 — detect() SO em conteudo de paginas visitadas. Cada sinal tem
    source_url verificavel. DDG so descobre URLs.
    """
    res = DeepOsintV2Result()
    if not nome:
        return res

    res.site_url = site_url
    res.instagram_url = instagram_url
    res.facebook_url = facebook_url

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=10.0) as client:
        # ---- FASE 1: DESCOBRIR URLs via DDG (sem detect aqui!) ----
        # LinkedIn da empresa
        linkedin_dork = f'site:linkedin.com/company "{nome}"'
        # Vagas: paginas que falam de vaga/contratacao DESSA empresa
        vaga_dork = (
            f'"{nome}" (vaga OR vagas OR "trabalhe conosco" OR contratando OR '
            f'"estamos contratando" OR "oportunidade")'
        )
        if cidade:
            vaga_dork += f' "{cidade}"'

        linkedin_urls, vaga_urls_raw = await asyncio.gather(
            ddg_buscar_urls(client, linkedin_dork, max_urls=2, filtro_dominio="linkedin.com/company"),
            ddg_buscar_urls(client, vaga_dork, max_urls=4),
        )
        res.linkedin_url = linkedin_urls[0] if linkedin_urls else ""
        # Filtra vaga_urls: evita redes sociais genericas, foca em paginas reais
        res.vaga_urls = [
            u for u in vaga_urls_raw
            if not any(x in u for x in ["facebook.com", "instagram.com", "twitter.com"])
        ][:3]

        # ---- FASE 2: VISITAR todas as paginas (detect roda no conteudo) ----
        # Monta lista de (chave_fonte, url) pra visitar
        paginas_pra_visitar = []
        if instagram_url:
            paginas_pra_visitar.append(("instagram", instagram_url))
        if facebook_url:
            paginas_pra_visitar.append(("facebook", facebook_url))
        if res.linkedin_url:
            paginas_pra_visitar.append(("linkedin", res.linkedin_url))
        for vu in res.vaga_urls:
            paginas_pra_visitar.append(("vaga", vu))

        # Visita site (multi-pagina) + as outras paginas, tudo paralelo
        site_task = coletar_site(client, site_url) if site_url else _empty_dict()
        outras_tasks = [visitar(client, u) for _, u in paginas_pra_visitar]
        site_dict, *outras_textos = await asyncio.gather(site_task, *outras_tasks)

        # textos_por_url: cada URL com seu texto
        if isinstance(site_dict, dict):
            for u, t in site_dict.items():
                res.textos_por_url[u] = t
            if site_dict:
                res.fontes_consultadas.append("site")

        for (fonte, url), texto in zip(paginas_pra_visitar, outras_textos):
            if texto and len(texto) > 50:
                res.textos_por_url[url] = texto
                res.fontes_consultadas.append(fonte)

        # ---- FASE 3: CNPJ via BrasilAPI (dado estruturado direto) ----
        try:
            if cnpj:
                cnpj_data = await cnpj_socios.consultar_brasilapi(client, cnpj)
            else:
                cnpj_data = await cnpj_socios.descobrir_cnpj_verificado(
                    client, nome, endereco_gmaps=endereco_gmaps,
                    cidade=cidade, min_proximidade=60,
                )
            if cnpj_data:
                res.cnpj_data = cnpj_data
                res.fontes_consultadas.append("brasilapi_cnpj")
        except Exception as e:
            log.debug(f"cnpj falhou: {e}")

    # ---- DETECCAO: roda SO no conteudo de paginas reais ----
    all_sinais = []
    categorias_aplicadas = set()
    boost_total = 0
    for url, texto in res.textos_por_url.items():
        sinais_da_pagina, _ = detect(texto)
        for s in sinais_da_pagina:
            s.source_url = url  # URL REAL E VERIFICAVEL da pagina onde foi visto
            # source_name = tipo de fonte (deduz da url)
            if "linkedin.com" in url:
                s.source_name = "linkedin"
            elif "instagram.com" in url:
                s.source_name = "instagram"
            elif "facebook.com" in url:
                s.source_name = "facebook"
            elif url in res.vaga_urls:
                s.source_name = "vaga"
            else:
                s.source_name = "site"
            all_sinais.append(s)
            if s.categoria not in categorias_aplicadas:
                categorias_aplicadas.add(s.categoria)
                boost_total += s.boost

    res.sinais = all_sinais
    res.boost_score = min(boost_total, 100)

    log.info(
        f"  deep_osint_v2.2: {len(res.fontes_consultadas)} fontes "
        f"({len(res.textos_por_url)} paginas visitadas), "
        f"{len(all_sinais)} sinais ({len(categorias_aplicadas)} cat), boost +{res.boost_score}, "
        f"linkedin={bool(res.linkedin_url)} vagas={len(res.vaga_urls)} cnpj={bool(res.cnpj_data)}"
    )
    return res


async def _empty_dict() -> dict:
    return {}
