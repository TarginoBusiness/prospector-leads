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
        -- Subtrai o boost da rodada ANTERIOR e soma o da rodada nova:
        -- assim re-rodar nao acumula falso, e o score eh ILIMITADO
        -- (reflete o peso real das evidencias, nao um 0-100).
        score_temperatura  = GREATEST(0, score_temperatura - %(prev_boost)s + %(new_boost)s),
        cnpj               = COALESCE(cnpj, %(cnpj)s),
        email              = COALESCE(email, %(email_receita)s),
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
    INSERT INTO intent_signals (lead_id, categoria, palavra_chave, trecho_texto, source_url, boost, n_ocorrencias)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
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

        # Extrai URLs JA CONHECIDAS do raw_payload (enrich_gmaps ja descobriu!)
        endereco_gmaps = site_known = ig_known = fb_known = ""
        try:
            rp = lead.get("raw_payload") or {}
            if isinstance(rp, str):
                rp = json.loads(rp)
            deep_enrich = rp.get("deep_enrich") or {}
            gmaps_data = rp.get("gmaps") or {}
            endereco_gmaps = gmaps_data.get("endereco", "") or deep_enrich.get("endereco", "")
            site_known = deep_enrich.get("site", "") or ""
            ig_known = deep_enrich.get("instagram", "") or ""
            fb_known = deep_enrich.get("facebook", "") or ""
        except Exception:
            pass

        try:
            r = await aprofundar_v2(
                nome=lead["nome"],
                cidade=cidade_str,
                cidade_tag=lead.get("cidade_tag") or "",
                cnpj=lead.get("cnpj") or "",
                telefone=lead.get("telefone") or "",
                site_url=site_known,           # USA o que o enrich ja achou
                instagram_url=ig_known,        # idem
                facebook_url=fb_known,         # idem
                endereco_gmaps=endereco_gmaps,
            )
        except Exception as e:
            log.exception(f"falhou aprofundar lead {lead['id']}: {e}")
            continue

        # Salva sinais (com source_url real pra cada um)
        with get_conn() as c, c.cursor() as cur:
            # DEDUP no banco: re-rodar um lead REPÕE os sinais de interesse dele
            # (senão acumula a mesma frase a cada run). Intent dos scrapers fica.
            cur.execute(
                "DELETE FROM intent_signals WHERE lead_id = %s AND categoria LIKE 'interest_%%'",
                (lead["id"],),
            )
            for s in r.sinais:
                cur.execute(
                    SQL_INSERT_INTEREST,
                    (lead["id"], f"interest_{s.categoria}", s.palavra_chave,
                     s.trecho[:500], s.source_url or None, s.boost,
                     getattr(s, "n_ocorrencias", 1)),
                )

            # Salva perfis sociais (só os que temos URL real)
            new_socials = 0
            if r.instagram_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "instagram", r.instagram_url, 85))
                new_socials += 1
            if r.facebook_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "facebook", r.facebook_url, 85))
                new_socials += 1
            if r.tiktok_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "tiktok", r.tiktok_url, 80))
                new_socials += 1
            if r.site_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "site", r.site_url, 90))
                new_socials += 1
            if r.linkedin_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "linkedin", r.linkedin_url, 90))
                new_socials += 1
            if r.linkedin_dono_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "linkedin_dono", r.linkedin_dono_url, 85))
                new_socials += 1
            if r.workana_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "workana", r.workana_url, 80))
                new_socials += 1
            if r.getninjas_url:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "getninjas", r.getninjas_url, 80))
                new_socials += 1
            for vu in r.vaga_urls:
                cur.execute(SQL_INSERT_SOCIAL, (lead["id"], "vaga", vu, 80))
                new_socials += 1

            perfis_novos += new_socials

            # Update lead
            categorias = sorted({s.categoria for s in r.sinais})
            breakdown_extra = {f"interest_{c}": r.boost_score for c in categorias} if categorias else {}

            # email descoberto: 1) Receita  2) colhido do site (/contato, rodape)
            email_descoberto = ""
            if r.cnpj_data:
                ev = (r.cnpj_data.get("email") or "").strip()
                if "@" in ev and "." in ev:
                    email_descoberto = ev
            if not email_descoberto:
                for ev in (r.contatos.get("emails") or []):
                    ev = (ev or "").strip()
                    if "@" in ev and "." in ev:
                        email_descoberto = ev
                        break

            # ===== BOOSTS por dado descoberto (Instagram/email/CNPJ) =====
            # Dados que comprovam que o lead esta "achavel" e pronto pra contato
            # ja sao um sinal de qualidade — score sobe quando temos isso.
            dados_boost = 0
            if r.instagram_url:
                dados_boost += 5
                breakdown_extra["dado_instagram"] = 5
            if email_descoberto or lead.get("email"):
                dados_boost += 10
                breakdown_extra["dado_email"] = 10
            if r.cnpj_data:
                dados_boost += 15
                breakdown_extra["dado_cnpj"] = 15

            payload_extra = {
                "deep_osint_v2": {
                    "fontes_consultadas": r.fontes_consultadas,
                    "n_sinais": len(r.sinais),
                    "boost": r.boost_score,
                    "categorias": categorias,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "cnpj_data": r.cnpj_data,
                    "contatos": r.contatos,
                    "instagram_bio": r.instagram_bio,
                    "dive_boost": r.boost_score + dados_boost,
                    "linkedin_url": r.linkedin_url,
                    "linkedin_dono_url": r.linkedin_dono_url,
                    "workana_url": r.workana_url,
                    "getninjas_url": r.getninjas_url,
                    "vaga_urls": r.vaga_urls[:5],
                    "paginas_visitadas": list(r.textos_por_url.keys())[:10],
                }
            }

            # Boost da rodada ANTERIOR (pra subtrair e nao acumular)
            prev_rp = lead.get("raw_payload") or {}
            if isinstance(prev_rp, str):
                try:
                    prev_rp = json.loads(prev_rp)
                except Exception:
                    prev_rp = {}
            prev_boost = int(((prev_rp.get("deep_osint_v2") or {}).get("dive_boost") or 0))

            cur.execute(SQL_UPDATE_LEAD, {
                "id": lead["id"],
                "prev_boost": prev_boost,
                "new_boost": r.boost_score + dados_boost,
                "cnpj": r.cnpj_data.get("cnpj") if r.cnpj_data else None,
                "email_receita": email_descoberto or None,
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
