"""
One-shot: marca como encerradas todas as runs orfas (started_at antigo,
ended_at NULL). Acontece quando workflow e cancelado via UI/CLI sem dar
chance do scraper rodar o finally que escreve end_run().
"""
from db.connection import get_conn


def main() -> None:
    sql = """
        UPDATE scrape_runs
        SET ended_at = NOW(),
            error_summary = COALESCE(error_summary, 'auto-fechado por cleanup (orfao > 1h)')
        WHERE ended_at IS NULL
          AND started_at < NOW() - INTERVAL '1 hour'
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        print(f"[cleanup] {cur.rowcount} runs orfas encerradas")


if __name__ == "__main__":
    main()
