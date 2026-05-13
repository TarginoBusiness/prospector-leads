"""
Scraper do 99Freelas — mesma logica do Workana, adaptada pro layout deles.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from db import repo
from scoring import engine as scoring_engine
from scrapers.base import fetch, http_client, human_delay

log = logging.getLogger("scraper.99freelas")

SOURCE = "99freelas"
BASE_URL = "https://www.99freelas.com.br"
SEARCH_URL = "https://www.99freelas.com.br/projects?keyword={query}&filter=open"

QUERIES = [
    "bot whatsapp",
    "chatbot",
    "automacao",
    "aplicativo",
    "app mobile",
    "site",
    "landing page",
    "sistema",
    "inteligencia artificial",
    "automatizar",
]


async def scrape_search(query: str) -> list[dict]:
    url = SEARCH_URL.format(query=query.replace(" ", "+"))
    log.info(f"[{query}] GET {url}")
    async with http_client() as client:
        resp = await fetch(client, url)
        html = resp.text

    repo.insert_raw_page(SOURCE, url, html)
    tree = HTMLParser(html)

    projetos = []
    for card in tree.css(".project-item, .project, li.project, article"):
        link = card.css_first("h2 a, a.project-title, a[href*='/projects/']")
        if not link:
            continue
        titulo = link.text(strip=True)
        href = link.attributes.get("href", "")
        url_p = urljoin(BASE_URL, href)

        desc_node = card.css_first(".project-description, p.description")
        descricao = desc_node.text(strip=True) if desc_node else ""

        projetos.append({"titulo": titulo, "descricao": descricao, "url": url_p})

    log.info(f"[{query}] encontrados {len(projetos)} projetos")
    return projetos


async def processar(p: dict) -> tuple[bool, bool]:
    texto = f"{p['titulo']} {p['descricao']}"
    s = scoring_engine.calcular(source=SOURCE, texto_para_analisar=texto)
    if s.score < 20:
        return True, False

    fp = repo.make_fingerprint(telefone=None, email=None, nome=p["titulo"], source=SOURCE)
    lead = {
        "fingerprint": fp,
        "source": SOURCE,
        "source_url": p["url"],
        "nome": None,
        "nicho": s.nicho,
        "cidade_tag": s.cidade_tag,
        "score_temperatura": s.score,
        "score_breakdown": s.breakdown,
        "raw_payload": {
            "titulo": p["titulo"],
            "descricao": p["descricao"][:1000],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    lead_id, is_new = repo.upsert_lead(lead)
    if s.intent_signals:
        repo.insert_intent_signals(
            lead_id,
            [{
                "categoria": x.categoria,
                "palavra_chave": x.palavra_chave,
                "trecho_texto": x.trecho,
                "source_url": p["url"],
                "boost": x.boost,
            } for x in s.intent_signals],
        )
    log.info(f"  {'NOVO' if is_new else 'upd'} id={lead_id} score={s.score}")
    return True, is_new


async def main() -> None:
    run_id = repo.start_run(SOURCE, metadata={"queries": QUERIES})
    ok = fail = nuevos = upd = 0
    err = None
    try:
        for q in QUERIES:
            try:
                ps = await scrape_search(q)
                ok += 1
                for p in ps:
                    try:
                        _, is_new = await processar(p)
                        if is_new: nuevos += 1
                        else: upd += 1
                    except Exception as e:
                        log.exception(e)
                        repo.push_dead_letter(p.get("url", "?"), SOURCE, str(e))
            except Exception as e:
                log.exception(e)
                fail += 1
            await human_delay()
    except Exception as e:
        err = str(e)
    finally:
        repo.end_run(run_id, pages_ok=ok, pages_failed=fail, leads_new=nuevos, leads_updated=upd, error_summary=err)
        log.info(f"DONE run={run_id} ok={ok} fail={fail} new={nuevos} upd={upd}")


if __name__ == "__main__":
    asyncio.run(main())
