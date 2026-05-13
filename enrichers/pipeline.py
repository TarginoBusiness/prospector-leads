"""
Pipeline de enriquecimento de leads.

Pega leads sem telefone/email no banco e roda multiplas fontes de enriquecimento.
Cada fonte que acerta deposita seu achado no lead. Score eh recalculado no final.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from db.connection import get_conn
from enrichers.brasilapi import consultar_cnpj
from enrichers.extractors import extract_all
from enrichers.workana_detail import enriquecer as enriquecer_workana

log = logging.getLogger("enricher.pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


SQL_PEGAR_LEADS = """
    SELECT id, source, source_url, raw_payload
    FROM leads
    WHERE telefone IS NULL
      AND source IN ('workana', '99freelas')
      AND source_url IS NOT NULL
    ORDER BY score_temperatura DESC, last_seen_at DESC
    LIMIT %s
"""

SQL_ATUALIZAR_LEAD = """
    UPDATE leads SET
        telefone           = COALESCE(telefone, %(telefone)s),
        email              = COALESCE(email, %(email)s),
        cnpj               = COALESCE(cnpj, %(cnpj)s),
        nome               = COALESCE(nome, %(nome)s),
        cidade             = COALESCE(cidade, %(cidade)s),
        estado             = COALESCE(estado, %(estado)s),
        cidade_tag         = COALESCE(cidade_tag, %(cidade_tag)s),
        score_temperatura  = LEAST(100, score_temperatura + %(score_boost)s),
        score_breakdown    = score_breakdown || %(breakdown_extra)s::jsonb,
        raw_payload        = raw_payload || %(payload_extra)s::jsonb,
        last_seen_at       = NOW()
    WHERE id = %(id)s
