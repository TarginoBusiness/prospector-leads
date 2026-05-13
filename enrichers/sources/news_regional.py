"""
Mencoes em portais de noticia regional BR.

Dork DDG site-restricted nos principais portais.
Pra Sao Luis: imirante, g1.globo.com/ma, oimparcial.com.br
Pra outras cidades: portais nacionais + locais.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.news_regional")


PORTAIS_POR_CIDADE = {
    "sao-luis": [
        "imirante.com",
        "oimparcial.com.br",
        "g1.globo.com/ma",
        "atosemfatos.com.br",
        "vias.com.br",
    ],
    "sao-paulo": [
        "g1.globo.com/sp",
        "estadao.com.br",
        "folha.uol.com.br",
        "exame.com",
    ],
    "curitiba": [
        "g1.globo.com/pr",
        "bemparana.com.br",
        "gazetadopovo.com.br",
    ],
    "rio-de-janeiro": [
        "g1.globo.com/rj",
        "oglobo.globo.com",
        "extra.globo.com",
    ],
}

PORTAIS_NACIONAIS = [
    "exame.com",
    "valor.com.br",
    "estadao.com.br",
    "olhardigital.com.br",
    "tecmundo.com.br",
]


async def buscar_mencoes(client: httpx.AsyncClient, nome: str, cidade_tag: str = "") -> dict:
    portais = PORTAIS_POR_CIDADE.get(cidade_tag or "", []) + PORTAIS_NACIONAIS
    portais = list(dict.fromkeys(portais))[:8]

    # OR'a todos num dork
    site_clauses = " OR ".join(f"site:{p}" for p in portais)
    dork = f'"{nome}" ({site_clauses})'

    snippets = []
    urls = []
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(dork)}&kl=br-pt",
            timeout=12.0,
        )
        if r.status_code != 200:
            return {}
        tree = HTMLParser(r.text)
        for result in tree.css(".result")[:10]:
            snip = result.css_first(".result__snippet")
            title = result.css_first(".result__title")
            link = result.css_first("a.result__a")
            if snip:
                snippets.append(snip.text(strip=True))
            if title:
                snippets.append(title.text(strip=True))
            if link:
                href = link.attributes.get("href", "")
                import re
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    href = unquote(m.group(1))
                if href.startswith("http"):
                    urls.append(href)
        return {
            "snippets": snippets,
            "urls": urls,
            "texto_concatenado": " ".join(snippets),
        }
    except Exception as e:
        log.warning(f"news mentions falhou: {e}")
        return {}
