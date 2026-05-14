"""
Diagnostico do deep_osint_v2: verifica se a v2.2 esta REALMENTE visitando
paginas (fontes_consultadas) ou se esta rodando "no vazio" (0 fontes -> 0 sinais).
"""
from __future__ import annotations

from db.connection import get_conn


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        # Quantos leads tem deep_enrich (insumo da v2.2: site/ig/fb conhecidos)
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE raw_payload ? 'deep_enrich')                       AS com_deep_enrich,
                COUNT(*) FILTER (WHERE raw_payload #>> '{deep_enrich,site}'      <> '')    AS com_site,
                COUNT(*) FILTER (WHERE raw_payload #>> '{deep_enrich,instagram}' <> '')    AS com_ig,
                COUNT(*) FILTER (WHERE raw_payload #>> '{deep_enrich,facebook}'  <> '')    AS com_fb,
                COUNT(*)                                                                  AS total
            FROM leads
        """)
        cols = [d.name for d in cur.description]
        print("[diag] INSUMO (o que a v2.2 tem pra trabalhar):")
        for k, v in zip(cols, cur.fetchone()):
            print(f"  {v:6}  {k}")

        # Leads processados pela v2.2 (tem o bloco deep_osint_v2 no payload)
        cur.execute("""
            SELECT
                COUNT(*)                                                                          AS processados,
                COUNT(*) FILTER (WHERE jsonb_array_length(raw_payload #> '{deep_osint_v2,fontes_consultadas}') > 0) AS com_fontes,
                COALESCE(SUM((raw_payload #>> '{deep_osint_v2,n_sinais}')::int), 0)                 AS total_sinais,
                COALESCE(AVG(jsonb_array_length(raw_payload #> '{deep_osint_v2,paginas_visitadas}')), 0)::numeric(10,2) AS media_paginas
            FROM leads
            WHERE raw_payload ? 'deep_osint_v2'
        """)
        cols = [d.name for d in cur.description]
        print()
        print("[diag] RESULTADO v2.2 (leads ja processados):")
        for k, v in zip(cols, cur.fetchone()):
            print(f"  {v:>10}  {k}")

        # Amostra: 10 leads processados, com contagem de fontes e paginas
        cur.execute("""
            SELECT id, nome,
                   raw_payload #> '{deep_osint_v2,fontes_consultadas}'  AS fontes,
                   raw_payload #>> '{deep_osint_v2,n_sinais}'           AS n_sinais,
                   raw_payload #> '{deep_osint_v2,paginas_visitadas}'   AS paginas
            FROM leads
            WHERE raw_payload ? 'deep_osint_v2'
            ORDER BY last_deep_dive_at DESC
            LIMIT 12
        """)
        print()
        print("[diag] AMOSTRA (12 mais recentes):")
        for lid, nome, fontes, n_sinais, paginas in cur.fetchall():
            np = len(paginas) if paginas else 0
            print(f"  #{lid} sinais={n_sinais} fontes={fontes} paginas_visitadas={np}  {(nome or '')[:40]}")

        # Sinais interest atuais
        cur.execute("SELECT COUNT(*) FROM intent_signals WHERE categoria LIKE 'interest_%'")
        print()
        print(f"[diag] intent_signals 'interest_*' no banco AGORA: {cur.fetchone()[0]}")


if __name__ == "__main__":
    main()
