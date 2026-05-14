"""Conexao com Neon Postgres — com POOL de conexoes (mata o delay).

Antes: cada query abria uma conexao nova (TLS handshake + cold start do
Neon serverless) — ~1-3s de latencia POR query. Abrir uma ficha = 4
conexoes = eternidade.

Agora: um pool mantem conexoes quentes. Checkout eh instantaneo. A API
`with get_conn() as c` continua identica — nenhum call-site muda.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Carrega .env se existir (dev local). Em CI/Streamlit Cloud, vem de secrets.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# _pool: None = ainda nao tentou criar; False = sem pool (usa conexao direta);
#        ConnectionPool = pool ativo.
_pool = None


def get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL nao definida. Configure no .env (dev) ou nos GitHub Secrets / "
            "Streamlit Cloud secrets (prod)."
        )
    return dsn


def _get_pool():
    """Cria (lazy) e devolve o pool. Retorna None se psycopg_pool indisponivel."""
    global _pool
    if _pool is None:
        try:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                conninfo=get_dsn(),
                min_size=1,          # mantem 1 conexao sempre quente
                max_size=6,
                max_idle=120.0,      # recicla conexao ociosa (Neon dropa idle)
                timeout=10.0,
                kwargs={"autocommit": False},
                check=ConnectionPool.check_connection,  # valida antes de entregar
                open=False,
            )
            pool.open()
            _pool = pool
        except Exception as e:  # sem psycopg_pool ou falhou — cai no modo direto
            print(f"[db.connection] pool indisponivel, usando conexao direta: {e}")
            _pool = False
    return _pool or None


@contextmanager
def get_conn():
    """
    Context manager — cuida de commit/rollback/close.
    Usa o pool se disponivel (rapido); senao abre conexao direta.
    """
    pool = _get_pool()
    if pool is not None:
        # pool.connection() ja faz commit no sucesso, rollback no erro,
        # e DEVOLVE a conexao pro pool (nao fecha) — checkout instantaneo.
        with pool.connection() as conn:
            yield conn
        return

    # Fallback: conexao direta (mesmo comportamento de antes)
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
