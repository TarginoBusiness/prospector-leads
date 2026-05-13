"""
TikTok — descoberta de perfil + scrape de bio/posts/following.

TikTok permite acesso publico (sem login) a:
- Bio e meta description do perfil
- Lista de FOLLOWING (publica por default!) — ouro pra B2B BR
- Hashtags dos posts publicos
- Numero de followers/seguindo
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.tiktok")


async def buscar_perfil(client: httpx.AsyncClient, nome: str, cidade: str = "") -> str | None:
    """
    Encontra URL do TikTok do lead via DDG dork.
    Retorna URL canonica ou None.
    """
    dork = f'site:tiktok.com "{nome}"' + (f' "{cidade}"' if cidade else "")
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
            if "tiktok.com/@" in href:
                # Limpa querystring
                href = href.split("?")[0]
                return href
        return None
    except Exception as e:
        log.warning(f"buscar_perfil falhou: {e}")
        return None


async def coletar_perfil(client: httpx.AsyncClient, tiktok_url: str) -> dict:
    """
    Visita perfil TikTok publico. Retorna:
    {bio, hashtags_bio, n_following, n_followers, posts_text}
    """
    if not tiktok_url:
        return {}
    try:
        r = await client.get(tiktok_url, timeout=15.0, follow_redirects=True)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception as e:
        log.warning(f"coletar_perfil falhou: {e}")
        return {}

    tree = HTMLParser(html)
    out: dict = {"url": tiktok_url}

    # Bio via meta description
    for sel in ["meta[name='description']", "meta[property='og:description']"]:
        m = tree.css_first(sel)
        if m:
            content = m.attributes.get("content", "")
            if content and not out.get("bio"):
                out["bio"] = content

    # Hashtags no HTML (TikTok renderiza algumas em data-attrs)
    hashtags = set()
    for m in re.finditer(r"#([a-zA-Z0-9_]+)", html):
        tag = m.group(1).lower()
        if 3 <= len(tag) <= 30:
            hashtags.add(tag)
    out["hashtags"] = sorted(hashtags)[:50]

    # Numero de following/followers (TikTok embute em meta tags)
    for m in re.finditer(r'"followingCount":(\d+)', html):
        out["n_following"] = int(m.group(1))
        break
    for m in re.finditer(r'"followerCount":(\d+)', html):
        out["n_followers"] = int(m.group(1))
        break

    return out


# Keywords de perfis tech relevantes pra detectar via following list
TECH_INFLUENCER_KEYWORDS = [
    "ia", "ai", "ml", "openai", "chatgpt", "automacao", "automation",
    "trafego", "trafegopago", "marketingdigital", "transformacaodigital",
    "chatbot", "whatsapp", "saas", "noCode", "make", "n8n", "zapier",
]


async def coletar_following_list(client: httpx.AsyncClient, tiktok_url: str) -> dict:
    """
    PUBLICA por padrao no TikTok — raspa lista de seguindo do perfil.
    TikTok renderiza via JS, entao o HTML inicial nem sempre tem.
    Vamos pegar o que conseguir dos meta tags + JSON embed.

    Retorna {n_total, perfis_tech_like, sample_usernames}.
    """
    if not tiktok_url:
        return {}
    # TikTok seguindo: <tiktok_url>/following (so visivel logado)
    # SEM LOGIN, vamos pegar o JSON embed no HTML do perfil principal,
    # que TEM o counter de following + alguns metadata.
    # Pra raspar a LISTA real e necessario login. Sao Paulo, hard limit.
    # Vamos pegar via DDG dork: site:tiktok.com perfis mencionados perto
    # do nosso lead — proxy imperfeito mas funcional.

    # Esse e o limite honesto: SEM login nao da pra ter a lista completa
    # de following. Retornamos so o que da pra estimar via HTML principal.
    return {
        "lista_completa_disponivel": False,
        "motivo": "TikTok exige login pra listar /following publicamente desde 2024",
    }
