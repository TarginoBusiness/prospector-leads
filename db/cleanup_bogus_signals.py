"""
One-shot: limpa os sinais de interesse FALSOS gerados pelo detector bugado.

O detector antigo usava substring matching ('llm' in texto casava dentro
de 'smallman', 'rpa' dentro de 'Serpa', etc). Isso gerou milhares de
interest_signals falsos. Este script:

1. DELETA todos os intent_signals com categoria 'interest_*' (os falsos)
2. Reseta last_deep_dive_at = NULL (pra reprocessar com detector corrigido)
3. Recalcula score_temperatura (remove os boosts dos sinais falsos)
4. Re-aplica cap 25% pra leads sem telefone

Os intent_signals SEM prefixo 'interest_' (vindos dos scrapers Workana/
99Freelas) sao mantidos — esses usaram o intent_detector que tambem foi
corrigido, mas como sao frases longas tinham baixo risco de false positive.
"""
from __future__ import annotations

import logging

from db.connection import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("db.cleanup_bogus")


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        # 1. Conta antes
        cur.execute("SELECT COUNT(*) FROM intent_signals WHERE categoria LIKE 'interest_%'")
        n_interest = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM intent_signals WHERE categoria NOT LIKE 'interest_%'")
        n_intent = cur.fetchone()[0]
        log.info(f"ANTES: {n_interest} sinais 'interest_*' (falsos) + {n_intent} sinais de intent (mantidos)")

        # 2. DELETA os sinais de interesse falsos
        cur.execute("DELETE FROM intent_signals WHERE categoria LIKE 'interest_%'")
        log.info(f"  Deletados {cur.rowcount} sinais de interesse falsos")

        # 3. Deleta social_profiles que vieram do deep_osint_v2 bugado
        cur.execute("DELETE FROM social_profiles WHERE fonte IN ('deep_osint_v2', 'deep_osint')")
        log.info(f"  Deletados {cur.rowcount} perfis sociais do deep_osint bugado")

        # 4. Reseta last_deep_dive_at + limpa o deep_osint_v2 do raw_payload
        cur.execute("""
            UPDATE leads
            SET last_deep_dive_at = NULL,
                raw_payload = raw_payload - 'deep_osint_v2' - 'deep_osint'
            WHERE last_deep_dive_at IS NOT NULL
               OR raw_payload ? 'deep_osint_v2'
               OR raw_payload ? 'deep_osint'
        """)
        log.info(f"  Resetados {cur.rowcount} leads (last_deep_dive_at + raw_payload limpo)")

        # 5. Limpa o score_breakdown dos boosts de interest falsos
        cur.execute("""
            UPDATE leads
            SET score_breakdown = (
                SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb)
                FROM jsonb_each(score_breakdown)
                WHERE NOT (key LIKE 'interest_%')
            )
            WHERE score_breakdown IS NOT NULL
        """)
        log.info(f"  Limpos {cur.rowcount} score_breakdown (boosts de interest removidos)")

    # 6. Recalcula score_temperatura do zero (base + intent real, sem interest falso)
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""
            WITH base AS (
                SELECT
                    l.id,
                    l.telefone,
                    COALESCE((
                        SELECT SUM((value)::int)
                        FROM jsonb_each_text(l.score_breakdown)
                        WHERE NOT starts_with(key, '__')
                          AND (value)::int >= 0
                    ), 0) AS base_score
                FROM leads l
            )
            UPDATE leads
            SET score_temperatura = CASE
                WHEN b.telefone IS NULL THEN LEAST(25, GREATEST(0, b.base_score))
                ELSE GREATEST(0, b.base_score)  -- score ILIMITADO p/ leads com telefone
            END
            FROM base b
            WHERE leads.id = b.id
        """)
        log.info(f"  Recalculados {cur.rowcount} scores (sem os boosts falsos)")

        # Stats finais
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE telefone IS NOT NULL) AS com_tel,
                   COUNT(*) FILTER (WHERE last_deep_dive_at IS NULL AND telefone IS NOT NULL) AS pendentes_osint,
                   AVG(score_temperatura)::int AS score_medio
            FROM leads
        """)
        cols = [d.name for d in cur.description]
        stats = dict(zip(cols, cur.fetchone()))
        log.info(f"  DEPOIS: {stats}")
        log.info("  ✅ Banco limpo. Pode rodar deep_osint v2.1 com detector corrigido.")


if __name__ == "__main__":
    main()