"""


def _match_cidade_tag(cidade: str, estado: str) -> str | None:
    """Mapeia 'Sao Luis' / 'MA' pra tag do dashboard."""
    if not cidade:
        return None
    cidade_lower = cidade.lower().strip()
    estado_upper = (estado or "").upper().strip()
    if "luis" in cidade_lower and (estado_upper == "MA" or not estado_upper):
        return "sao-luis"
    if "paulo" in cidade_lower and estado_upper == "SP":
        return "sao-paulo"
    if cidade_lower == "curitiba":
        return "curitiba"
    if "janeiro" in cidade_lower or "rio" == cidade_lower:
        return "rio-de-janeiro"
    return None


async def enriquecer_lead(page, lead: dict) -> dict | None:
    """Roda todas as fontes de enriquecimento em 1 lead. Retorna o update dict."""
    lead_id = lead["id"]
    source = lead["source"]
    url = lead["source_url"]

    log.info(f"== Lead #{lead_id} ({source}) — {url}")

    achados = {
        "telefone": None,
        "email": None,
        "cnpj": None,
        "nome": None,
        "cidade": None,
        "estado": None,
        "cidade_tag": None,
        "score_boost": 0,
        "breakdown_extra": {},
        "payload_extra": {},
    }

    # === FONTE 1: Pagina detalhada do Workana ===
    if source == "workana":
        detail = await enriquecer_workana(page, url)
        if detail:
            if detail.contatos.telefones:
                achados["telefone"] = detail.contatos.telefones[0]
                achados["score_boost"] += 30
                achados["breakdown_extra"]["enriq_telefone"] = 30
            if detail.contatos.emails:
                achados["email"] = detail.contatos.emails[0]
                achados["score_boost"] += 10
                achados["breakdown_extra"]["enriq_email"] = 10
            if detail.contatos.cnpjs:
                achados["cnpj"] = detail.contatos.cnpjs[0]
                achados["score_boost"] += 5

            if detail.cliente_nome:
                achados["nome"] = detail.cliente_nome
            if detail.cliente_cidade:
                achados["cidade"] = detail.cliente_cidade
                achados["estado"] = detail.cliente_estado
                achados["cidade_tag"] = _match_cidade_tag(detail.cliente_cidade, detail.cliente_estado)
                if achados["cidade_tag"]:
                    boost = 15 if achados["cidade_tag"] == "sao-luis" else 10
                    achados["score_boost"] += boost
                    achados["breakdown_extra"][f"enriq_cidade_{achados['cidade_tag']}"] = boost

            achados["payload_extra"]["workana_detail"] = {
                "descricao_completa": (detail.descricao_completa or "")[:2000],
                "skills": detail.skills,
                "cliente_username": detail.cliente_username,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }

    # === FONTE 2: BrasilAPI (se temos CNPJ) ===
    if achados.get("cnpj"):
        cnpj_data = await consultar_cnpj(achados["cnpj"])
        if cnpj_data:
            if cnpj_data.telefone and not achados["telefone"]:
                achados["telefone"] = cnpj_data.telefone
                achados["score_boost"] += 20
                achados["breakdown_extra"]["enriq_cnpj_tel"] = 20
            if cnpj_data.email and not achados["email"]:
                achados["email"] = cnpj_data.email
                achados["score_boost"] += 10
            if cnpj_data.nome_fantasia and not achados["nome"]:
                achados["nome"] = cnpj_data.nome_fantasia or cnpj_data.razao_social
            if cnpj_data.cidade and not achados["cidade"]:
                achados["cidade"] = cnpj_data.cidade
                achados["estado"] = cnpj_data.estado
                tag = _match_cidade_tag(cnpj_data.cidade, cnpj_data.estado)
                if tag:
                    achados["cidade_tag"] = tag
            achados["payload_extra"]["brasilapi"] = {
                "razao_social": cnpj_data.razao_social,
                "cnae": cnpj_data.cnae_descricao,
                "situacao": cnpj_data.situacao,
            }

    # === FONTE 3: regex no raw_payload (descricao + titulo armazenados) ===
    if lead.get("raw_payload"):
        payload = lead["raw_payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        texto_acumulado = " ".join([
            str(payload.get("titulo", "")),
            str(payload.get("descricao", "")),
            str((payload.get("workana_detail") or {}).get("descricao_completa", "")),
        ])
        contatos = extract_all(texto_acumulado)
        if contatos.telefones and not achados["telefone"]:
            achados["telefone"] = contatos.telefones[0]
            achados["score_boost"] += 25
        if contatos.emails and not achados["email"]:
            achados["email"] = contatos.emails[0]
            achados["score_boost"] += 8

    return achados if (achados["telefone"] or achados["email"] or achados["cnpj"] or achados["cidade_tag"]) else None


async def main(limit: int = 30) -> None:
    log.info(f"== Iniciando enriquecimento (limit={limit}) ==")
    # Pega leads
    with get_conn() as c, c.cursor() as cur:
        cur.execute(SQL_PEGAR_LEADS, (limit,))
        cols = [d.name for d in cur.description]
        leads = [dict(zip(cols, row)) for row in cur.fetchall()]
    log.info(f"  {len(leads)} leads pra enriquecer")

    if not leads:
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="pt-BR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        enriquecidos = 0
        for lead in leads:
            try:
                update = await enriquecer_lead(page, lead)
                if update:
                    with get_conn() as c, c.cursor() as cur:
                        cur.execute(SQL_ATUALIZAR_LEAD, {
                            "id": lead["id"],
                            "telefone": update["telefone"],
                            "email": update["email"],
                            "cnpj": update["cnpj"],
                            "nome": update["nome"],
                            "cidade": update["cidade"],
                            "estado": update["estado"],
                            "cidade_tag": update["cidade_tag"],
                            "score_boost": update["score_boost"],
                            "breakdown_extra": json.dumps(update["breakdown_extra"]),
                            "payload_extra": json.dumps(update["payload_extra"]),
                        })
                    enriquecidos += 1
                    log.info(
                        f"  ✓ atualizado lead #{lead['id']} (tel={update['telefone']} "
                        f"email={update['email']} cidade={update['cidade_tag']} "
                        f"boost=+{update['score_boost']})"
                    )
                else:
                    log.info(f"  - lead #{lead['id']}: nada novo encontrado")

                # rate limiting humano
                await asyncio.sleep(2)
            except Exception as e:
                log.exception(f"erro enriquecendo lead {lead['id']}: {e}")
                continue

        await browser.close()

    log.info(f"== Enriquecimento concluido: {enriquecidos}/{len(leads)} leads atualizados ==")


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(main(limit))
