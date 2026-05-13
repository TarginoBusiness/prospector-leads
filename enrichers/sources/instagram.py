"""
Instagram — bio + hashtags + open graph (sem login).

Limitacao Meta: lista de seguindo bloqueada. Mas conseguimos:
- Bio do perfil (meta description)
- Open graph metadata
- Numero de followers/posts via JSON-LD embedado no HTML
- Hashtags da bio + hashtags detectaveis no HTML inicial
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.instagram")


async def buscar_perfil(client: httpx.AsyncClient, nome: str, cidade: str = "") -> str | None:
    dork = f'site:instagram.com "{nome}"' + (f' "{cidade}"' if cidade else "")
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
            if "instagram.com/" in href and not "/p/" in href:
                return href.split("?")[0]
        return None
    except Exception:
        return None


async def coletar_perfil(client: httpx.AsyncClient, ig_url: str) -> dict:
    """Bio + hashtags + counts do perfil publico."""
    if not ig_url:
        return {}
    try:
        r = await client.get(ig_url, timeout=12.0, follow_redirects=True)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:
        return {}

    tree = HTMLParser(html)
    out: dict = {"url": ig_url}

    # Bio
    bio_partes = []
    for sel in ["meta[name='description']", "meta[property='og:description']"]:
        m = tree.css_first(sel)
        if m:
            content = m.attributes.get("content", "")
            if content:
                bio_partes.append(content)
    out["bio"] = " | ".join(set(bio_partes))[:1000]

    # Hashtags presentes na bio + no HTML inicial
    hashtags = set()
    for m in re.finditer(r"#([a-zA-Z0-9_]+)", out["bio"] + " " + html[:50000]):
        tag = m.group(1).lower()
        if 3 <= len(tag) <= 30:
            hashtags.add(tag)
    out["hashtags"] = sorted(hashtags)[:30]

    # Counts via regex no embed JSON
    for m in re.finditer(r'"edge_followed_by":\{"count":(\d+)\}', html):
        out["n_followers"] = int(m.group(1))
        break
    for m in re.finditer(r'"edge_owner_to_timeline_media":\{"count":(\d+)\}', html):
        out["n_posts"] = int(m.group(1))
        break

    return out
