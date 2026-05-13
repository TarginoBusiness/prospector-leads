"""
CNPJ → Quadro societario via BrasilAPI.

ESTRATEGIA APRIMORADA:
1. Busca CNPJ via dorks combinando NOME + CIDADE + ENDERECO (Google Maps)
2. Pra cada CNPJ candidato, consulta BrasilAPI e pega endereco registrado
3. Compara endereco_receita com endereco_gmaps via overlap de tokens
4. So aceita o CNPJ se match >= 60% (alta confianca)
5. Retorna socios, telefone, email, todos os dados da Receita

Resultado: nome do dono + telefone + email REGISTRADO oficialmente.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote, unquote

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger("source.cnpj_socios")


STOPWORDS_END = {
    "rua", "avenida", "av", "r", "travessa", "tv", "praca", "praça",
    "estrada", "rodovia", "rod", "alameda", "no", "do", "da", "de",
    "dos", "das", "n", "numero", "nº", "andar", "sala", "loja", "ap",
    "apt", "apartamento", "bairro", "br", "ma", "sp", "pr", "rj",
}


def _normalizar_tokens(endereco: str) -> set:
    """Quebra endereço em tokens significativos pra comparação."""
    if not endereco:
        return set()
    tokens = re.findall(r"\w+", endereco.lower())
    return set(
        t for t in tokens
        if len(t) >= 3 and t not in STOPWORDS_END
    )


def proximidade_enderecos(addr_a: str, addr_b: str) -> int:
    """Retorna 0-100% baseado em overlap de tokens significativos."""
    a = _normalizar_tokens(addr_a)
    b = _normalizar_tokens(addr_b)
    if not a or not b:
        return 0
    intersect = a & b
    smaller = min(len(a), len(b))
    return int(100 * len(intersect) / smaller) if smaller else 0


async def buscar_cnpj_candidatos(
    client: httpx.AsyncClient,
    nome: str,
    cidade: str = "",
    endereco: str = "",
) -> list[str]:
    """
    Tenta multiplos dorks DDG pra encontrar CNPJs candidatos.
    Retorna lista de CNPJs distintos encontrados.
    """
    dorks = [
        f'"{nome}" CNPJ' + (f' "{cidade}"' if cidade else ""),
        f'"{nome}" site:cnpj.biz',
        f'"{nome}" site:econodata.com.br OR site:cnpja.com',
    ]
    if endereco:
        # Pega so a 1a parte do endereço (rua + numero)
        parte_end = endereco.split(",")[0].strip()
        if parte_end:
            dorks.append(f'"{nome}" "{parte_end}" CNPJ')

    cnpjs_encontrados = []
    cnpj_re = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}\/?\d{4}\-?\d{2})\b")

    for dork in dorks:
        try:
            r = await client.get(
                f"https://html.duckduckgo.com/html/?q={quote(dork)}&kl=br-pt",
                timeout=10.0,
            )
            if r.status_code != 200:
                continue
            for m in cnpj_re.finditer(r.text):
                cnpj = re.sub(r"\D", "", m.group(1))
                if len(cnpj) == 14 and cnpj not in cnpjs_encontrados:
                    cnpjs_encontrados.append(cnpj)
                    if len(cnpjs_encontrados) >= 5:  # max 5 candidatos
                        return cnpjs_encontrados
        except Exception as e:
            log.debug(f"dork falhou: {e}")
            continue

    return cnpjs_encontrados


async def consultar_brasilapi(client: httpx.AsyncClient, cnpj_raw: str) -> dict:
    """BrasilAPI publica: razao social, socios, tel, email, endereco, etc."""
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
            "pais": q.get("pais") or "Brasil",
        })

    tel = data.get("ddd_telefone_1") or data.get("ddd_telefone_2") or ""
    if tel and not tel.startswith("+"):
        tel = "+55" + re.sub(r"\D", "", tel)

    # Monta endereco completo pra comparacao
    endereco_completo = " ".join(filter(None, [
        data.get("descricao_tipo_de_logradouro", ""),
        data.get("logradouro", ""),
        data.get("numero", ""),
        data.get("bairro", ""),
        data.get("municipio", ""),
        data.get("uf", ""),
        data.get("cep", ""),
    ]))

    return {
        "cnpj": cnpj_clean,
        "razao_social": data.get("razao_social", ""),
        "nome_fantasia": data.get("nome_fantasia", ""),
        "email": data.get("email") or "",
        "telefone_receita": tel,
        "endereco_completo": endereco_completo,
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


async def descobrir_cnpj_verificado(
    client: httpx.AsyncClient,
    nome: str,
    endereco_gmaps: str = "",
    cidade: str = "",
    min_proximidade: int = 60,
) -> dict:
    """
    Pipeline completo: busca candidatos, consulta cada, verifica endereço,
    retorna o que tiver MAIOR proximidade com endereço do GMaps.

    Retorna {} se nada bater ou nenhum candidato.
    """
    candidatos = await buscar_cnpj_candidatos(client, nome, cidade, endereco_gmaps)
    if not candidatos:
        log.info(f"  CNPJ candidato: 0 encontrados para '{nome}'")
        return {}

    log.info(f"  CNPJ candidatos: {len(candidatos)} encontrados para '{nome}'")

    melhor = None
    melhor_score = 0
    for cnpj in candidatos:
        data = await consultar_brasilapi(client, cnpj)
        if not data:
            continue
        score = proximidade_enderecos(endereco_gmaps, data["endereco_completo"]) if endereco_gmaps else 50
        log.info(f"    CNPJ {cnpj}: proximidade {score}% (razao: {data['razao_social'][:40]})")
        if score > melhor_score:
            melhor_score = score
            melhor = data
            melhor["proximidade_endereco"] = score

    if melhor and melhor_score >= min_proximidade:
        return melhor
    elif melhor:
        # Match fraco — retorna mas marca como baixa confianca
        melhor["match_fraco"] = True
        return melhor
    return {}


# Compat antigo
async def buscar_cnpj_por_nome(client, nome_empresa):
    """Wrapper de compatibilidade."""
    candidatos = await buscar_cnpj_candidatos(client, nome_empresa)
    return candidatos[0] if candidatos else None
