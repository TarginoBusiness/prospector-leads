"""
LinkedIn — pagina publica da EMPRESA (nao do perfil pessoal).

Sem login, ainda da pra puxar do HTML inicial:
- Descricao da empresa
- Industria
- Tamanho (range de funcionarios)
- Sede
- Especialidades

NAO da pra puxar funcionarios reais nem postagens individuais.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.linkedin_company")


async def buscar_pagina_empresa(client: httpx.AsyncClient, nome: str, cidade: str = "") -> str | None:
    dork = f'site:linkedin.com/company "{nome}"' + (f' "{cidade}"' if cidade else "")
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(dork)}",
            timeout=10.0,
        )
        if r.status_code != 200:
            return None
        tree = HTMLParser(r.text)
        for result in tree.css(".result__a"):
            href = result.attributes.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote
                href = unquote(m.group(1))
            if "linkedin.com/company/" in href:
                return href.split("?")[0]
        return None
    except Exception:
        return None


async def coletar_pagina_empresa(client: httpx.AsyncClient, page_url: str) -> dict:
    if not page_url:
        return {}
    try:
        r = await client.get(page_url, timeout=12.0, follow_redirects=True)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:
        return {}

    tree = HTMLParser(html)
    out: dict = {"url": page_url}

    # Meta description = descricao da empresa
    desc = tree.css_first("meta[name='description']")
    if desc:
        out["descricao"] = (desc.attributes.get("content") or "")[:2000]

    # JSON-LD structured data: industry, size, etc
    for s in tree.css("script[type='application/ld+json']"):
        try:
            import json as _json
            payload = _json.loads(s.text())
            if isinstance(payload, dict):
                if payload.get("@type") == "Organization":
                    out["industry"] = payload.get("industry", "")
                    out["address"] = payload.get("address", "")
                    out["founded"] = payload.get("foundingDate", "")
        except Exception:
            pass

    # Headcount range via regex no html
    m = re.search(r"(\d{1,3}(?:\.\d{3})*[\.\s]*-?\s*\d{1,3}(?:\.\d{3})*)\s*funcionari", html, re.I)
    if m:
        out["headcount"] = m.group(0)

    return out
