"""
CNPJ → Quadro societario via BrasilAPI.

Dado PUBLICO oficial da Receita Federal. Retorna nomes dos socios,
participacao, qualificacao (administrador, etc).

Tambem busca CNPJ a partir do nome quando nao temos via dork em
sites de consulta de CNPJ publicos (cnpj.biz, etc).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote, unquote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.cnpj_socios")


async def buscar_cnpj_por_nome(client: httpx.AsyncClient, nome_empresa: str) -> str | None:
    """Dork no Google/DDG procurando o CNPJ pelo nome da empresa."""
    dork = f'"{nome_empresa}" CNPJ'
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(dork)}",
            timeout=10.0,
        )
        if r.status_code != 200:
            return None
        text = r.text
        # Regex CNPJ formatado ou cru
        m = re.search(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\b", text)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{14})\b", text)
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


async def consultar_brasilapi(client: httpx.AsyncClient, cnpj_raw: str) -> dict:
    """
    BrasilAPI publica gratuita: razao social, socios, tel oficial, email, etc.
    """
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    if len(cnpj_clean) != 14:
        return {}
    url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_clean}"
    try:
        r = await client.get(url, timeout=15.0)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception as e:
        log.warning(f"brasilapi falhou: {e}")
        return {}

    socios = []
    for q in (data.get("qsa") or []):
        socios.append({
            "nome": q.get("nome_socio", ""),
            "qualificacao": q.get("qualificacao_socio", ""),
            "data_entrada": q.get("data_entrada_sociedade", ""),
            "pais": q.get("pais", "") or "Brasil",
        })

    tel = data.get("ddd_telefone_1") or data.get("ddd_telefone_2") or ""
    if tel and not tel.startswith("+"):
        tel = "+55" + re.sub(r"\D", "", tel)

    return {
        "cnpj": cnpj_clean,
        "razao_social": data.get("razao_social", ""),
        "nome_fantasia": data.get("nome_fantasia", ""),
        "email": data.get("email") or "",
        "telefone_receita": tel,
        "data_abertura": data.get("data_inicio_atividade", ""),
        "natureza_juridica": data.get("natureza_juridica", ""),
        "cnae_principal": data.get("cnae_fiscal_descricao", ""),
        "capital_social": data.get("capital_social", 0),
        "situacao": data.get("descricao_situacao_cadastral", ""),
        "porte": data.get("porte", ""),
        "municipio": data.get("municipio", ""),
        "uf": data.get("uf", ""),
        "socios": socios,
    }
