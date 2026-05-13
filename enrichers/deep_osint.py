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

async def coletar_site(client: httpx.AsyncClient, site_url: str) -> str:
    """Visita home + /sobre + /servicos + /blog + /vagas — agrega todo o texto."""
    if not site_url:
        return ""
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url

    paths = ["", "/sobre", "/about", "/servicos", "/blog", "/vagas",
             "/carreiras", "/jobs", "/contato"]

    textos = []
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for p in paths:
        try:
            url = base + p
            r = await client.get(url, timeout=10.0, follow_redirects=True)
            if r.status_code != 200:
                continue
            tree = HTMLParser(r.text)
            txt = (tree.text() or "")[:20000]
            textos.append(txt)
            await asyncio.sleep(0.3)
        except Exception:
            continue

    return " ".join(textos)


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
    Dorks especificos pra detectar pistas de que a empresa esta no contexto.
    Cada dork retorna snippets, todos concatenados.
    """
    dorks = [
        f'"{nome}" vaga (desenvolvedor OR programador OR ti)',
        f'"{nome}" "{cidade}" (automacao OR chatbot OR "inteligencia artificial")',
        f'"{nome}" "{cidade}" (site OR "novo aplicativo" OR app)',
        f'"{nome}" linkedin (vaga OR contratacao)',
    ]
    textos = []
    for d in dorks:
        snip = await ddg_search(client, d, max_results=3)
        if snip:
            textos.append(snip)
        await asyncio.sleep(1.5)  # cortesia DDG
    return " ".join(textos)


# ============================================================================
# Fonte 4: site da prefeitura/regionais (mencao da empresa em contexto tech)
# ============================================================================

async def coletar_mencoes_regionais(client: httpx.AsyncClient, nome: str) -> str:
    """Busca mencoes da empresa em portais regionais + universidades + governo."""
    dorks = [
        f'"{nome}" (sebrae OR senac OR senai OR fapema OR cnpq)',
        f'"{nome}" (premio OR inovacao OR startup)',
    ]
    textos = []
    for d in dorks:
        snip = await ddg_search(client, d, max_results=3)
        if snip:
            textos.append(snip)
        await asyncio.sleep(1.5)
    return " ".join(textos)


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

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=20.0) as client:
        # Fonte 1: Site oficial
        if site_url:
            txt = await coletar_site(client, site_url)
            if txt:
                res.textos_coletados["site"] = txt[:50000]
                res.fontes_consultadas.append("site")

        # Fonte 2: Instagram
        if instagram_url:
            txt = await coletar_instagram(client, instagram_url)
            if txt:
                res.textos_coletados["instagram"] = txt
                res.fontes_consultadas.append("instagram")

        # Fonte 3: Facebook (mesma logica do IG)
        if facebook_url:
            txt = await coletar_instagram(client, facebook_url)
            if txt:
                res.textos_coletados["facebook"] = txt
                res.fontes_consultadas.append("facebook")

        # Fonte 4: DDG dorks
        if cidade:
            txt = await coletar_dorks(client, nome, cidade)
            if txt:
                res.textos_coletados["ddg_dorks"] = txt
                res.fontes_consultadas.append("ddg_dorks")

        # Fonte 5: Mencoes regionais
        txt = await coletar_mencoes_regionais(client, nome)
        if txt:
            res.textos_coletados["mencoes_regionais"] = txt
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
