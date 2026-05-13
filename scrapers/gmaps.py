"""
Scraper do Google Maps — varredura larga por nicho + cidade.

Estrategia: pra cada nicho × cidade-alvo, executa uma busca no Google Maps,
scrolla a lista de resultados ate o fim, extrai NOME + TELEFONE + ENDERECO
+ SITE de cada estabelecimento. Telefone publico = lead pronto pra ataque.

Fonte de OURO: o ticker de leads que nao precisa enriquecimento.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import yaml
from playwright.async_api import async_playwright

from db import repo
from scoring import engine as scoring_engine
from scoring.config_loader import CONFIG_DIR
from scrapers.base import human_delay

log = logging.getLogger("scraper.gmaps")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

SOURCE = "gmaps"
BASE_URL = "https://www.google.com/maps/search/"


def _build_queries() -> list[tuple[str, str, str]]:
    """Monta (query, nicho, cidade_tag) pra cada combinacao."""
    cities = yaml.safe_load((CONFIG_DIR / "cities.yaml").read_text(encoding="utf-8"))
    niches = yaml.safe_load((CONFIG_DIR / "niches.yaml").read_text(encoding="utf-8"))

    queries = []
    # Nicho geral × todas as cidades
    for nicho_key, nicho_info in niches["nichos"].items():
        # pega primeira query (mais especifica) pra cada nicho
        nicho_q = nicho_info["queries"][0]
        for city_key, city_info in cities["cidades"].items():
            cidade_nome = city_info["nome_completo"]
            q = f"{nicho_q} em {cidade_nome}"
            queries.append((q, nicho_key, city_key))

    # Nichos regionais (so Sao Luis)
    for nicho_key, nicho_info in (niches.get("nichos_regionais_sao_luis") or {}).items():
        nicho_q = nicho_info["queries"][0]
        queries.append((nicho_q, nicho_key, "sao-luis"))

    return queries


async def _scroll_results(page) -> int:
    """Scrolla o painel de resultados ate o fim. Retorna num cards apos scroll."""
    feed_sel = "div[role='feed']"
    try:
        await page.wait_for_selector(feed_sel, timeout=15000)
    except Exception:
        log.warning("painel de resultados nao apareceu")
        return 0

    for i in range(20):  # max 20 scrolls
        await page.evaluate(
            "() => { const f = document.querySelector(\"div[role='feed']\"); if (f) f.scrollTo(0, f.scrollHeight); }"
        )
        await asyncio.sleep(1.2)
        # Detecta fim ("Você chegou ao final da lista")
        ended = await page.evaluate(
            """() => Array.from(document.querySelectorAll('span,p')).some(
                el => /(chegou ao final|end of the list|fim da lista)/i.test(el.textContent || '')
            )"""
        )
        if ended:
            log.info(f"  scroll terminou na iteracao {i}")
            break

    count = await page.evaluate(
        "() => document.querySelectorAll(\"div[role='feed'] > div > div[role='article'], div[role='feed'] a.hfpxzc\").length"
    )
    return count


async def _extract_cards(page) -> list[dict]:
    """Extrai dados de cada card de empresa na lista."""
    cards = await page.evaluate(
        """() => {
            const articles = Array.from(document.querySelectorAll("a.hfpxzc"));
            return articles.map(a => {
                const card = a.closest("div[role='article']") || a.parentElement;
                const name = a.getAttribute('aria-label') || '';
                // Telefone aparece em span com texto que combina com formato BR
                const allSpans = card ? Array.from(card.querySelectorAll('span')) : [];
                let telefone = '';
                let endereco = '';
                let rating = '';
                let tipo = '';
                for (const sp of allSpans) {
                    const t = (sp.textContent || '').trim();
                    if (!telefone && /^[\\(]?\\d{2}[\\)]?[\\s\\-]?\\d{4,5}[\\s\\-]?\\d{4}/.test(t)) {
                        telefone = t;
                    } else if (!rating && /^\\d[\\.,]\\d/.test(t) && t.length < 8) {
                        rating = t;
                    }
                }
                // Endereco e tipo geralmente sao primeiras 2 linhas apos nome
                const lines = card ? (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean) : [];
                if (lines.length > 1) {
                    tipo = lines[1] || '';
                }
                for (const l of lines) {
                    if (/(R\\.|Av\\.|Rua|Avenida|Travessa|Praça|Rod\\.|Estrada)/i.test(l)) {
                        endereco = l;
                        break;
                    }
                }
                return {
                    nome: name,
                    href: a.getAttribute('href') || '',
                    telefone: telefone,
                    rating: rating,
                    tipo: tipo,
                    endereco: endereco,
                };
            }).filter(c => c.nome);
        }"""
    )
    return cards


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10 or len(digits) > 13:
        return None
    if len(digits) == 10 or len(digits) == 11:
        digits = "55" + digits
    return f"+{digits}"


async def _processar_estabelecimento(card: dict, nicho_key: str, cidade_tag: str, query: str) -> tuple[bool, bool]:
    """Salva 1 lead. Retorna (foi_processado, eh_novo)."""
    tel = _normalize_phone(card.get("telefone", ""))
    nome = card.get("nome", "").strip()
    if not nome:
        return False, False

    # Score
    s = scoring_engine.calcular(
        source=SOURCE,
        texto_para_analisar=f"{nome} {card.get('tipo', '')} {card.get('endereco', '')} {query}",
        cidade_hint=cidade_tag,
        nicho_hint=nicho_key,
        has_telefone=bool(tel),
    )

    fp = repo.make_fingerprint(telefone=tel, email=None, nome=nome, source=SOURCE)
    lead = {
        "fingerprint": fp,
        "source": SOURCE,
        "source_url": "https://www.google.com/maps/place/" + quote(nome),
        "nome": nome,
        "telefone": tel,
        "nicho": nicho_key,
        "cidade_tag": cidade_tag,
        "score_temperatura": s.score,
        "score_breakdown": s.breakdown,
        "raw_payload": {
            "gmaps": {
                "rating": card.get("rating"),
                "tipo": card.get("tipo"),
                "endereco": card.get("endereco"),
                "query": query,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }
    lead_id, is_new = repo.upsert_lead(lead)
    log.info(
        f"  {'NOVO' if is_new else 'upd'} #{lead_id} '{nome[:50]}' "
        f"tel={tel} score={s.score} cidade={cidade_tag} nicho={nicho_key}"
    )
    return True, is_new


async def buscar(page, query: str, nicho_key: str, cidade_tag: str) -> tuple[int, int]:
    url = BASE_URL + quote(query)
    log.info(f"[{cidade_tag}/{nicho_key}] GET {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log.warning(f"timeout: {e}")
        return 0, 0

    await asyncio.sleep(3)
    count = await _scroll_results(page)
    log.info(f"[{cidade_tag}/{nicho_key}] {count} estabelecimentos na lista")

    cards = await _extract_cards(page)
    log.info(f"[{cidade_tag}/{nicho_key}] {len(cards)} cards extraidos")

    # Salva pagina cru
    html = await page.content()
    repo.insert_raw_page(SOURCE, url, html[:500_000])  # limita 500KB

    novos = 0
    atualizados = 0
    for card in cards:
        try:
            _, is_new = await _processar_estabelecimento(card, nicho_key, cidade_tag, query)
            if is_new:
                novos += 1
            else:
                atualizados += 1
        except Exception as e:
            log.exception(f"erro processando {card.get('nome')}: {e}")

    return novos, atualizados


async def main() -> None:
    queries = _build_queries()
    log.info(f"== Iniciando GMaps scrape: {len(queries)} queries ==")

    run_id = repo.start_run(SOURCE, metadata={"total_queries": len(queries)})
    ok = fail = leads_new = leads_upd = 0
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
            viewport={"width": 1366, "height": 900},
            timezone_id="America/Sao_Paulo",
        )
        page = await context.new_page()

        try:
            for q, nicho_key, cidade_tag in queries:
                try:
                    novos, atualizados = await buscar(page, q, nicho_key, cidade_tag)
                    leads_new += novos
                    leads_upd += atualizados
                    ok += 1
                except Exception as e:
                    log.exception(f"falhou query '{q}': {e}")
                    fail += 1
                await human_delay(3, 8)  # delay maior contra rate limit Google
        except Exception as e:
            err = str(e)
            log.exception(e)
        finally:
            await browser.close()
            repo.end_run(
                run_id,
                pages_ok=ok, pages_failed=fail,
                leads_new=leads_new, leads_updated=leads_upd,
                error_summary=err,
            )
            log.info(
                f"== DONE run={run_id} queries_ok={ok} queries_fail={fail} "
                f"leads_new={leads_new} leads_upd={leads_upd} =="
            )


if __name__ == "__main__":
    asyncio.run(main())
