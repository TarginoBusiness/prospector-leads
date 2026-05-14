"""
Utilitario de normalizacao de texto COM MAPA DE INDICES.

Por que existe:
  Os detectores (interest_detector, scoring.intent) normalizam o texto
  (lowercase, sem acento, whitespace colapsado) pra casar keywords sem
  falso negativo. MAS o `trecho` salvo no banco precisa ser o texto
  ORIGINAL — porque o dashboard monta um Chrome Text Fragment
  (#:~:text=...) com esse trecho, e o Chrome so destaca/scrolla se o
  texto bater EXATAMENTE com o que esta na pagina (com acento e
  maiuscula). Trecho normalizado nunca casa -> nada destacado.

  _normalize_mapped() devolve (texto_normalizado, mapa) onde
  mapa[i] = indice no texto ORIGINAL do i-esimo caractere do normalizado.
  Assim da pra achar a keyword no texto normalizado e recortar o trecho
  do texto ORIGINAL.
"""
from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    """Lowercase, sem acento, whitespace colapsado. (versao sem mapa)"""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _normalize_mapped(text: str) -> tuple[str, list[int]]:
    """
    Mesma normalizacao de _normalize() (NFKD, drop combining, lower,
    colapsa whitespace, strip), mas tambem devolve o mapa de indices
    normalizado -> original.
    """
    out_chars: list[str] = []
    out_map: list[int] = []
    prev_ws = True  # True no inicio -> descarta whitespace a esquerda (strip-left)

    for orig_i, ch in enumerate(text):
        for d in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(d):
                continue
            d = d.lower()
            if d.isspace():
                if prev_ws:
                    continue
                out_chars.append(" ")
                out_map.append(orig_i)
                prev_ws = True
            else:
                out_chars.append(d)
                out_map.append(orig_i)
                prev_ws = False

    # strip-right
    while out_chars and out_chars[-1] == " ":
        out_chars.pop()
        out_map.pop()

    return "".join(out_chars), out_map


def trecho_original(text: str, norm: str, nmap: list[int],
                    idx_norm: int, kw_len: int,
                    antes: int = 60, depois: int = 90) -> str:
    """
    Dado um match na posicao `idx_norm` do texto normalizado, recorta a
    janela equivalente no texto ORIGINAL (com acento e maiuscula),
    colapsa whitespace e devolve pronto pra salvar como `trecho`.
    """
    if not nmap:
        return ""
    n = len(norm)
    start_n = max(0, idx_norm - antes)
    end_n = min(n - 1, idx_norm + kw_len + depois - 1)
    o_start = nmap[start_n]
    o_end = nmap[end_n] + 1
    return re.sub(r"\s+", " ", text[o_start:o_end]).strip()


def keyword_como_na_pagina(trecho: str, keyword: str) -> str:
    """
    Acha a `keyword` (forma normalizada do config) dentro do `trecho`
    (texto ORIGINAL) e devolve a keyword EXATAMENTE como aparece na
    pagina — com acento e maiuscula. Esse eh o melhor texto pra um
    Chrome Text Fragment: curto, exato, casa = comportamento Ctrl+F.
    Retorna "" se nao achar.
    """
    if not trecho or not keyword:
        return ""
    kw_norm = _normalize(keyword)
    if not kw_norm:
        return ""
    tnorm, tmap = _normalize_mapped(trecho)
    i = tnorm.find(kw_norm)
    if i == -1 or not tmap:
        return ""
    o_start = tmap[i]
    o_end = tmap[i + len(kw_norm) - 1] + 1
    return trecho[o_start:o_end].strip()
