"""
Twitter via Nitter — mirror publico sem login.

Algumas instancias Nitter ainda funcionam. Tentamos a lista em ordem
de confiabilidade ate uma responder.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.nitter")

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.net",
]


async def buscar_perfil_twitter(client: httpx.AsyncClient, nome: str) -> str | None:
    """Tenta achar o @ do Twitter do lead via DDG dork."""
    dork = f'site:twitter.com "{nome}"'
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
            mm = re.search(r"(?:twitter|x)\.com/(\w+)", href)
            if mm and mm.group(1) not in {"search", "home", "explore"}:
                return mm.group(1)
        return None
    except Exception:
        return None


async def coletar_tweets(client: httpx.AsyncClient, username: str) -> dict:
    """Tenta cada instancia Nitter ate uma funcionar. Retorna tweets + hashtags."""
    if not username:
        return {}

    for base in NITTER_INSTANCES:
        try:
            url = f"{base}/{username}"
            r = await client.get(url, timeout=10.0, follow_redirects=True)
            if r.status_code != 200:
                continue
            tree = HTMLParser(r.text)
            tweets = []
            for t in tree.css(".tweet-content")[:20]:
                txt = (t.text() or "").strip()
                if txt:
                    tweets.append(txt[:500])

            hashtags = set()
            for tw in tweets:
                for m in re.finditer(r"#([a-zA-Z0-9_]+)", tw):
                    hashtags.add(m.group(1).lower())

            if tweets:
                return {
                    "username": username,
                    "instance": base,
                    "tweets": tweets,
                    "hashtags": sorted(hashtags)[:30],
                    "tweets_text": " ".join(tweets),
                }
        except Exception:
            continue

    log.info(f"  todas instancias Nitter falharam pra @{username}")
    return {}
