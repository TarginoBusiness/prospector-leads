"""
Deep OSINT v2 — orquestrador aprofundado (REESCRITO v2.1).

LICAO APRENDIDA: a v2.0 fazia ~7 buscas DuckDuckGo POR LEAD (achar perfil
IG + TikTok + Twitter + LinkedIn + CNPJ + news + reclame). DDG bloqueou
apos ~30 requests → todas as fontes falhavam → 0 sinais + 80s/lead.

ESTRATEGIA v2.1:
- NAO re-descobre perfis via DDG. Usa o site/Instagram/Facebook que o
  enrich_gmaps JA achou e guardou em raw_payload.deep_enrich.
- Visita esses perfis DIRETO (sem DDG).
- BrasilAPI = API direta, sem DDG.
- 1 dork DDG MAXIMO por lead (so pra interest keywords + news).
- Timeout 8s (era 15s) → falhas sao rapidas.
- Resultado esperado: ~5-10s/lead, sem rate-limit.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

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
    cnpj_data: dict = field(default_factory=dict)
    news_urls: list = field(default_factory=list)
    reclame_aqui: dict = field(default_factory=dict)
    textos: dict = field(default_factory=dict)
    fontes_consultadas: list[str] = field(default_factory=list)


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ============================================================================
# Coletores DIRETOS (sem DDG) — usam URLs que ja temos
# ============================================================================

async def _visitar_url(client: httpx.AsyncClient, url: str, max_chars: int = 30000) -> str:
    """Visita uma URL e retorna o texto. Timeout curto pra falhar rapido."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = await client.get(url, timeout=8.0, follow_redirects=True)
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        # Pega meta description + texto do body
        partes = []
        for sel in ["meta[name='description']", "meta[property='og:description']", "meta[property='og:title']"]:
            m = tree.css_first(sel)
            if m:
                c = m.attributes.get("content", "")
                if c:
                    partes.append(c)
        body = tree.text() or ""
        partes.append(body[:max_chars])
        return " ".join(partes)
    except Exception:
        return ""


async def coletar_site_completo(client: httpx.AsyncClient, site_url: str) -> str:
    """Visita home + /sobre + /servicos + /vagas EM PARALELO."""
    if not site_url:
        return ""
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url
    from urllib.parse import urlparse
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    paths = ["", "/sobre", "/servicos", "/vagas", "/contato"]
    textos = await asyncio.gather(*[_visitar_url(client, base + p) for p in paths])
    return " ".join(t for t in textos if t)


# ============================================================================
# 1 DORK DDG por lead — combinado pra pegar interest keywords + news
# ============================================================================

async def dork_unico(client: httpx.AsyncClient, nome: str, cidade: str) -> tuple[str, list]:
    """
    UMA busca DDG só por lead. Combina deteccao de interesse + news.
    Retorna (texto_snippets, urls).
    """
    if not nome:
        return "", []
    dork = (
        f'"{nome}" '
        f'(automacao OR chatbot OR "inteligencia artificial" OR "vaga" OR '
        f'"novo site" OR app OR "transformacao digital" OR whatsapp)'
    )
    if cidade:
        dork += f' "{cidade}"'
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(dork)}&kl=br-pt",
            timeout=10.0,
        )
        if r.status_code != 200:
            return "", []
        tree = HTMLParser(r.text)
        snippets = []
        urls = []
        for result in tree.css(".result")[:8]:
            snip = result.css_first(".result__snippet")
            title = result.css_first(".result__title")
            link = result.css_first("a.result__a")
            if snip:
                snippets.append(snip.text(strip=True))
            if title:
                snippets.append(title.text(strip=True))
            if link:
                href = link.attributes.get("href", "")
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    href = unquote(m.group(1))
                if href.startswith("http"):
                    urls.append(href)
        return " ".join(snippets), urls
    except Exception as e:
        log.debug(f"dork falhou: {e}")
        return "", []


# ============================================================================
# Orquestrador v2.1 — minimo de DDG, maximo de fontes diretas
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
    v2.1 — usa URLs JA CONHECIDAS (do enrich_gmaps), faz 1 dork DDG so.
    """
    res = DeepOsintV2Result()
    if not nome:
        return res

    res.site_url = site_url
    res.instagram_url = instagram_url
    res.facebook_url = facebook_url

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=10.0) as client:
        # TODAS as fontes em paralelo — mas só 1 usa DDG
        tasks = {
            "site":      coletar_site_completo(client, site_url) if site_url else _empty_str(),
            "instagram": _visitar_url(client, instagram_url) if instagram_url else _empty_str(),
            "facebook":  _visitar_url(client, facebook_url) if facebook_url else _empty_str(),
            "dork":      dork_unico(client, nome, cidade),  # 1 DDG só
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        result_map = dict(zip(tasks.keys(), results))

        # Processa textos
        if isinstance(result_map["site"], str) and result_map["site"]:
            res.textos["site"] = result_map["site"]
            res.fontes_consultadas.append("site")
        if isinstance(result_map["instagram"], str) and result_map["instagram"]:
            res.textos["instagram"] = result_map["instagram"]
            res.fontes_consultadas.append("instagram")
        if isinstance(result_map["facebook"], str) and result_map["facebook"]:
            res.textos["facebook"] = result_map["facebook"]
            res.fontes_consultadas.append("facebook")
        if isinstance(result_map["dork"], tuple):
            dork_texto, dork_urls = result_map["dork"]
            if dork_texto:
                res.textos["dork"] = dork_texto
                res.news_urls = dork_urls
                res.fontes_consultadas.append("ddg_dork")

        # CNPJ via BrasilAPI — API DIRETA, sem DDG (rapido e confiavel)
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
                # Razao social + CNAE tambem viram texto pra detectar interesse
                res.textos["cnpj"] = (
                    cnpj_data.get("razao_social", "") + " " +
                    cnpj_data.get("nome_fantasia", "") + " " +
                    cnpj_data.get("cnae_principal", "")
                )
        except Exception as e:
            log.debug(f"cnpj falhou: {e}")

    # Mapa source → URL pra rastrear cada sinal
    source_url_map = {
        "site":      site_url,
        "instagram": instagram_url,
        "facebook":  facebook_url,
        "dork":      (res.news_urls or [""])[0] if res.news_urls else "",
        "cnpj":      "",
    }

    # Detector POR FONTE — cada sinal sabe sua origem
    all_sinais = []
    categorias_aplicadas = set()
    boost_total = 0
    for source_name, texto in res.textos.items():
        sinais_da_fonte, _ = detect(texto)
        for s in sinais_da_fonte:
            s.source_name = source_name
            s.source_url = source_url_map.get(source_name, "")
            all_sinais.append(s)
            if s.categoria not in categorias_aplicadas:
                categorias_aplicadas.add(s.categoria)
                boost_total += s.boost

    res.sinais = all_sinais
    res.boost_score = min(boost_total, 100)

    log.info(
        f"  deep_osint_v2.1: {len(res.fontes_consultadas)} fontes, "
        f"{len(all_sinais)} sinais ({len(categorias_aplicadas)} cat), boost +{res.boost_score}, "
        f"site={bool(site_url)} ig={bool(instagram_url)} cnpj={bool(res.cnpj_data)}"
    )
    return res


async def _empty_str() -> str:
    return ""
