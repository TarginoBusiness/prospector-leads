"""
Runner v2 — usa deep_osint_v2 com todas as novas fontes.

Mesma logica de prioridade do v1, mas:
- Salva perfis sociais novos descobertos (TikTok, IG, FB, LinkedIn)
- Salva CNPJ + socios em raw_payload
- Salva news mentions e reclame aqui em raw_payload
- Aplica boost de score baseado em sinais detectados
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

from db import repo
from db.connection import get_conn
from enrichers.deep_osint_v2 import aprofundar_v2

log = logging.getLogger("enricher.deep_osint_v2_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


SQL_PEGAR_LEADS = """
    SELECT id, nome, telefone, cnpj, cidade_tag, cidade, score_temperatura, raw_payload
    FROM leads
    WHERE telefone IS NOT NULL
      AND (last_deep_dive_at IS NULL OR last_deep_dive_at < NOW() - INTERVAL '7 days')
    ORDER BY
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
        cnpj               = COALESCE(cnpj, %(cnpj)s),
        score_breakdown    = score_breakdown || %(breakdown_extra)s::jsonb,
        raw_payload        = raw_payload || %(payload_extra)s::jsonb,
        last_deep_dive_at  = NOW()
    WHERE id = %(id)s
"""

SQL_INSERT_SOCIAL = """
    INSERT INTO social_profiles (lead_id, plataforma, url, fonte, confianca)
    VALUES (%s, %s, %s, 'deep_osint_v2', %s)
    ON CONFLICT DO NOTHING
"""

SQL_INSERT_INTEREST = """
    INSERT INTO intent_signals (lead_id, categoria, palavra_chave, trecho_texto, source_url, boost)
    VALUES (%s, %s, %s, %s, %s, %s)
"""


async def main(limit: int = 1000) -> None:
    log.info(f"== Deep OSINT v2 (max {limit} leads) ==")

    with get_conn() as c, c.cursor() as cur:
        cur.execute(SQL_PEGAR_LEADS, (limit,))
        cols = [d.name for d in cur.description]
        leads = [dict(zip(cols, row)) for row in cur.fetchall()]

    log.info(f"  {len(leads)} leads pra aprofundar")
    if not leads:
        return

    run_id = repo.start_run("deep_osint_v2", metadata={"total_leads": len(leads), "limit": limit})

    com_sinais = 0
    sem_sinais = 0
    cnpj_descobertos = 0
    perfis_novos = 0

    cidade_nome = {
        "sao-luis": "São Luís MA",
        "sao-paulo": "São Paulo SP",
        "curitiba": "Curitiba PR",
        "rio-de-janeiro": "Rio de Janeiro RJ",
    }

    for i, lead in enumerate(leads, 1):
        log.info(
            f"== [{i}/{len(leads)}] Lead #{lead['id']} score={lead['score_temperatura']} "
            f"cidade={lead['cidade_tag']}: {lead['nome'][:50]}"
        )

        cidade_str = lead.get("cidade") or cidade_nome.get(lead.get("cidade_tag") or "", "")

        try:
            r = await aprofundar_v2(
                nome=lead["nome"],
                cidade=cidade_str,
                cidade_tag=lead.get("cidade_tag") or "",
                cnpj=lead.get("cnpj") or "",
                telefone=lead.get("telefone") or "",
                site_url="",  # site ja eh coberto pelo deep_osint v1, aqui focamos em socials
            )
        except Exception as e:
            log.exception(f"falhou aprofundar lead {lead['id']}: {e}")
            continue

        # Salva sinais
        with get_conn() as c, c.cursor() as cur:
            for s in r.sinais:
                cur.execute(
                    SQL_INSERT_INTEREST,
                    (lead["id"], f"interest_{s.categoria}", s.palavra_chave,
                     s.trecho[:500], None, s.boost),
                )

            # Salva perfis sociais descobertos
            new_socials = 0
            if r.instagram_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "instagram", r.instagram_url, 85))
                new_socials += 1
            if r.tiktok_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "tiktok", r.tiktok_url, 85))
                new_socials += 1
            if r.twitter_username:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "twitter",
                                                f"https://twitter.com/{r.twitter_username}", 75))
                new_socials += 1
            if r.linkedin_company_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "linkedin",
                                                r.linkedin_company_url, 90))
                new_socials += 1

            perfis_novos += new_socials

            # Update lead
            categorias = sorted({s.categoria for s in r.sinais})
            breakdown_extra = {f"interest_{c}": r.boost_score for c in categorias} if categorias else {}

            payload_extra = {
                "deep_osint_v2": {
                    "fontes_consultadas": r.fontes_consultadas,
                    "n_sinais": len(r.sinais),
                    "boost": r.boost_score,
                    "categorias": categorias,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "cnpj_data": r.cnpj_data,
                    "reverse_phone": r.reverse_phone_data,
                    "news_mentions": {
                        "n_urls": len(r.news_mentions.get("urls", [])),
                        "urls_top5": r.news_mentions.get("urls", [])[:5],
                    } if r.news_mentions else {},
                    "reclame_aqui": {
                        "n_resultados": r.reclame_aqui.get("n_resultados", 0),
                        "urls_top3": r.reclame_aqui.get("urls", [])[:3],
                    } if r.reclame_aqui else {},
                }
            }

            cur.execute(SQL_UPDATE_LEAD, {
                "id": lead["id"],
                "boost": r.boost_score,
                "cnpj": r.cnpj_data.get("cnpj") if r.cnpj_data else None,
                "breakdown_extra": json.dumps(breakdown_extra),
                "payload_extra": json.dumps(payload_extra),
            })

            if r.cnpj_data and not lead.get("cnpj"):
                cnpj_descobertos += 1

        # Progress update
        if r.sinais:
            com_sinais += 1
        else:
            sem_sinais += 1

        with get_conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE scrape_runs SET pages_ok = %s, leads_new = %s, leads_updated = %s "
                "WHERE id = %s",
                (i, com_sinais, sem_sinais, run_id),
            )

        if r.sinais:
            log.info(
                f"  ✓ +{r.boost_score} score, sinais: {[s.categoria for s in r.sinais]}, "
                f"perfis_novos={new_socials}, cnpj={'SIM' if r.cnpj_data else 'nao'}"
            )

        await asyncio.sleep(1)

    repo.end_run(
        run_id,
        pages_ok=len(leads),
        pages_failed=0,
        leads_new=com_sinais,
        leads_updated=sem_sinais,
    )

    log.info(
        f"== FIM: {com_sinais} c/ sinais, {sem_sinais} s/ sinais, "
        f"{cnpj_descobertos} CNPJs descobertos, {perfis_novos} perfis novos =="
    )


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    asyncio.run(main(limit))
