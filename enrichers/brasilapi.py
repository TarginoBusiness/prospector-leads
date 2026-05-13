"""
Enriquecimento via BrasilAPI — quando temos um CNPJ, puxamos tudo da Receita.

API publica e gratuita: https://brasilapi.com.br/api/cnpj/v1/<cnpj>
Retorna: razao_social, nome_fantasia, email, ddd_telefone_1, ddd_telefone_2,
         logradouro, municipio, uf, cnae_fiscal_descricao, etc.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("enricher.brasilapi")


@dataclass
class CNPJData:
    cnpj: str
    razao_social: str = ""
    nome_fantasia: str = ""
    email: str = ""
    telefone: str = ""
    cidade: str = ""
    estado: str = ""
    cnae_descricao: str = ""
    situacao: str = ""


async def consultar_cnpj(cnpj_raw: str) -> CNPJData | None:
    cnpj = re.sub(r"\D", "", cnpj_raw)
    if len(cnpj) != 14:
        return None

    url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
    log.info(f"  BrasilAPI: GET {url}")

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(url)
            if r.status_code == 404:
                log.info(f"  CNPJ {cnpj} nao encontrado")
                return None
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(f"  BrasilAPI falhou: {e}")
            return None

    tel = data.get("ddd_telefone_1") or data.get("ddd_telefone_2") or ""
    if tel and not tel.startswith("+"):
        tel = "+55" + re.sub(r"\D", "", tel)

    return CNPJData(
        cnpj=cnpj,
        razao_social=data.get("razao_social", "") or "",
        nome_fantasia=data.get("nome_fantasia", "") or "",
        email=data.get("email", "") or "",
        telefone=tel,
        cidade=data.get("municipio", "") or "",
        estado=data.get("uf", "") or "",
        cnae_descricao=data.get("cnae_fiscal_descricao", "") or "",
        situacao=data.get("descricao_situacao_cadastral", "") or "",
    )
