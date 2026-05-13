"""
Runner: aprofunda leads via OSINT.

Pega leads que:
  - Tem telefone (vale a pena gastar OSINT em quem ja podemos contatar)
  - NAO foram aprofundados nos ultimos 7 dias

Ordena segundo prioridade combinada do Targino:
  1. Maior score atual (confirma o que ja parece quente)
  2. Sao Luis primeiro (geografia priorizada)
  3. Score mais baixo (acha hidden gems)
  4. Resto
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from db import repo
from db.connection import get_conn
from enrichers.deep_osint import aprofundar_lead

log = logging.getLogger("enricher.deep_osint_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


SQL_PEGAR_LEADS = """
    SELECT id, nome, telefone, cidade_tag, cidade, score_temperatura, raw_payload
    FROM leads
    WHERE telefone IS NOT NULL
      AND (last_deep_dive_at IS NULL OR last_deep_dive_at < NOW() - INTERVAL '7 days')
    ORDER BY
      -- Prioridade combinada:
      -- 1. Score alto (>=50) primeiro
      -- 2. Sao Luis depois
      -- 3. Score baixo (25-50)
      -- 4. Resto
      CASE
        WHEN score_temperatura >= 50 THEN 1
        WHEN cidade_tag = 'sao-luis' THEN 2
        WHEN score_temperatura BETWEEN 25 AND 49 THEN 3
        ELSE 4
      END,
      score_temperatura DESC
    LIMIT %s
"""

SQL_UPDATE_LEAD = """
    UPDATE leads SET
        score_temperatura  = LEAST(100, score_temperatura + %(boost)s),
        score_breakdown    = score_breakdown || %(breakdown_extra)s::jsonb,
        raw_payload        = raw_payload || %(payload_extra)s::jsonb,
        last_deep_dive_at  = NOW()
    WHERE id = %(id)s
"""

SQL_INSERT_INTEREST = """
    INSERT INTO intent_signals (lead_id, categoria, palavra_chave, trecho_texto, source_url, boost)
    VALUES (%s, %s, %s, %s, %s, %s)
"""

CIDADE_NOME_FULL = {
    "sao-luis": "São Luís MA",
    "sao-paulo": "São Paulo SP",
    "curitiba": "Curitiba PR",
    "rio-de-janeiro": "Rio de Janeiro RJ",
}


def _extract_urls_from_payload(payload) -> dict:
    """Pega site, instagram, facebook que ja estao no raw_payload."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    deep = payload.get("deep_enrich") if isinstance(payload, dict) else None
    if not deep:
        return {}
    return {
        "site": deep.get("site") or "",
        "instagram": deep.get("instagram") or "",
        "facebook": deep.get("facebook") or "",
    }


async def main(limit: int = 1000) -> None:
    log.info(f"== Deep OSINT (max {limit} leads) ==")

    with get_conn() as c, c.cursor() as cur:
        cur.execute(SQL_PEGAR_LEADS, (limit,))
        cols = [d.name for d in cur.description]
        leads = [dict(zip(cols, row)) for row in cur.fetchall()]

    log.info(f"  {len(leads)} leads pra aprofundar")
    if not leads:
        return

    # Registra run pro dashboard mostrar barra de progresso
    run_id = repo.start_run("deep_osint", metadata={"total_leads": len(leads), "limit": limit})

    aprofundados = 0
    com_sinais = 0
    total_sinais = 0

    for i, lead in enumerate(leads, 1):
        log.info(
            f"== [{i}/{len(leads)}] Lead #{lead['id']} score={lead['score_temperatura']} "
            f"cidade={lead['cidade_tag']}: {lead['nome'][:50]}"
        )

        # Extrai URLs ja conhecidas (site/IG/FB) do raw_payload
        urls = _extract_urls_from_payload(lead.get("raw_payload"))
        cidade = lead.get("cidade") or CIDADE_NOME_FULL.get(lead.get("cidade_tag") or "", "")

        try:
            result = await aprofundar_lead(
                nome=lead["nome"],
                cidade=cidade,
                site_url=urls.get("site", ""),
                instagram_url=urls.get("instagram", ""),
                facebook_url=urls.get("facebook", ""),
            )
        except Exception as e:
            log.exception(f"falhou aprofundar lead {lead['id']}: {e}")
            continue

        # Salva sinais no banco
        with get_conn() as c, c.cursor() as cur:
            for s in result.sinais:
                cur.execute(
                    SQL_INSERT_INTEREST,
                    (
                        lead["id"],
                        f"interest_{s.categoria}",
                        s.palavra_chave,
                        s.trecho[:500],
                        None,
                        s.boost,
                    ),
                )

            # Atualiza lead com boost no score
            categorias_detectadas = sorted({s.categoria for s in result.sinais})
            breakdown_extra = {f"interest_{c}": result.boost_score for c in categorias_detectadas} if categorias_detectadas else {"deep_osint_sem_sinais": 0}
            payload_extra = {
                "deep_osint": {
                    "fontes_consultadas": result.fontes_consultadas,
                    "n_sinais": len(result.sinais),
                    "boost": result.boost_score,
                    "categorias": categorias_detectadas,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
            }

            cur.execute(SQL_UPDATE_LEAD, {
                "id": lead["id"],
                "boost": result.boost_score,
                "breakdown_extra": json.dumps(breakdown_extra),
                "payload_extra": json.dumps(payload_extra),
            })

        aprofundados += 1
        if result.sinais:
            com_sinais += 1
            total_sinais += len(result.sinais)
            log.info(
                f"  ✓ +{result.boost_score} score, sinais: {[s.categoria for s in result.sinais]}"
            )
        else:
            log.info(f"  - sem sinais de interesse detectados")

        # Atualiza progresso (pages_ok = quantos processados) pra dashboard
        with get_conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE scrape_runs SET pages_ok = %s, leads_new = %s, leads_updated = %s "
                "WHERE id = %s",
                (i, com_sinais, aprofundados - com_sinais, run_id),
            )

        # Cortesia: nao martelar fontes externas
        await asyncio.sleep(1)

    # Marca como concluido
    repo.end_run(
        run_id,
        pages_ok=aprofundados,
        pages_failed=len(leads) - aprofundados,
        leads_new=com_sinais,
        leads_updated=total_sinais,
    )

    log.info(
        f"== FIM: {aprofundados} aprofundados, {com_sinais} com sinais, "
        f"{total_sinais} sinais totais detectados =="
    )


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    asyncio.run(main(limit))
