"""
Recalcula score_temperatura aplicando os NOVOS pesos do interest_keywords.yaml.

Pra cada lead, soma:
  - score base (do score_breakdown)
  - boost por cada categoria distinta de interest_signals (usando NOVOS pesos)

Substitui o score_temperatura pelo valor recalculado.
"""
from __future__ import annotations

import logging

import yaml

from db.connection import get_conn
from scoring.config_loader import CONFIG_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("db.recalc_new_weights")


def main() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "interest_keywords.yaml").read_text(encoding="utf-8"))
    boosts_novos = {f"interest_{cat}": int(info["boost"]) for cat, info in cfg["categorias"].items()}
    teto = int(cfg.get("teto_cumulativo", 100))
    log.info(f"Novos boosts: {boosts_novos} (teto: {teto})")

    with get_conn() as c, c.cursor() as cur:
        # Pra cada lead, pega o "base" do score_breakdown (sem os boosts de interest)
        # e re-aplica os novos boosts de interest baseado nas categorias distintas detectadas
        cur.execute("""
            WITH base_scores AS (
                SELECT
                    l.id AS lead_id,
                    -- soma de TODOS os componentes do breakdown que NAO sao interest_*
                    COALESCE((
                        SELECT SUM((value)::int)
                        FROM jsonb_each_text(l.score_breakdown)
                        WHERE NOT (key LIKE 'interest_%')
                          AND key NOT LIKE '__%'
                          AND (value)::int >= 0
                    ), 0) AS base_score
                FROM leads l
            ),
            interest_per_lead AS (
                SELECT
                    lead_id,
                    array_agg(DISTINCT categoria) AS categorias
                FROM intent_signals
                WHERE categoria LIKE 'interest_%'
                GROUP BY lead_id
            )
            SELECT b.lead_id, b.base_score, COALESCE(i.categorias, ARRAY[]::text[]) AS cats
            FROM base_scores b
            LEFT JOIN interest_per_lead i ON i.lead_id = b.lead_id
        """)
        rows = cur.fetchall()
        log.info(f"Processando {len(rows)} leads...")

    atualizados = 0
    with get_conn() as c, c.cursor() as cur:
        for lead_id, base_score, categorias in rows:
            # Aplica boost por categoria distinta
            boost_total = 0
            for cat in categorias:
                if cat in boosts_novos:
                    boost_total += boosts_novos[cat]
            boost_total = min(boost_total, teto)

            novo_score = base_score + boost_total
            # Cap em 100 final, mas se nao tem telefone ainda capa em 25
            cur.execute("SELECT telefone FROM leads WHERE id = %s", (lead_id,))
            tel = cur.fetchone()[0]
            if not tel:
                novo_score = min(novo_score, 25)
            else:
                novo_score = max(0, min(100, novo_score))

            cur.execute("UPDATE leads SET score_temperatura = %s WHERE id = %s", (novo_score, lead_id))
            atualizados += 1

    log.info(f"Recalculados {atualizados} leads com os novos pesos")


if __name__ == "__main__":
    main()
