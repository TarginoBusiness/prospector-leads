"""Operacoes de leitura/escrita de leads e logs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from db.connection import get_conn


def make_fingerprint(*, telefone: str | None, email: str | None, nome: str | None, source: str) -> str:
    """Hash estavel pra dedup. Prioridade: telefone > email > (nome+source)."""
    key = (telefone or email or f"{nome}|{source}" or "").strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def upsert_lead(lead: dict[str, Any]) -> tuple[int, bool]:
    """
    Insere ou atualiza um lead. Retorna (lead_id, is_new).
    `lead` deve conter as chaves de `leads` (qualquer subset; faltantes viram NULL).
    """
    lead.setdefault("first_seen_at", datetime.now(timezone.utc))
    lead.setdefault("last_seen_at", datetime.now(timezone.utc))

    sql = """
        INSERT INTO leads (
            fingerprint, source, source_url, nome, telefone, email, cnpj,
            nicho, cidade, cidade_tag, estado,
            score_temperatura, score_breakdown, raw_payload,
            first_seen_at, last_seen_at
        ) VALUES (
            %(fingerprint)s, %(source)s, %(source_url)s, %(nome)s, %(telefone)s, %(email)s, %(cnpj)s,
            %(nicho)s, %(cidade)s, %(cidade_tag)s, %(estado)s,
            %(score_temperatura)s, %(score_breakdown)s, %(raw_payload)s,
            %(first_seen_at)s, %(last_seen_at)s
        )
        ON CONFLICT (fingerprint) DO UPDATE SET
            last_seen_at      = EXCLUDED.last_seen_at,
            score_temperatura = GREATEST(leads.score_temperatura, EXCLUDED.score_temperatura),
            score_breakdown   = EXCLUDED.score_breakdown,
            nicho             = COALESCE(EXCLUDED.nicho, leads.nicho),
            cidade            = COALESCE(EXCLUDED.cidade, leads.cidade),
            cidade_tag        = COALESCE(EXCLUDED.cidade_tag, leads.cidade_tag),
            telefone          = COALESCE(EXCLUDED.telefone, leads.telefone),
            email             = COALESCE(EXCLUDED.email, leads.email),
            cnpj              = COALESCE(EXCLUDED.cnpj, leads.cnpj),
            raw_payload       = leads.raw_payload || EXCLUDED.raw_payload
        RETURNING id, (xmax = 0) AS is_new;
    """

    params = {
        "fingerprint": lead["fingerprint"],
        "source": lead["source"],
        "source_url": lead.get("source_url"),
        "nome": lead.get("nome"),
        "telefone": lead.get("telefone"),
        "email": lead.get("email"),
        "cnpj": lead.get("cnpj"),
        "nicho": lead.get("nicho"),
        "cidade": lead.get("cidade"),
        "cidade_tag": lead.get("cidade_tag"),
        "estado": lead.get("estado"),
        "score_temperatura": lead.get("score_temperatura", 0),
        "score_breakdown": json.dumps(lead.get("score_breakdown", {})),
        "raw_payload": json.dumps(lead.get("raw_payload", {})),
        "first_seen_at": lead["first_seen_at"],
        "last_seen_at": lead["last_seen_at"],
    }

    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]), bool(row[1])


def insert_intent_signals(lead_id: int, signals: list[dict[str, Any]]) -> None:
    if not signals:
        return
    sql = """
        INSERT INTO intent_signals (lead_id, categoria, palavra_chave, trecho_texto, source_url, boost)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    with get_conn() as c, c.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    lead_id,
                    s["categoria"],
                    s["palavra_chave"],
                    s.get("trecho_texto"),
                    s.get("source_url"),
                    s["boost"],
                )
                for s in signals
            ],
        )


def insert_raw_page(source: str, url: str, html: str) -> None:
    sql = """
        INSERT INTO raw_pages (source, url, html)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, (source, url, html))


def start_run(source: str, metadata: dict | None = None) -> int:
    sql = "INSERT INTO scrape_runs (source, metadata) VALUES (%s, %s) RETURNING id"
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, (source, json.dumps(metadata or {})))
        return int(cur.fetchone()[0])


def end_run(
    run_id: int,
    *,
    pages_ok: int,
    pages_failed: int,
    leads_new: int,
    leads_updated: int,
    error_summary: str | None = None,
) -> None:
    sql = """
        UPDATE scrape_runs SET
            ended_at = NOW(),
            pages_ok = %s, pages_failed = %s,
            leads_new = %s, leads_updated = %s,
            error_summary = %s
        WHERE id = %s
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, (pages_ok, pages_failed, leads_new, leads_updated, error_summary, run_id))


def push_dead_letter(url: str, source: str, error: str) -> None:
    sql = """
        INSERT INTO dead_letter (url, source, last_error, retries)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT DO NOTHING
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, (url, source, error[:2000]))
