"""Carrega configs YAML (intent_keywords, niches, cities)."""
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@lru_cache(maxsize=1)
def load_intent_keywords() -> dict:
    return yaml.safe_load((CONFIG_DIR / "intent_keywords.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_niches() -> dict:
    return yaml.safe_load((CONFIG_DIR / "niches.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_cities() -> dict:
    return yaml.safe_load((CONFIG_DIR / "cities.yaml").read_text(encoding="utf-8"))
