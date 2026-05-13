"""
Engine principal de scoring.

Calcula score 0-100 de "temperatura" do lead.
Componentes (todos clampados em 0-100 no final):
    - base por tipo de fonte
    - boost por cidade-alvo (Sao Luis pesa mais)
    - boost por nicho prioritario
    - boost por intencao declarada (palavras-chave)
    - boost por dados-extras (whatsapp validado, ja tem cnpj, etc)
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from scoring.config_loader import load_cities, load_niches
from scoring.intent import IntentSignal, detect


# Base por tipo de fonte de scraping
BASE_FONTE = {
    "workana":      40,   # gente literalmente postando "preciso de"
    "99freelas":    40,
    "twitter":      35,
    "reddit":       30,
    "olx":          25,
    "mercadolivre": 25,
    "instagram":    20,
    "linkedin":     20,
    "gmaps":        10,   # varredura larga
    "brasilapi":    10,
    "cnpjbiz":      10,
}


@dataclass
class ScoreResult:
    score: int
    breakdown: dict[str, int] = field(default_factory=dict)
    cidade_tag: str | None = None
    nicho: str | None = None
    intent_signals: list[IntentSignal] = field(default_factory=list)


def _normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def detect_cidade(text: str) -> tuple[str | None, int]:
    if not text:
        return None, 0
    norm = _normalize(text)
    cities = load_cities()["cidades"]
    for tag, info in cities.items():
        for alias in info["aliases"]:
            if _normalize(alias) in norm:
                return tag, int(info["boost"])
    return None, 0


def detect_nicho(text: str) -> tuple[str | None, int]:
    if not text:
        return None, 0
    norm = _normalize(text)
    cfg = load_niches()
    for nicho, info in cfg["nichos"].items():
        for q in info["queries"]:
            if _normalize(q) in norm:
                return nicho, int(info["boost"])
    return None, 0


def calcular(
    *,
    source: str,
    texto_para_analisar: str = "",
    has_telefone: bool = False,
    has_email: bool = False,
    has_cnpj: bool = False,
    cidade_hint: str | None = None,    # se o scraper ja sabe a cidade, passa aqui
    nicho_hint: str | None = None,     # idem pra nicho
) -> ScoreResult:
    """Calcula score completo. Returns ScoreResult com breakdown auditavel."""
    breakdown: dict[str, int] = {}

    # 1) Base por fonte
    base = BASE_FONTE.get(source, 5)
    breakdown["base_fonte"] = base

    # 2) Cidade
    cidade_tag = None
    if cidade_hint:
        cidade_tag = cidade_hint
        cities = load_cities()["cidades"]
        if cidade_hint in cities:
            breakdown["cidade"] = int(cities[cidade_hint]["boost"])
    else:
        cidade_tag, boost_cidade = detect_cidade(texto_para_analisar)
        if boost_cidade:
            breakdown["cidade"] = boost_cidade

    # 3) Nicho
    nicho = nicho_hint
    if nicho_hint:
        cfg = load_niches()
        nichos = {**cfg["nichos"], **cfg.get("nichos_regionais_sao_luis", {})}
        if nicho_hint in nichos:
            breakdown["nicho"] = int(nichos[nicho_hint]["boost"])
    else:
        nicho, boost_nicho = detect_nicho(texto_para_analisar)
        if boost_nicho:
            breakdown["nicho"] = boost_nicho

    # 4) Intent
    signals, boost_intent = detect(texto_para_analisar)
    if boost_intent:
        breakdown["intent"] = boost_intent

    # 5) Bonus de qualidade de dado
    if has_telefone:
        breakdown["tem_telefone"] = 5
    if has_email:
        breakdown["tem_email"] = 3
    if has_cnpj:
        breakdown["tem_cnpj"] = 5

    total = sum(breakdown.values())
    score = max(0, min(100, total))

    return ScoreResult(
        score=score,
        breakdown=breakdown,
        cidade_tag=cidade_tag,
        nicho=nicho,
        intent_signals=signals,
    )
