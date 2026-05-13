"""Aplica o schema.sql no banco. Idempotente (CREATE IF NOT EXISTS)."""
from pathlib import Path

from db.connection import get_conn

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def main() -> None:
    print("[migrate] aplicando schema...")
    with get_conn() as c, c.cursor() as cur:
        cur.execute(SCHEMA)
    print("[migrate] OK")


if __name__ == "__main__":
    main()
