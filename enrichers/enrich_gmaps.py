"""
Runner: enriquece leads GMaps existentes que estao sem telefone.

Usa o pipeline gmaps_deep (cascata de 6 tecnicas).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from playwright.async_api import async_playwright

from db import repo
from db.connection import get_conn
from enrichers.gmaps_deep import enriquecer_gmaps_lead

log = logging.getLogger("enricher.gmaps_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


SQL_PEGAR_LEADS = """
    SELECT id, nome, cidade_tag, raw_payload
    FROM leads
    WHERE source = 'gmaps'
      AND telefone IS NULL
      AND nome IS NOT NULL
    ORDER BY score_temperatura DESC, id ASC
    LIMIT %s
"""


SQL_UPDATE = """
    UPDATE leads SET
        telefone          = COALESCE(telefone, %(telefone)s),
        email             = COALESCE(email, %(email)s),
        score_temperatura = LEAST(100, score_temperatura + %(boost)s),
        score_breakdown   = score_breakdown || %(breakdown_extra)s::jsonb,
        raw_payload       = raw_payload || %(payload_extra)s::jsonb,
        last_seen_at      = NOW()
    WHERE id = %(id)s
"""

CIDADE_NOME = {
    "sao-luis": "São Luís MA",
    "sao-paulo": "São Paulo SP",
    "curitiba": "Curitiba PR",
    "rio-de-janeiro": "Rio de Janeiro RJ",
}


async def main(limit: int = 50) -> None:
    log.info(f"== Enrich profundo de leads GMaps (limit={limit}) ==")

    with get_conn() as c, c.cursor() as cur:
        cur.execute(SQL_PEGAR_LEADS, (limit,))
        cols = [d.name for d in cur.description]
        leads = [dict(zip(cols, row)) for row in cur.fetchall()]

    log.info(f"  {len(leads)} leads pra enriquecer")
    if not leads:
        return

    # Registra run pro dashboard mostrar barra de progresso
    run_id = repo.start_run("enrich_gmaps", metadata={"total_leads": len(leads), "limit": limit})

    enriquecidos_com_tel = 0
    enriquecidos_parcial = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="pt-BR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            timezone_id="America/Sao_Paulo",
        )
        page = await ctx.new_page()

        for i, lead in enumerate(leads, 1):
            log.info(f"== [{i}/{len(leads)}] Lead #{lead['id']}: {lead['nome'][:60]}")
            cidade = CIDADE_NOME.get(lead.get("cidade_tag") or "", "Brasil")

            try:
                r = await enriquecer_gmaps_lead(page, lead["nome"], cidade)
            except Exception as e:
                log.exception(f"falhou: {e}")
                continue

            boost = 0
            breakdown_extra = {}
            payload_extra = {}

            if r.telefone:
                boost += 25
                breakdown_extra["deep_telefone"] = 25
                enriquecidos_com_tel += 1
            if r.email:
                boost += 8
            if r.site or r.instagram_url or r.facebook_url:
                payload_extra["deep_enrich"] = {
                    "site": r.site,
                    "instagram": r.instagram_url,
                    "facebook": r.facebook_url,
                    "endereco": r.endereco_completo,
                    "fontes": r.fontes_usadas,
                }
                if not r.telefone:
                    enriquecidos_parcial += 1

            # Salva perfis sociais
            with get_conn() as c, c.cursor() as cur:
                if r.instagram_url:
                    cur.execute(
                        "INSERT INTO social_profiles (lead_id, plataforma, url, fonte, confianca) "
                        "VALUES (%s, 'instagram', %s, 'gmaps_deep', 80) ON CONFLICT DO NOTHING",
                        (lead["id"], r.instagram_url),
                    )
                if r.facebook_url:
                    cur.execute(
                        "INSERT INTO social_profiles (lead_id, plataforma, url, fonte, confianca) "
                        "VALUES (%s, 'facebook', %s, 'gmaps_deep', 80) ON CONFLICT DO NOTHING",
                        (lead["id"], r.facebook_url),
                    )
                if r.site:
                    cur.execute(
                        "INSERT INTO social_profiles (lead_id, plataforma, url, fonte, confianca) "
                        "VALUES (%s, 'site', %s, 'gmaps_deep', 90) ON CONFLICT DO NOTHING",
                        (lead["id"], r.site),
                    )

                cur.execute(SQL_UPDATE, {
                    "id": lead["id"],
                    "telefone": r.telefone or None,
                    "email": r.email or None,
                    "boost": boost,
                    "breakdown_extra": json.dumps(breakdown_extra),
                    "payload_extra": json.dumps(payload_extra),
                })

            # Atualiza progresso a cada lead
            with get_conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE scrape_runs SET pages_ok = %s, leads_new = %s, leads_updated = %s "
                    "WHERE id = %s",
                    (i, enriquecidos_com_tel, enriquecidos_parcial, run_id),
                )

            await asyncio.sleep(2)

        await browser.close()

    # Marca como concluido
    repo.end_run(
        run_id,
        pages_ok=len(leads),
        pages_failed=0,
        leads_new=enriquecidos_com_tel,
        leads_updated=enriquecidos_parcial,
    )

    log.info(
        f"== FIM: {enriquecidos_com_tel} com telefone, "
        f"{enriquecidos_parcial} parcial (site/IG/FB), "
        f"{len(leads) - enriquecidos_com_tel - enriquecidos_parcial} sem nada =="
    )


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    asyncio.run(main(limit))
