"""
Scraper do Workana — busca projetos publicados que batem com servicos do Targino.

Estrategia: usa a busca publica do Workana (nao precisa login) filtrando por
categoria "Programacao e Tecnologia". Cada projeto = um lead potencial:
quem postou o projeto, descricao, palavras-chave, cidade (se publicada).
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

log = logging.getLogger("scraper.workana")

SOURCE = "workana"
BASE_URL = "https://www.workana.com"
SEARCH_URL = (
    "https://www.workana.com/jobs"
    "?category=it-programming&language=pt&query={query}"
)

# Queries iniciais — recortes do nosso publico-alvo
QUERIES = [
    "bot whatsapp",
    "chatbot",
    "automacao whatsapp",
    "aplicativo personalizado",
    "site institucional",
    "landing page",
    "sistema personalizado",
    "inteligencia artificial",
    "agente de ia",
    "automatizar planilha",
]


async def scrape_search(query: str) -> list[dict]:
    """Faz a busca, raspa a primeira pagina de resultados."""
    url = SEARCH_URL.format(query=query.replace(" ", "+"))
    log.info(f"[{query}] GET {url}")
    async with http_client() as client:
        resp = await fetch(client, url)
        html = resp.text

    repo.insert_raw_page(SOURCE, url, html)

    tree = HTMLParser(html)
    projetos = []
    # Workana usa class `project-item` no listing
    for card in tree.css("div.project-item, .project, article"):
        link_node = card.css_first("h2 a, a.project-title, a.project-name")
        if not link_node:
            continue
        titulo = link_node.text(strip=True)
        href = link_node.attributes.get("href", "")
        url_projeto = urljoin(BASE_URL, href) if href else ""

        descricao_node = card.css_first(".project-description, .project-details p")
        descricao = descricao_node.text(strip=True) if descricao_node else ""

        autor_node = card.css_first(".project-client a, .client-name")
        autor = autor_node.text(strip=True) if autor_node else None

        projetos.append({
            "titulo": titulo,
            "descricao": descricao,
            "url": url_projeto,
            "autor": autor,
        })

    log.info(f"[{query}] encontrados {len(projetos)} projetos")
    return projetos


async def processar_projeto(projeto: dict) -> tuple[bool, bool]:
    """
    Avalia 1 projeto, gera lead se relevante.
    Retorna (foi_processado, eh_novo).
    """
    texto = f"{projeto['titulo']} {projeto['descricao']}"

    score = scoring_engine.calcular(
        source=SOURCE,
        texto_para_analisar=texto,
    )

    if score.score < 20:
        return True, False  # ignora ruim

    fingerprint = repo.make_fingerprint(
        telefone=None,
        email=None,
        nome=projeto.get("autor") or projeto["titulo"],
        source=SOURCE,
    )

    lead = {
        "fingerprint": fingerprint,
        "source": SOURCE,
        "source_url": projeto["url"],
        "nome": projeto.get("autor"),
        "nicho": score.nicho,
        "cidade_tag": score.cidade_tag,
        "score_temperatura": score.score,
        "score_breakdown": score.breakdown,
        "raw_payload": {
            "titulo": projeto["titulo"],
            "descricao": projeto["descricao"][:1000],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    lead_id, is_new = repo.upsert_lead(lead)

    if score.intent_signals:
        repo.insert_intent_signals(
            lead_id,
            [
                {
                    "categoria": s.categoria,
                    "palavra_chave": s.palavra_chave,
                    "trecho_texto": s.trecho,
                    "source_url": projeto["url"],
                    "boost": s.boost,
                }
                for s in score.intent_signals
            ],
        )

    log.info(
        f"  lead {'NOVO' if is_new else 'atualizado'} id={lead_id} score={score.score}"
        f" cidade={score.cidade_tag} nicho={score.nicho}"
    )
    return True, is_new


async def main() -> None:
    run_id = repo.start_run(SOURCE, metadata={"queries": QUERIES})
    pages_ok = pages_failed = leads_new = leads_updated = 0
    err = None

    try:
        for query in QUERIES:
            try:
                projetos = await scrape_search(query)
                pages_ok += 1
                for p in projetos:
                    try:
                        _, is_new = await processar_projeto(p)
                        if is_new:
                            leads_new += 1
                        else:
                            leads_updated += 1
                    except Exception as e:
                        log.exception(f"falhou ao processar projeto {p.get('url')}: {e}")
                        repo.push_dead_letter(p.get("url", "?"), SOURCE, str(e))
            except Exception as e:
                log.exception(f"falhou query {query}: {e}")
                pages_failed += 1

            await human_delay()
    except Exception as e:
        err = str(e)
        log.exception(e)
    finally:
        repo.end_run(
            run_id,
            pages_ok=pages_ok,
            pages_failed=pages_failed,
            leads_new=leads_new,
            leads_updated=leads_updated,
            error_summary=err,
        )
        log.info(
            f"DONE run={run_id} ok={pages_ok} fail={pages_failed} "
            f"new={leads_new} upd={leads_updated}"
        )


if __name__ == "__main__":
    asyncio.run(main())
