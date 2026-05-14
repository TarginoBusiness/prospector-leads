"""
One-shot: reseta last_deep_dive_at dos leads que foram "aprofundados"
pelo deep_osint v2.0 (bugado, DDG bloqueado, 0 sinais) pra que sejam
reprocessados pela v2.1 corrigida.

Criterio: tem last_deep_dive_at setado mas raw_payload.deep_osint_v2
mostra fontes_consultadas <= 1 (sinal de que DDG estava bloqueado).
"""
from db.connection import get_conn


def main() -> None:
    sql = """
        UPDATE leads
        SET last_deep_dive_at = NULL
        WHERE last_deep_dive_at IS NOT NULL
          AND (
            raw_payload -> 'deep_osint_v2' IS NULL
            OR jsonb_array_length(
                COALESCE(raw_payload -> 'deep_osint_v2' -> 'fontes_consultadas', '[]'::jsonb)
            ) <= 1
            OR (raw_payload -> 'deep_osint_v2' ->> 'n_sinais')::int = 0
          )
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        print(f"[reset] {cur.rowcount} leads resetados pra reprocessamento pelo deep_osint v2.1")

        # Estatistica
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE last_deep_dive_at IS NULL AND telefone IS NOT NULL) AS pendentes,
                COUNT(*) FILTER (WHERE last_deep_dive_at IS NOT NULL) AS ja_processados
            FROM leads
        """)
        pend, proc = cur.fetchone()
        print(f"[reset] {pend} leads com telefone pendentes de OSINT, {proc} ja processados")


if __name__ == "__main__":
    main()
