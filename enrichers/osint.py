"""
OSINT enricher — cross-reference de dados publicos.

Tecnicas implementadas:
1. Username pivoting: dado um username, checa existencia em N plataformas
   (Instagram, GitHub, Twitter/X, Facebook, TikTok, LinkedIn perfis publicos).
2. DuckDuckGo dorking: busca "<nome>" "<cidade>" em plataformas alvo, retorna
   URLs encontradas (mais leve que Google, sem captcha).
3. Extrai contatos dos snippets retornados.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from enrichers.extractors import ContactInfo, extract_all

log = logging.getLogger("enricher.osint")


# Plataformas pra checagem por username.
# {plataforma: (url_template, regex_pra_detectar_existencia_no_html_OU_None_se_status_basta)}
PLATFORMS = {
    "instagram":  ("https://www.instagram.com/{u}/",          None),
    "github":     ("https://github.com/{u}",                  None),
    "twitter":    ("https://twitter.com/{u}",                 None),
    "x":          ("https://x.com/{u}",                       None),
    "facebook":   ("https://www.facebook.com/{u}",            None),
    "tiktok":     ("https://www.tiktok.com/@{u}",             None),
    "linkedin":   ("https://br.linkedin.com/in/{u}",          None),
    "pinterest":  ("https://br.pinterest.com/{u}/",           None),
    "youtube":    ("https://www.youtube.com/@{u}",            None),
    "reddit":     ("https://www.reddit.com/user/{u}",         None),
}


@dataclass
class OsintResult:
    perfis_encontrados: dict[str, str] = field(default_factory=dict)  # plataforma -> url
    urls_dork: list[str] = field(default_factory=list)
    contatos: ContactInfo = field(default_factory=lambda: ContactInfo([], [], [], []))


async def _check_platform(client: httpx.AsyncClient, plat: str, username: str) -> str | None:
    url_tpl, _ = PLATFORMS[plat]
    url = url_tpl.format(u=username)
    try:
        r = await client.head(url, follow_redirects=True, timeout=8.0)
        # Maioria das plataformas devolve 200 quando existe, 404 quando nao
        if r.status_code == 200:
            return url
        if r.status_code in (301, 302, 308):
            return url
        return None
    except Exception:
        return None


async def username_pivot(username: str) -> dict[str, str]:
    """Checa existencia do username em ~10 plataformas em paralelo."""
    if not username or len(username) < 3:
        return {}
    # Sanitiza
    u = re.sub(r"[^a-zA-Z0-9_.-]", "", username)
    if not u:
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [_check_platform(client, p, u) for p in PLATFORMS]
        results = await asyncio.gather(*tasks)

    encontrados = {}
    for plat, url in zip(PLATFORMS.keys(), results):
        if url:
            encontrados[plat] = url
    return encontrados


async def ddg_search(query: str, max_results: int = 10) -> tuple[list[str], str]:
    """Busca DuckDuckGo HTML (lite). Retorna (urls, texto_completo_snippets)."""
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}&kl=br-pt"
    log.info(f"  DDG dork: {query}")
    async with httpx.AsyncClient(
        timeout=15.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    ) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
        except Exception as e:
            log.warning(f"  DDG falhou: {e}")
            return [], ""

    tree = HTMLParser(r.text)
    urls = []
    snippets = []
    for result in tree.css(".result")[:max_results]:
        link = result.css_first("a.result__a")
        snippet = result.css_first(".result__snippet")
        if link:
            href = link.attributes.get("href", "")
            # DDG envolve em redirect /l/?uddg=
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote
                href = unquote(m.group(1))
            if href.startswith("http"):
                urls.append(href)
        if snippet:
            snippets.append(snippet.text(strip=True))

    return urls, " ".join(snippets)


async def enriquecer_osint(*, username: str = "", nome: str = "", cidade: str = "", nicho: str = "") -> OsintResult:
    """
    Pipeline completo de OSINT.
    Sao 3 fases:
      1) Username pivot em 10 plataformas (se temos username)
      2) DDG dorks combinando nome + cidade + plataforma alvo
      3) Roda extractors em todos os snippets pra pegar contatos vazados
    """
    res = OsintResult()

    # FASE 1: Username pivot
    if username:
        try:
            res.perfis_encontrados = await username_pivot(username)
            log.info(f"  username pivot: {len(res.perfis_encontrados)} plataformas com '@{username}'")
        except Exception as e:
            log.warning(f"username_pivot falhou: {e}")

    # FASE 2: Dorks (so se temos nome + cidade)
    if nome and len(nome.split()) >= 2 and cidade:
        dorks = []
        # Pega contatos por plataformas-alvo
        dorks.append(f'"{nome}" "{cidade}" (whatsapp OR telefone OR contato)')
        dorks.append(f'"{nome}" "{cidade}" site:instagram.com')
        dorks.append(f'"{nome}" "{cidade}" site:facebook.com')
        if nicho:
            dorks.append(f'"{nome}" {nicho} {cidade}')

        snippets_combined = []
        for dork in dorks:
            urls, snippets = await ddg_search(dork, max_results=5)
            res.urls_dork.extend(urls)
            snippets_combined.append(snippets)
            await asyncio.sleep(2)  # cortesia com DDG

        # FASE 3: Extrai contatos de TODO o texto coletado
        texto_total = " ".join(snippets_combined)
        res.contatos = extract_all(texto_total)

    # Deduplicar URLs
    res.urls_dork = list(dict.fromkeys(res.urls_dork))

    return res
