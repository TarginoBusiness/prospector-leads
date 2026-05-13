"""
Reverse phone lookup via Google dork + classifieds + sites publicos.

Pra um telefone tipo +5598999998888, busca:
- O numero literal em DDG (acha classificados, listagens de empresa)
- O numero entre aspas no Google via DDG dork
- Tudosabre / consulta-numero-celular publicos

Retorna nome alternativo, contexto onde foi mencionado, e qualquer
URL adicional pra investigar.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.reverse_phone")


def _format_variations(phone: str) -> list[str]:
    """Gera variacoes do telefone pra buscar."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("55"):
        digits = digits[2:]
    if len(digits) < 10:
        return []

    ddd = digits[:2]
    rest = digits[2:]

    variations = [
        digits,                       # 11999998888
        f"({ddd}){rest[:5]}-{rest[5:]}",      # (11)99999-8888
        f"({ddd}) {rest[:5]}-{rest[5:]}",     # (11) 99999-8888
        f"{ddd} {rest[:5]} {rest[5:]}",       # 11 99999 8888
        f"+55{digits}",
        f"+55 {ddd} {rest[:5]}-{rest[5:]}",
    ]
    return variations


async def buscar_via_ddg(client: httpx.AsyncClient, phone: str) -> dict:
    """DDG dorks no telefone."""
    variations = _format_variations(phone)
    if not variations:
        return {}

    snippets = []
    urls = []
    nomes_encontrados = set()

    for var in variations[:3]:  # 3 melhores variantes
        dork = f'"{var}"'
        try:
            r = await client.get(
                f"https://html.duckduckgo.com/html/?q={quote(dork)}&kl=br-pt",
                timeout=10.0,
            )
            if r.status_code != 200:
                continue
            tree = HTMLParser(r.text)
            for result in tree.css(".result")[:5]:
                title = result.css_first(".result__title")
                snip = result.css_first(".result__snippet")
                link = result.css_first("a.result__a")
                if title:
                    nome = title.text(strip=True)
                    if nome:
                        nomes_encontrados.add(nome[:80])
                if snip:
                    snippets.append(snip.text(strip=True))
                if link:
                    href = link.attributes.get("href", "")
                    m = re.search(r"uddg=([^&]+)", href)
                    if m:
                        from urllib.parse import unquote
                        href = unquote(m.group(1))
                    if href.startswith("http"):
                        urls.append(href)
        except Exception:
            continue

    return {
        "telefone_original": phone,
        "nomes_encontrados": list(nomes_encontrados)[:10],
        "snippets": snippets[:20],
        "urls": urls[:15],
    }
