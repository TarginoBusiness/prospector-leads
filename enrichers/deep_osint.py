"""
DEEP OSINT — aprofunda 1 lead pra detectar SINAIS DE INTERESSE.

Diferente do enricher de telefone (que busca contato), aqui buscamos pistas
de que a empresa ESTA no momento certo de comprar tech:
  - Site fala sobre automacao? IA? tem vaga de dev?
  - Instagram bio menciona chatbot, agendamento automatico, etc?
  - Vagas no Catho/Vagas.com pra essa empresa?
  - Posts recentes mencionando tema tech?

Coleta TEXTO de varias fontes publicas e roda o interest_detector.
Retorna lista de InterestSignals + boost agregado.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import httpx
from selectolax.parser import HTMLParser

from enrichers.interest_detector import InterestSignal, detect

log = logging.getLogger("enricher.deep_osint")


async def _empty() -> str:
    """Placeholder pra asyncio.gather quando uma fonte e skip."""
    return ""


@dataclass
class DeepOsintResult:
    sinais: list[InterestSignal] = field(default_factory=list)
    boost_score: int = 0
    fontes_consultadas: list[str] = field(default_factory=list)
    textos_coletados: dict[str, str] = field(default_factory=dict)


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ============================================================================
# Fonte 1: Site oficial da empresa
# ============================================================================

async def _fetch_path(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=5.0, follow_redirects=True)
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        return (tree.text() or "")[:20000]
    except Exception:
        return ""


async def coletar_site(client: httpx.AsyncClient, site_url: str) -> str:
    """Visita home + /sobre + /servicos + /vagas EM PARALELO. Rapido."""
    if not site_url:
        return ""
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url

    # Reduzido pra 4 paths mais valiosos + timeout 5s + parallel
    paths = ["", "/sobre", "/servicos", "/vagas"]
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls = [base + p for p in paths]

    textos = await asyncio.gather(*[_fetch_path(client, u) for u in urls])
    return " ".join(t for t in textos if t)


# ============================================================================
# Fonte 2: Instagram bio + open graph
# ============================================================================

async def coletar_instagram(client: httpx.AsyncClient, ig_url: str) -> str:
    """Pega meta description (bio) + open graph do perfil."""
    if not ig_url:
        return ""
    try:
        r = await client.get(ig_url, timeout=10.0, follow_redirects=True)
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        partes = []
        # Meta description tem a bio
        for sel in ["meta[name='description']", "meta[property='og:description']", "meta[property='og:title']"]:
            for m in tree.css(sel):
                content = m.attributes.get("content", "")
                if content:
                    partes.append(content)
        return " ".join(partes)
    except Exception:
        return ""


# ============================================================================
# Fonte 3: DDG dorks pra detectar pistas de interesse
# ============================================================================

async def ddg_search(client: httpx.AsyncClient, query: str, max_results: int = 5) -> str:
    """Retorna snippets concatenados da busca DDG."""
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(query)}&kl=br-pt",
            timeout=15.0,
        )
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        snippets = []
        for result in tree.css(".result")[:max_results]:
            snip = result.css_first(".result__snippet")
            title = result.css_first(".result__title")
            if snip:
                snippets.append(snip.text(strip=True))
            if title:
                snippets.append(title.text(strip=True))
        return " ".join(snippets)
    except Exception:
        return ""


async def coletar_dorks(client: httpx.AsyncClient, nome: str, cidade: str) -> str:
    """
    Dork unico OR'ado combinando todos os sinais que buscamos.
    1 query so em vez de 4 = muito mais rapido.
    """
    dork = (
        f'"{nome}" '
        f'(vaga OR desenvolvedor OR automacao OR chatbot OR '
        f'"inteligencia artificial" OR "novo site" OR app OR linkedin)'
    )
    return await ddg_search(client, dork, max_results=10)


# ============================================================================
# Fonte 4: site da prefeitura/regionais (mencao da empresa em contexto tech)
# ============================================================================

async def coletar_mencoes_regionais(client: httpx.AsyncClient, nome: str) -> str:
    """Dork unico OR'ado pra mencoes em contextos premium (sebrae/inovacao)."""
    dork = f'"{nome}" (sebrae OR senac OR senai OR fapema OR premio OR inovacao OR startup)'
    return await ddg_search(client, dork, max_results=5)


# ============================================================================
# Orquestrador
# ============================================================================

async def aprofundar_lead(
    *,
    nome: str,
    cidade: str,
    site_url: str = "",
    instagram_url: str = "",
    facebook_url: str = "",
) -> DeepOsintResult:
    """
    Executa todas as fontes pro lead. Concatena os textos coletados,
    roda o interest_detector e devolve sinais + boost.
    """
    res = DeepOsintResult()

    if not nome:
        return res

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=15.0) as client:
        # Roda TODAS as 5 fontes em paralelo (asyncio.gather)
        site_task = coletar_site(client, site_url) if site_url else _empty()
        ig_task = coletar_instagram(client, instagram_url) if instagram_url else _empty()
        fb_task = coletar_instagram(client, facebook_url) if facebook_url else _empty()
        dorks_task = coletar_dorks(client, nome, cidade) if cidade else _empty()
        mencoes_task = coletar_mencoes_regionais(client, nome)

        site_txt, ig_txt, fb_txt, dorks_txt, mencoes_txt = await asyncio.gather(
            site_task, ig_task, fb_task, dorks_task, mencoes_task,
            return_exceptions=False,
        )

        if site_txt:
            res.textos_coletados["site"] = site_txt[:50000]
            res.fontes_consultadas.append("site")
        if ig_txt:
            res.textos_coletados["instagram"] = ig_txt
            res.fontes_consultadas.append("instagram")
        if fb_txt:
            res.textos_coletados["facebook"] = fb_txt
            res.fontes_consultadas.append("facebook")
        if dorks_txt:
            res.textos_coletados["ddg_dorks"] = dorks_txt
            res.fontes_consultadas.append("ddg_dorks")
        if mencoes_txt:
            res.textos_coletados["mencoes_regionais"] = mencoes_txt
            res.fontes_consultadas.append("mencoes_regionais")

    # Junta tudo e roda detector
    texto_total = " ".join(res.textos_coletados.values())
    sinais, boost = detect(texto_total)
    res.sinais = sinais
    res.boost_score = boost

    log.info(
        f"  deep_osint: {len(res.fontes_consultadas)} fontes consultadas, "
        f"{len(sinais)} sinais detectados, boost +{boost}"
    )
    return res
