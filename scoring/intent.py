"""Detector de palavras-chave de intencao."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from scoring.config_loader import load_intent_keywords


@dataclass
class IntentSignal:
    categoria: str
    palavra_chave: str
    trecho: str
    boost: int


def _normalize(text: str) -> str:
    """Lowercase, remove acentos, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


@lru_cache(maxsize=512)
def _kw_pattern(kw_norm: str) -> re.Pattern:
    """Regex com word boundary — keyword tem que ser palavra inteira."""
    return re.compile(r"\b" + re.escape(kw_norm) + r"\b")


def detect(text: str) -> tuple[list[IntentSignal], int]:
    """
    Detecta palavras-chave de intencao no texto.
    Retorna (lista de sinais, boost total aplicado com teto).

    Matching por WORD BOUNDARY (\\b) — keyword tem que ser palavra inteira,
    nao substring. Trecho extraido do texto NORMALIZADO (indices corretos).
    """
    if not text:
        return [], 0

    cfg = load_intent_keywords()
    norm = _normalize(text)

    sinais: list[IntentSignal] = []
    boost_por_categoria: dict[str, int] = {}

    for cat, info in cfg["categorias"].items():
        cat_boost = int(info["boost"])
        for kw in info["palavras"]:
            kw_norm = _normalize(kw)
            if len(kw_norm) < 3:
                continue
            m = _kw_pattern(kw_norm).search(norm)
            if m:
                idx = m.start()
                trecho = norm[max(0, idx - 45): idx + len(kw_norm) + 65].strip()
                sinais.append(
                    IntentSignal(
                        categoria=cat,
                        palavra_chave=kw,
                        trecho=trecho,
                        boost=cat_boost,
                    )
                )
                boost_por_categoria.setdefault(cat, cat_boost)
                break

    total = sum(boost_por_categoria.values())
    teto = int(cfg.get("teto_cumulativo", 50))
    return sinais, min(total, teto)
