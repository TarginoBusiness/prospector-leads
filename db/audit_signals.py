"""
Auditoria de sinais: pra CADA intent_signal, verifica se a palavra-chave
REALMENTE aparece no trecho_texto. Se nao aparecer = false positive / bug.
"""
from __future__ import annotations

import re
import unicodedata

from db.connection import get_conn


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT id, lead_id, categoria, palavra_chave, trecho_texto, captured_at
            FROM intent_signals
            ORDER BY captured_at DESC
        """)
        rows = cur.fetchall()

    total = len(rows)
    if total == 0:
        print("[audit] 0 sinais no banco")
        return

    validos = 0
    invalidos = []
    interest_count = 0
    intent_count = 0

    for sid, lead_id, cat, kw, trecho, captured in rows:
        is_interest = (cat or "").startswith("interest_")
        if is_interest:
            interest_count += 1
        else:
            intent_count += 1

        kw_norm = _normalize(kw)
        trecho_norm = _normalize(trecho)
        # keyword aparece no trecho como WORD BOUNDARY?
        if kw_norm and re.search(r"\b" + re.escape(kw_norm) + r"\b", trecho_norm):
            validos += 1
        else:
            if len(invalidos) < 25:
                invalidos.append((sid, lead_id, cat, kw, (trecho or "")[:90]))

    print(f"[audit] TOTAL: {total} sinais ({interest_count} interest, {intent_count} intent)")
    print(f"[audit] VALIDOS (keyword no trecho): {validos} ({100*validos//total}%)")
    print(f"[audit] INVALIDOS (false positive/bug): {total - validos} ({100*(total-validos)//total}%)")
    print()
    if invalidos:
        print("[audit] Amostra de INVALIDOS:")
        for sid, lid, cat, kw, trecho in invalidos:
            print(f"  id={sid} lead={lid} cat={cat}")
            print(f"     kw='{kw}'")
            print(f"     trecho='{trecho}'")
    else:
        print("[audit] ✅ TODOS os sinais sao validos (keyword presente no trecho)")

    # Distribuicao por categoria
    print()
    with get_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT categoria, COUNT(*)
            FROM intent_signals
            GROUP BY categoria ORDER BY COUNT(*) DESC
        """)
        print("[audit] Distribuicao por categoria:")
        for cat, cnt in cur.fetchall():
            print(f"  {cnt:5d}  {cat}")


if __name__ == "__main__":
    main()
