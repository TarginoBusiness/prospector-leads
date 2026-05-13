"""Detector de palavras-chave de intencao."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

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


def detect(text: str) -> tuple[list[IntentSignal], int]:
    """
    Detecta palavras-chave de intencao no texto.
    Retorna (lista de sinais, boost total aplicado com teto).
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
            if kw_norm in norm:
                idx = norm.find(kw_norm)
                trecho = text[max(0, idx - 30) : idx + len(kw_norm) + 50]
                sinais.append(
                    IntentSignal(
                        categoria=cat,
                        palavra_chave=kw,
                        trecho=trecho.strip(),
                        boost=cat_boost,
                    )
                )
                boost_por_categoria.setdefault(cat, cat_boost)
                break  # 1 hit por categoria ja vale o boost total daquela categoria

    total = sum(boost_por_categoria.values())
    teto = int(cfg.get("teto_cumulativo", 50))
    return sinais, min(total, teto)
