"""
One-shot: remove sinais DUPLICADOS (mesma frase repetida).

A mesma mencao aparecia varias vezes — paginas diferentes do site repetem
o mesmo rodape, e re-runs re-inseriam tudo. Mantem o registro de menor id
de cada grupo (lead_id, categoria, palavra_chave, trecho_texto) e apaga o
resto. O detector e o runner ja foram corrigidos pra nao gerar mais dup.
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

        cur.execute("""
            DELETE FROM intent_signals
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY lead_id, categoria, palavra_chave,
                                            COALESCE(LOWER(TRIM(trecho_texto)), '')
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

    log.info(f"[dedup] {antes} sinais -> {depois} ({removidos} duplicados removidos)")


if __name__ == "__main__":
    main()
