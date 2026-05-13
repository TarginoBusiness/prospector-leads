"""Conexao com Neon Postgres."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Carrega .env se existir (dev local). Em CI/Streamlit Cloud, vem de secrets.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL nao definida. Configure no .env (dev) ou nos GitHub Secrets / "
            "Streamlit Cloud secrets (prod)."
        )
    return dsn


@contextmanager
def get_conn():
    """Context manager — cuida de commit/rollback/close."""
    conn = psycopg.connect(get_dsn(), autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> bool:
    """Testa conexao."""
    try:
        with get_conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)
    except Exception as e:
        print(f"[db.ping] FALHOU: {e}")
        return False
