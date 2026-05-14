"""
Deep OSINT v2.2 — orquestrador aprofundado (REESCRITO).

LICOES APRENDIDAS:
- v2.0: 7 buscas DDG/lead → DDG bloqueou → 0 sinais.
- v2.1: rodava detect() na RESPOSTA do DDG → DDG ecoa a query de volta
  ("no results found for: <query>") e a query continha as keywords →
  AUTO-CONTAMINACAO, false positives em massa.

ESTRATEGIA v2.2 — PRINCIPIO FUNDAMENTAL:
  detect() SO roda em CONTEUDO REAL de PAGINAS VISITADAS.
  Cada sinal aponta pra URL REAL e VERIFICAVEL.
  DDG eh usado APENAS pra DESCOBRIR URLs — nunca pra detectar.

FONTES (todas com source_url verificavel):
  1. Site oficial (URL conhecida do enrich) — visita home + /sobre + /vagas
  2. Instagram (URL conhecida) — visita bio
  3. Facebook (URL conhecida) — visita pagina
  4. LinkedIn company (descoberto via dork → VISITA a pagina)
  5. Paginas de vaga (descobertas via dork → VISITA cada uma)
  6. CNPJ via BrasilAPI (dado estruturado, source = brasilapi)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from enrichers.extractors import _validate_cnpj, extract_all
from enrichers.interest_detector import InterestSignal, detect
from enrichers.sources import cnpj_socios

log = logging.getLogger("enricher.deep_osint_v2")


@dataclass
class DeepOsintV2Result:
    sinais: list[InterestSignal] = field(default_factory=list)
    boost_score: int = 0
    instagram_url: str = ""
    facebook_url: str = ""
    site_url: str = ""
    linkedin_url: str = ""
    vaga_urls: list = field(default_factory=list)
    cnpj_data: dict = field(default_factory=dict)
    # contatos colhidos das paginas visitadas (email/telefone/whatsapp)
    contatos: dict = field(default_factory=dict)
    # textos_por_url: {url: texto} — cada texto vira detect com source_url=url
    textos_por_url: dict = field(default_factory=dict)
    fontes_consultadas: list[str] = field(default_factory=list)


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ============================================================================
# Visita de paginas — retorna (url, texto). detect() roda nisso.
# ============================================================================

# Palavras que, no href ou no texto do link, indicam pagina interna relevante
LINK_RELEVANTE = (
    "sobre", "quem-somos", "quemsomos", "empresa", "servico", "servicos",
    "solucoes", "solucao", "produto", "produtos", "vaga", "vagas",
    "trabalhe", "carreira", "carreiras", "oportunidade", "blog", "noticia",
    "noticias", "contato", "fale-conosco", "tecnologia", "inovacao", "digital",
)


async def visitar(client: httpx.AsyncClient, url: str, max_chars: int = 25000,
                  retornar_tree: bool = False):
    """
    Visita URL, retorna texto limpo. Timeout curto.
    Se retornar_tree=True, retorna (texto, HTMLParser) pra extrair links.
    """
    vazio = ("", None) if retornar_tree else ""
    if not url:
        return vazio
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = await client.get(url, timeout=8.0, follow_redirects=True)
        if r.status_code != 200:
            return vazio
        tree = HTMLParser(r.text)
        # Remove tags de script/style ANTES de extrair texto (evita pegar JS!)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        partes = []
        for sel in ["meta[name='description']", "meta[property='og:description']", "meta[property='og:title']"]:
            m = tree.css_first(sel)
            if m:
                c = m.attributes.get("content", "")
                if c:
                    partes.append(c)
        # separator=" " — poe espaco entre text-nodes, senao "Clinica"+"Agenda"
        # vira "ClinicaAgenda" e o trecho nunca casa com o Text Fragment do Chrome
        body = tree.body.text(separator=" ") if tree.body else (tree.text(separator=" ") or "")
        partes.append(body[:max_chars])
        texto = " ".join(partes)
        return (texto, tree) if retornar_tree else texto
    except Exception:
        return vazio


def _extrair_links_internos(tree: HTMLParser, base: str, dominio: str,
                            max_links: int = 6) -> list[str]:
    """
    Da home, extrai links internos (mesmo dominio) que parecem relevantes
    (sobre, servicos, vagas, blog...). Ordena: link relevante primeiro.
    Substitui o chute de paths fixos — usa os links REAIS do site.
    """
    from urllib.parse import urljoin

    candidatos: list[tuple[int, str]] = []
    vistos: set[str] = set()
    for a in tree.css("a"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base + "/", href)
        full = full.split("#")[0].rstrip("/")
        p = urlparse(full)
        if p.scheme not in ("http", "https") or dominio not in p.netloc:
            continue
        if full in vistos or full.rstrip("/") == base.rstrip("/"):
            continue
        # ignora arquivos / midia
        if re.search(r"\.(pdf|jpe?g|png|gif|svg|zip|mp4|webp)$", p.path, re.I):
            continue
        vistos.add(full)
        alvo = (p.path + " " + (a.text() or "")).lower()
        score = 1 if any(k in alvo for k in LINK_RELEVANTE) else 0
        candidatos.append((score, full))

    # relevantes primeiro, mantem ordem de aparicao dentro de cada grupo
    candidatos.sort(key=lambda x: -x[0])
    return [u for _, u in candidatos[:max_links]]


async def coletar_site(client: httpx.AsyncClient, site_url: str) -> dict:
    """
    Visita a HOME, le os links REAIS do site, e segue ate 6 paginas
    internas relevantes (sobre, servicos, vagas, blog...).
    Antes chutava paths fixos (/sobre, /servicos) que davam 404 na maioria.
    Retorna {url_completa: texto} — cada pagina com sua URL exata.
    """
    if not site_url:
        return {}
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    dominio = parsed.netloc.replace("www.", "")

    home_texto, home_tree = await visitar(client, base, retornar_tree=True)
    out: dict[str, str] = {}
    if home_texto and len(home_texto) > 50:
        out[base] = home_texto
    if home_tree is None:
        return out

    internos = _extrair_links_internos(home_tree, base, dominio, max_links=6)
    if internos:
        textos = await asyncio.gather(*[visitar(client, u) for u in internos])
        for u, t in zip(internos, textos):
            if t and len(t) > 50:
                out[u] = t
    return out


# ============================================================================
# DDG — APENAS pra DESCOBRIR URLs. Nunca rodamos detect() no resultado DDG.
# ============================================================================

async def ddg_buscar_urls(client: httpx.AsyncClient, query: str, max_urls: int = 5,
                          filtro_dominio: str = "") -> list[str]:
    """
    Roda dork DDG e retorna SO AS URLs dos resultados (nao o texto/snippet).
    filtro_dominio: se setado, so retorna URLs que contem essa string.
    """
    try:
        r = await client.get(
            f"https://html.duckduckgo.com/html/?q={quote(query)}&kl=br-pt",
            timeout=10.0,
        )
        if r.status_code != 200:
            return []
        tree = HTMLParser(r.text)
        urls = []
        for link in tree.css("a.result__a"):
            href = link.attributes.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
            if href.startswith("http"):
                if not filtro_dominio or filtro_dominio in href:
                    urls.append(href)
            if len(urls) >= max_urls:
                break
        return urls
    except Exception as e:
        log.debug(f"ddg_buscar_urls falhou: {e}")
        return []


# ============================================================================
# Orquestrador v2.2
# ============================================================================

async def aprofundar_v2(
    *,
    nome: str,
    cidade: str = "",
    cidade_tag: str = "",
    cnpj: str = "",
    telefone: str = "",
    site_url: str = "",
    instagram_url: str = "",
    facebook_url: str = "",
    endereco_gmaps: str = "",
) -> DeepOsintV2Result:
    """
    v2.2 — detect() SO em conteudo de paginas visitadas. Cada sinal tem
    source_url verificavel. DDG so descobre URLs.
    """
    res = DeepOsintV2Result()
    if not nome:
        return res

    res.site_url = site_url
    res.instagram_url = instagram_url
    res.facebook_url = facebook_url

    async with httpx.AsyncClient(headers=COMMON_HEADERS, timeout=10.0) as client:
        # ---- FASE 1: DESCOBRIR URLs via DDG (sem detect aqui!) ----
        # LinkedIn da empresa
        linkedin_dork = f'site:linkedin.com/company "{nome}"'
        # Vagas genericas: paginas que falam de vaga/contratacao DESSA empresa
        vaga_dork = (
            f'"{nome}" (vaga OR vagas OR "trabalhe conosco" OR contratando OR '
            f'"estamos contratando" OR "oportunidade")'
        )
        # DEMANDA EXPLICITA (peso maximo): vaga de pre-atendimento/atendente/
        # recepcao — exatamente o que nossa automacao de WhatsApp resolve.
        # Pega LinkedIn Jobs, Workana, GetNinjas, OLX, Indeed etc naturalmente.
        demanda_dork = (
            f'"{nome}" (atendente OR recepcionista OR "atendimento ao cliente" OR '
            f'sac OR "pre-atendimento" OR "central de atendimento" OR telemarketing) '
            f'(vaga OR contratando OR "trabalhe conosco" OR "estamos contratando")'
        )
        if cidade:
            vaga_dork += f' "{cidade}"'
            demanda_dork += f' "{cidade}"'

        linkedin_urls, vaga_urls_raw, demanda_urls_raw = await asyncio.gather(
            ddg_buscar_urls(client, linkedin_dork, max_urls=2, filtro_dominio="linkedin.com/company"),
            ddg_buscar_urls(client, vaga_dork, max_urls=4),
            ddg_buscar_urls(client, demanda_dork, max_urls=5),
        )
        res.linkedin_url = linkedin_urls[0] if linkedin_urls else ""
        # Junta vaga generica + demanda explicita, filtra redes sociais, dedup.
        # demanda_urls primeiro (prioridade — sinal mais forte).
        res.vaga_urls = []
        for u in demanda_urls_raw + vaga_urls_raw:
            if any(x in u for x in ["facebook.com", "instagram.com", "twitter.com"]):
                continue
            if u not in res.vaga_urls:
                res.vaga_urls.append(u)
        res.vaga_urls = res.vaga_urls[:5]

        # ---- FASE 2: VISITAR todas as paginas (detect roda no conteudo) ----
        # Monta lista de (chave_fonte, url) pra visitar
        paginas_pra_visitar = []
        if instagram_url:
            paginas_pra_visitar.append(("instagram", instagram_url))
        if facebook_url:
            paginas_pra_visitar.append(("facebook", facebook_url))
        if res.linkedin_url:
            paginas_pra_visitar.append(("linkedin", res.linkedin_url))
        for vu in res.vaga_urls:
            paginas_pra_visitar.append(("vaga", vu))

        # Visita site (multi-pagina) + as outras paginas, tudo paralelo
        site_task = coletar_site(client, site_url) if site_url else _empty_dict()
        outras_tasks = [visitar(client, u) for _, u in paginas_pra_visitar]
        site_dict, *outras_textos = await asyncio.gather(site_task, *outras_tasks)

        # textos_por_url: cada URL com seu texto
        if isinstance(site_dict, dict):
            for u, t in site_dict.items():
                res.textos_por_url[u] = t
            if site_dict:
                res.fontes_consultadas.append("site")

        for (fonte, url), texto in zip(paginas_pra_visitar, outras_textos):
            if texto and len(texto) > 50:
                res.textos_por_url[url] = texto
                res.fontes_consultadas.append(fonte)

        # ---- FASE 2.5: colhe contatos (email/tel/whatsapp) das paginas ----
        res.contatos = _extrair_contatos_de_textos(res.textos_por_url)

        # ---- FASE 3: CNPJ via BrasilAPI (dado estruturado direto) ----
        # Prioridade: 1) CNPJ ja conhecido  2) CNPJ no rodape do site visitado
        # (confiavel, sem DDG)  3) descoberta via dorks DDG (fallback).
        try:
            cnpj_data = {}
            cnpj_do_site = "" if cnpj else _extrair_cnpj_de_textos(res.textos_por_url)
            if cnpj:
                cnpj_data = await cnpj_socios.consultar_brasilapi(client, cnpj)
                if cnpj_data:
                    res.fontes_consultadas.append("brasilapi_cnpj")
            elif cnpj_do_site:
                cnpj_data = await cnpj_socios.consultar_brasilapi(client, cnpj_do_site)
                if cnpj_data:
                    res.fontes_consultadas.append("cnpj_site")
                    log.info(f"  CNPJ achado no rodape do site: {cnpj_do_site}")
            if not cnpj_data:
                cnpj_data = await cnpj_socios.descobrir_cnpj_verificado(
                    client, nome, endereco_gmaps=endereco_gmaps,
                    cidade=cidade, min_proximidade=60,
                )
                if cnpj_data:
                    res.fontes_consultadas.append("brasilapi_cnpj")
            if cnpj_data:
                res.cnpj_data = cnpj_data
        except Exception as e:
            log.debug(f"cnpj falhou: {e}")

    # ---- DETECCAO: roda SO no conteudo de paginas reais ----
    all_sinais = []
    categorias_aplicadas = set()
    boost_total = 0
    for url, texto in res.textos_por_url.items():
        sinais_da_pagina, _ = detect(texto)
        for s in sinais_da_pagina:
            s.source_url = url  # URL REAL E VERIFICAVEL da pagina onde foi visto
            # source_name = tipo de fonte (deduz da url)
            if "linkedin.com" in url:
                s.source_name = "linkedin"
            elif "instagram.com" in url:
                s.source_name = "instagram"
            elif "facebook.com" in url:
                s.source_name = "facebook"
            elif url in res.vaga_urls:
                s.source_name = "vaga"
            else:
                s.source_name = "site"
            all_sinais.append(s)
            if s.categoria not in categorias_aplicadas:
                categorias_aplicadas.add(s.categoria)
                boost_total += s.boost

    res.sinais = all_sinais
    res.boost_score = min(boost_total, 100)

    log.info(
        f"  deep_osint_v2.2: {len(res.fontes_consultadas)} fontes "
        f"({len(res.textos_por_url)} paginas visitadas), "
        f"{len(all_sinais)} sinais ({len(categorias_aplicadas)} cat), boost +{res.boost_score}, "
        f"linkedin={bool(res.linkedin_url)} vagas={len(res.vaga_urls)} cnpj={bool(res.cnpj_data)}"
    )
    return res


async def _empty_dict() -> dict:
    return {}


# CNPJ no formato XX.XXX.XXX/XXXX-XX — a barra obrigatoria evita falso
# positivo (sequencia aleatoria de numeros). Rodape de site eh fonte confiavel.
_CNPJ_RE = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}/\d{4}-?\d{2})\b")


def _extrair_cnpj_de_textos(textos_por_url: dict) -> str:
    """
    Varre o texto das paginas visitadas (home, /sobre, rodape...) procurando
    um CNPJ MATEMATICAMENTE VALIDO (digitos verificadores conferem). Retorna
    os 14 digitos limpos do primeiro valido encontrado, ou "".
    """
    for texto in textos_por_url.values():
        for m in _CNPJ_RE.finditer(texto or ""):
            limpo = re.sub(r"\D", "", m.group(1))
            if len(limpo) == 14 and _validate_cnpj(limpo):
                return limpo
    return ""


def _extrair_contatos_de_textos(textos_por_url: dict) -> dict:
    """
    Roda os extratores de contato (email/telefone/whatsapp) em TODO o texto
    das paginas visitadas — pega o que estava em /contato, /sobre, rodape etc.
    Retorna {"emails": [...], "telefones": [...], "whatsapp_urls": [...]}.
    """
    texto_total = " ".join(t for t in textos_por_url.values() if t)
    ci = extract_all(texto_total)
    return {
        "emails": ci.emails,
        "telefones": ci.telefones,
        "whatsapp_urls": ci.whatsapp_urls,
    }
