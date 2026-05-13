"""
Scraper do Workana — usa Playwright pois o Workana e SPA (renderiza via JS).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from db import repo
from scoring import engine as scoring_engine
from scrapers.base import human_delay

log = logging.getLogger("scraper.workana")

SOURCE = "workana"
BASE_URL = "https://www.workana.com"
SEARCH_URL = (
    "https://www.workana.com/jobs"
    "?category=it-programming&language=pt&query={query}"
)

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


async def scrape_search(page, query: str) -> list[dict]:
    """Faz busca com Playwright, espera os cards renderizarem."""
    url = SEARCH_URL.format(query=query.replace(" ", "+"))
    log.info(f"[{query}] GET {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.warning(f"[{query}] timeout no goto: {e}")
        return []

    # Espera os cards carregarem (varios seletores possiveis pq Workana muda layout)
    selectors_to_try = [
        "h3.project-title",
        "h2.title a",
        "a.h-link[href*='/jobs/']",
        ".project-item",
        ".js-project",
    ]
    for sel in selectors_to_try:
        try:
            await page.wait_for_selector(sel, timeout=10000)
            log.info(f"[{query}] cards renderizaram (selector: {sel})")
            break
        except Exception:
            continue

    # Persiste HTML cru pra reprocessamento futuro
    html = await page.content()
    repo.insert_raw_page(SOURCE, url, html)

    # Pega todos os links de projeto via URL pattern (mais robusto a mudanca de classes)
    project_links = await page.eval_on_selector_all(
        "a[href*='/jobs/']",
        """elements => Array.from(new Set(elements.map(e => e.href)))
            .filter(h => h.match(/\\/jobs\\/[\\w-]+/))""",
    )

    log.info(f"[{query}] {len(project_links)} links de projeto encontrados")

    projetos = []
    for href in project_links[:20]:  # limita 20 por query
        # Tenta extrair titulo e descricao do card sem visitar a pagina detalhada
        try:
            card = await page.locator(f"a[href='{href}']").first.locator("xpath=ancestor::*[self::article or self::div][1]").element_handle()
            if not card:
                continue
            titulo_node = await card.query_selector("h1, h2, h3, .title, [class*='title']")
            titulo = (await titulo_node.text_content() or "").strip() if titulo_node else ""
            desc_node = await card.query_selector("[class*='description'], p")
            descricao = (await desc_node.text_content() or "").strip() if desc_node else ""

            if titulo:
                projetos.append({
                    "titulo": titulo,
                    "descricao": descricao,
                    "url": urljoin(BASE_URL, href),
                })
        except Exception as e:
            log.debug(f"erro extraindo card de {href}: {e}")
            continue

    log.info(f"[{query}] extraidos {len(projetos)} projetos com titulo")
    return projetos


async def processar_projeto(projeto: dict) -> tuple[bool, bool]:
    texto = f"{projeto['titulo']} {projeto['descricao']}"
    s = scoring_engine.calcular(source=SOURCE, texto_para_analisar=texto)
    if s.score < 20:
        return True, False

    fingerprint = repo.make_fingerprint(
        telefone=None, email=None,
        nome=projeto["titulo"], source=SOURCE,
    )
    lead = {
        "fingerprint": fingerprint,
        "source": SOURCE,
        "source_url": projeto["url"],
        "nome": None,
        "nicho": s.nicho,
        "cidade_tag": s.cidade_tag,
        "score_temperatura": s.score,
        "score_breakdown": s.breakdown,
        "raw_payload": {
            "titulo": projeto["titulo"],
            "descricao": projeto["descricao"][:1000],
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
                "source_url": projeto["url"],
                "boost": x.boost,
            } for x in s.intent_signals],
        )
    log.info(
        f"  {'NOVO' if is_new else 'upd'} id={lead_id} score={s.score} "
        f"cidade={s.cidade_tag} nicho={s.nicho}"
    )
    return True, is_new


async def main() -> None:
    run_id = repo.start_run(SOURCE, metadata={"queries": QUERIES})
    ok = fail = nuevos = upd = 0
    err = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        try:
            for q in QUERIES:
                try:
                    projetos = await scrape_search(page, q)
                    ok += 1
                    for p in projetos:
                        try:
                            _, is_new = await processar_projeto(p)
                            if is_new:
                                nuevos += 1
                            else:
                                upd += 1
                        except Exception as e:
                            log.exception(f"erro processando {p.get('url')}: {e}")
                            repo.push_dead_letter(p.get("url", "?"), SOURCE, str(e))
                except Exception as e:
                    log.exception(f"falhou query {q}: {e}")
                    fail += 1
                await human_delay(1, 3)
        except Exception as e:
            err = str(e)
            log.exception(e)
        finally:
            await browser.close()
            repo.end_run(
                run_id,
                pages_ok=ok, pages_failed=fail,
                leads_new=nuevos, leads_updated=upd,
                error_summary=err,
            )
            log.info(f"DONE run={run_id} ok={ok} fail={fail} new={nuevos} upd={upd}")


if __name__ == "__main__":
    asyncio.run(main())
