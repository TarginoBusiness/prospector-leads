"""
One-shot: colapsa sinais por KEYWORD.

Cada keyword deve ser UM unico sinal. Se a mesma keyword (mesmo lead +
categoria + palavra_chave) aparece varias vezes, mantemos o registro de
menor id, gravamos n_ocorrencias = total do grupo, e apagamos o resto.
Score conta a keyword 1x; a ficha mostra "(Nx)".
"""
from __future__ import annotations

import logging

from db.connection import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("db.dedup_signals")


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM intent_signals")
        antes = cur.fetchone()[0]

        # 1. Grava n_ocorrencias no registro que vai SOBREVIVER (menor id do grupo)
        cur.execute("""
            WITH grupos AS (
                SELECT lead_id, categoria, palavra_chave,
                       MIN(id) AS keep_id,
                       COUNT(*) AS total
                FROM intent_signals
                GROUP BY lead_id, categoria, palavra_chave
            )
            UPDATE intent_signals s
            SET n_ocorrencias = g.total
            FROM grupos g
            WHERE s.id = g.keep_id
        """)
        log.info(f"[dedup] {cur.rowcount} sinais unicos com n_ocorrencias atualizado")

        # 2. Apaga os duplicados (tudo que nao eh o menor id do grupo)
        cur.execute("""
            DELETE FROM intent_signals
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY lead_id, categoria, palavra_chave
                        ORDER BY id
                    ) AS rn
                    FROM intent_signals
                ) t
                WHERE t.rn > 1
            )
        """)
        removidos = cur.rowcount

        cur.execute("SELECT COUNT(*) FROM intent_signals")
        depois = cur.fetchone()[0]

    log.info(f"[dedup] {antes} sinais -> {depois} ({removidos} duplicados de keyword removidos)")


if __name__ == "__main__":
    main()
