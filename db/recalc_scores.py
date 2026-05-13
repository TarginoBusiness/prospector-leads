"""
Aplica a NOVA regra de scoring aos leads existentes.

REGRA: lead sem telefone = lead frio (cap 25%).
Razao: sem forma de contato direto, nao adianta saber que o lead e
"intencao quente em sao luis" — voce nao tem como mandar mensagem.
"""
from __future__ import annotations

import logging

from db.connection import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("db.recalc")


SQL_BEFORE = """
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE telefone IS NULL) AS sem_tel,
        COUNT(*) FILTER (WHERE telefone IS NULL AND score_temperatura > 25) AS sem_tel_score_alto,
        AVG(score_temperatura)::int AS score_medio
    FROM leads
"""

SQL_RECALC = """
    UPDATE leads
    SET score_temperatura = 25,
        score_breakdown = score_breakdown || jsonb_build_object('__cap_sem_telefone__', -1)
    WHERE telefone IS NULL
      AND score_temperatura > 25
"""

SQL_AFTER = """
    SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE score_temperatura >= 60) AS quentes,
        AVG(score_temperatura)::int AS score_medio
    FROM leads
"""


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute(SQL_BEFORE)
        cols = [d.name for d in cur.description]
        before = dict(zip(cols, cur.fetchone()))
        log.info(f"ANTES: {before}")

        cur.execute(SQL_RECALC)
        log.info(f"UPDATE atingiu {cur.rowcount} leads sem telefone (cap 25%)")

        cur.execute(SQL_AFTER)
        after = dict(zip([d.name for d in cur.description], cur.fetchone()))
        log.info(f"DEPOIS: {after}")


if __name__ == "__main__":
    main()
