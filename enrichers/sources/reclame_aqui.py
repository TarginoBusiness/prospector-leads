"""
Reclame Aqui — pain points reais publicados por clientes.

Quando uma empresa aparece no Reclame Aqui com queixas sobre
"atendimento demorou", "ninguem responde no WhatsApp", "perdi pedido"
etc, e sinal direto de que ELA PRECISA de automacao.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.reclame_aqui")


async def buscar_reclamacoes(client: httpx.AsyncClient, nome: str) -> dict:
    """Dork no site reclameaqui pra essa empresa."""
    dork = f'site:reclameaqui.com.br "{nome}"'
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(dork)}",
            timeout=10.0,
        )
        if r.status_code != 200:
            return {}
        tree = HTMLParser(r.text)
        snippets = []
        urls = []
        for result in tree.css(".result")[:10]:
            snip = result.css_first(".result__snippet")
            link = result.css_first("a.result__a")
            if snip:
                snippets.append(snip.text(strip=True))
            if link:
                href = link.attributes.get("href", "")
                import re
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    href = unquote(m.group(1))
                if "reclameaqui.com.br" in href:
                    urls.append(href)

        return {
            "n_resultados": len(urls),
            "urls": urls,
            "snippets": snippets,
            "texto_concatenado": " ".join(snippets),
        }
    except Exception:
        return {}
