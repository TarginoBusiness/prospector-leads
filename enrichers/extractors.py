"""
Extratores regex para encontrar telefone/email/CNPJ/WhatsApp em textos arbitrarios.

Sao a ultima linha de defesa: depois que pegamos tudo que pudemos da fonte,
varremos o texto coletado completo procurando dados de contato que escaparam.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers


# Telefone BR — varios formatos vistos na pratica
PHONE_PATTERNS = [
    # +55 (11) 99999-9999
    re.compile(r"\+?55[\s.\-]?\(?(\d{2})\)?[\s.\-]?(\d{4,5})[\s.\-]?(\d{4})"),
    # (11) 99999-9999
    re.compile(r"\(?(\d{2})\)?[\s.\-]?(\d{4,5})[\s.\-]?(\d{4})\b"),
    # 11999999999
    re.compile(r"\b(\d{2})(\d{4,5})(\d{4})\b"),
]

# Email
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# CNPJ (formatado ou cru)
CNPJ_RE = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}\/?\d{4}\-?\d{2})\b")

# wa.me / wa.link / chat.whatsapp.com
WHATSAPP_URL_RE = re.compile(
    r"(?:wa\.me|api\.whatsapp\.com\/send|chat\.whatsapp\.com|wa\.link)\/[\w\d\-\+\?\=\&]*",
    re.IGNORECASE,
)
# Numero apos palavra "whatsapp"
WHATSAPP_NEAR_RE = re.compile(
    r"(?:whats\s?app|whats|wpp|zap)[^\d]{0,20}(\+?\d[\d\s\-\(\)\.]{7,})",
    re.IGNORECASE,
)


@dataclass
class ContactInfo:
    telefones: list[str]
    emails: list[str]
    cnpjs: list[str]
    whatsapp_urls: list[str]

    @property
    def is_empty(self) -> bool:
        return not (self.telefones or self.emails or self.cnpjs or self.whatsapp_urls)


def _normalize_phone(raw: str) -> str | None:
    """Valida e normaliza pra E.164. Retorna None se invalido."""
    try:
        parsed = phonenumbers.parse(raw, "BR")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass
    return None


def _validate_cnpj(cnpj: str) -> bool:
    """Validacao matematica do CNPJ (dois digitos verificadores)."""
    nums = re.sub(r"\D", "", cnpj)
    if len(nums) != 14 or nums == nums[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    soma1 = sum(int(nums[i]) * pesos1[i] for i in range(12))
    d1 = (soma1 % 11)
    d1 = 0 if d1 < 2 else 11 - d1
    soma2 = sum(int(nums[i]) * pesos2[i] for i in range(13))
    d2 = (soma2 % 11)
    d2 = 0 if d2 < 2 else 11 - d2
    return nums[12] == str(d1) and nums[13] == str(d2)


def extract_all(text: str) -> ContactInfo:
    """Roda todos os extratores no texto. Deduplica e valida."""
    if not text:
        return ContactInfo([], [], [], [])

    telefones = set()
    for pat in PHONE_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(0)
            normalized = _normalize_phone(raw)
            if normalized:
                telefones.add(normalized)

    # WhatsApp explicito (texto "whatsapp 119...")
    for m in WHATSAPP_NEAR_RE.finditer(text):
        normalized = _normalize_phone(m.group(1))
        if normalized:
            telefones.add(normalized)

    emails = set(EMAIL_RE.findall(text))
    # Filtra emails obviamente nao-pessoais (no-reply, etc)
    emails = {e for e in emails if not e.lower().startswith(("noreply", "no-reply", "no_reply"))}

    cnpjs_brutos = set(CNPJ_RE.findall(text))
    cnpjs_validos = {c for c in cnpjs_brutos if _validate_cnpj(c)}

    whatsapp_urls = set(m.group(0) for m in WHATSAPP_URL_RE.finditer(text))

    return ContactInfo(
        telefones=sorted(telefones),
        emails=sorted(emails),
        cnpjs=sorted(cnpjs_validos),
        whatsapp_urls=sorted(whatsapp_urls),
    )
