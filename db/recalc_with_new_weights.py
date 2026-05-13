"""
Recalcula score_temperatura aplicando os NOVOS pesos do interest_keywords.yaml.

VERSAO RAPIDA: 1 UPDATE SQL inteiro em vez de 1915 queries individuais.
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
    boosts = {f"interest_{cat}": int(info["boost"]) for cat, info in cfg["categorias"].items()}
    teto = int(cfg.get("teto_cumulativo", 100))
    log.info(f"Novos boosts: {boosts} (teto: {teto})")

    # Constroi clausula CASE pro boost
    case_clauses = "\n            ".join(
        f"WHEN '{cat}' THEN {bst}" for cat, bst in boosts.items()
    )

    sql = f"""
    WITH base AS (
        SELECT
            l.id,
            l.telefone,
            COALESCE((
                SELECT SUM((value)::int)
                FROM jsonb_each_text(l.score_breakdown)
                WHERE NOT (key LIKE 'interest_%%')
                  AND NOT (key LIKE '__%%')
                  AND (value)::int >= 0
            ), 0) AS base_score
        FROM leads l
    ),
    interest_boost AS (
        SELECT
            i.lead_id,
            LEAST({teto}, COALESCE(SUM(CASE i.categoria
                {case_clauses}
                ELSE 0
            END), 0)) AS total_boost
        FROM (
            SELECT DISTINCT lead_id, categoria
            FROM intent_signals
            WHERE categoria LIKE 'interest_%%'
        ) i
        GROUP BY i.lead_id
    ),
    novo_score AS (
        SELECT
            b.id,
            CASE
                WHEN b.telefone IS NULL
                    THEN LEAST(25, GREATEST(0, b.base_score + COALESCE(ib.total_boost, 0)))
                ELSE LEAST(100, GREATEST(0, b.base_score + COALESCE(ib.total_boost, 0)))
            END AS score
        FROM base b
        LEFT JOIN interest_boost ib ON ib.lead_id = b.id
    )
    UPDATE leads
    SET score_temperatura = ns.score
    FROM novo_score ns
    WHERE leads.id = ns.id
      AND leads.score_temperatura IS DISTINCT FROM ns.score
    """

    log.info("Executando UPDATE em massa...")
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        log.info(f"  {cur.rowcount} leads tiveram score recalculado")

    # Estatistica final
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                MIN(score_temperatura) AS min_s,
                MAX(score_temperatura) AS max_s,
                AVG(score_temperatura)::int AS avg_s,
                COUNT(*) FILTER (WHERE score_temperatura >= 75) AS top_75
            FROM leads
        """)
        cols = [d.name for d in cur.description]
        stats = dict(zip(cols, cur.fetchone()))
        log.info(f"  Stats: {stats}")


if __name__ == "__main__":
    main()
