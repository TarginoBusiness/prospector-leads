"""
Detector de sinais de INTERESSE da empresa-lead.

Diferente do intent_detector (que busca pedido explicito tipo "preciso de bot"),
aqui buscamos mencao passiva: a empresa fala sobre IA, automacao, postou vaga
de dev, mencionou modernizacao, etc. Sinal de "to no momento certo".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from textutil import _normalize_mapped, trecho_original


@dataclass
class InterestSignal:
    categoria: str
    palavra_chave: str
    trecho: str
    boost: int
    source_name: str = ""   # qual fonte detectou (instagram, tiktok, news, ...)
    source_url: str = ""    # URL especifica onde o trecho foi visto


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "interest_keywords.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


@lru_cache(maxsize=512)
def _kw_pattern(kw_norm: str) -> re.Pattern:
    """
    Compila regex com WORD BOUNDARY pra evitar false positives.
    BUG ANTIGO: 'kw in texto' casava 'llm' dentro de 'smallman', 'rpa'
    dentro de 'Serpa', etc. Agora 'llm' so casa standalone.
    """
    # \b no inicio e fim — keyword tem que ser palavra(s) inteira(s)
    return re.compile(r"\b" + re.escape(kw_norm) + r"\b")


def detect(text: str) -> tuple[list[InterestSignal], int]:
    """
    Retorna (sinais, boost total aplicado com teto).
    1 hit por categoria conta o boost dela. Multiplas categorias somam.

    Matching por WORD BOUNDARY (regex \\b) — keyword tem que ser palavra
    inteira, nao substring. O MATCH roda no texto normalizado, mas o
    `trecho` salvo eh recortado do texto ORIGINAL (com acento e maiuscula)
    via mapa de indices — assim o Chrome Text Fragment do dashboard
    consegue destacar/scrollar ate a mencao na pagina real.
    """
    if not text:
        return [], 0

    cfg = load_config()
    norm, nmap = _normalize_mapped(text)

    sinais: list[InterestSignal] = []
    boost_por_categoria: dict[str, int] = {}

    for cat, info in cfg["categorias"].items():
        cat_boost = int(info["boost"])
        for kw in info["palavras"]:
            kw_norm = _normalize(kw)
            if len(kw_norm) < 3:  # ignora keywords curtas demais (perigosas)
                continue
            m = _kw_pattern(kw_norm).search(norm)
            if m:
                idx = m.start()
                # Trecho recortado do texto ORIGINAL (acento + maiuscula intactos)
                trecho = trecho_original(text, norm, nmap, idx, len(kw_norm))
                sinais.append(
                    InterestSignal(
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
