"""
Backfill: conserta o `trecho_texto` dos sinais ANTIGOS.

PROBLEMA:
  Sinais criados antes do fix guardavam o trecho NORMALIZADO (minusculo,
  sem acento). O Chrome Text Fragment do dashboard so destaca/scrolla se
  o texto bater EXATAMENTE com a pagina — entao trecho normalizado nunca
  funciona.

SOLUCAO:
  Pra cada sinal com source_url, re-baixa a pagina, roda o detector de
  novo (que agora recorta o trecho do texto ORIGINAL) e atualiza o
  trecho_texto. Sinal cujo source_url morreu ou que nao casa mais a
  keyword: deixa como esta (sera regenerado no proximo deep dive).
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from selectolax.parser import HTMLParser

from db.connection import get_conn
from enrichers import interest_detector
from scoring import intent as intent_detector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("db.backfill_trechos")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


async def baixar_texto(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, timeout=10.0, follow_redirects=True)
        if r.status_code != 200:
            return ""
        tree = HTMLParser(r.text)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        body = tree.body.text() if tree.body else (tree.text() or "")
        return body[:25000]
    except Exception:
        return ""


def _norm(s: str) -> str:
    return interest_detector._normalize(s or "")


async def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT id, categoria, palavra_chave, source_url
            FROM intent_signals
            WHERE source_url IS NOT NULL AND source_url <> ''
            ORDER BY captured_at DESC
        """)
        rows = cur.fetchall()

    log.info(f"{len(rows)} sinais com source_url pra revisar")
    if not rows:
        return

    corrigidos = 0
    sem_pagina = 0
    sem_match = 0

    async with httpx.AsyncClient(headers=HEADERS, timeout=12.0) as client:
        for sid, categoria, kw, url in rows:
            texto = await baixar_texto(client, url)
            if not texto or len(texto) < 50:
                sem_pagina += 1
                continue

            is_interest = (categoria or "").startswith("interest_")
            detector = interest_detector if is_interest else intent_detector
            sinais, _ = detector.detect(texto)

            # acha o sinal cuja keyword bate com a do registro
            kw_norm = _norm(kw)
            novo_trecho = ""
            for s in sinais:
                if _norm(s.palavra_chave) == kw_norm:
                    novo_trecho = s.trecho
                    break

            if not novo_trecho:
                sem_match += 1
                continue

            with get_conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE intent_signals SET trecho_texto = %s WHERE id = %s",
                    (novo_trecho[:500], sid),
                )
            corrigidos += 1
            await asyncio.sleep(0.3)

    log.info(
        f"== FIM: {corrigidos} trechos corrigidos, "
        f"{sem_pagina} sem pagina (url morta?), {sem_match} keyword nao casou mais =="
    )


if __name__ == "__main__":
    asyncio.run(main())
