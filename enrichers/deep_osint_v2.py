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
from enrichers.sources import cnpj_socios, instagram

log = logging.getLogger("enricher.deep_osint_v2")


@dataclass
class DeepOsintV2Result:
    sinais: list[InterestSignal] = field(default_factory=list)
    boost_score: int = 0
    instagram_url: str = ""
    facebook_url: str = ""
    site_url: str = ""
    linkedin_url: str = ""
    tiktok_url: str = ""
    instagram_bio: str = ""
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


# Links que NAO sao perfil de empresa (post, share, login, etc)
_SOCIAL_BLACKLIST = (
    "/p/", "/reel", "/explore", "/stories", "/tv/", "/sharer", "/share.php",
    "/plugins", "/dialog", "/login", "/accounts", "facebook.com/tr",
    "/hashtag/", "/events/", "/photo", "/posts/", "intent/", "/watch",
)


def _eh_perfil_social(url: str, plat: str) -> bool:
    low = url.lower()
    if any(b in low for b in _SOCIAL_BLACKLIST):
        return False
    if plat == "linkedin":
        return "/company/" in low or "/in/" in low
    if plat == "instagram":
        m = re.search(r"instagram\.com/([^/?#]+)", low)
        return bool(m) and len(m.group(1)) >= 2 and m.group(1) not in ("explore", "accounts")
    if plat == "facebook":
        m = re.search(r"facebook\.com/([^/?#]+)", low)
        return bool(m) and len(m.group(1)) >= 2 and m.group(1) not in ("pages", "groups", "events")
    if plat == "tiktok":
        return "tiktok.com/@" in low
    return False


def _extrair_social_links(trees: list) -> dict:
    """
    Varre o HTML das paginas visitadas procurando link pro Instagram,
    Facebook, LinkedIn, TikTok da empresa — geralmente no cabecalho,
    rodape ou aba de contato. Retorna {plataforma: url} do 1o valido.
    """
    plats = {
        "instagram": "instagram.com",
        "facebook": "facebook.com",
        "linkedin": "linkedin.com",
        "tiktok": "tiktok.com",
    }
    out: dict[str, str] = {}
    for tree in trees:
        if tree is None:
            continue
        for a in tree.css("a"):
            href = (a.attributes.get("href") or "").strip()
            if not href:
                continue
            # normaliza URL protocolo-relativo (//instagram.com/...) e sem esquema
            if href.startswith("//"):
                href = "https:" + href
            low = href.lower()
            if not low.startswith("http"):
                # href tipo "instagram.com/empresa" sem esquema
                if any(d in low for d in plats.values()):
                    href = "https://" + href.lstrip("/")
                    low = href.lower()
                else:
                    continue
            for plat, dom in plats.items():
                if plat in out or dom not in low:
                    continue
                if _eh_perfil_social(href, plat):
                    out[plat] = href.split("?")[0].rstrip("/")
        if len(out) == len(plats):
            break
    return out


async def coletar_site(client: httpx.AsyncClient, site_url: str) -> tuple[dict, dict]:
    """
    Visita a HOME, le os links REAIS do site, e segue ate 6 paginas
    internas relevantes (sobre, servicos, vagas, contato, blog...).
    Em CADA pagina tambem varre o HTML procurando perfil social
    (Instagram/Facebook/LinkedIn/TikTok) — fica facil achar na aba contato.
    Retorna ({url: texto}, {plataforma: url_do_perfil}).
    """
    if not site_url:
        return {}, {}
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    dominio = parsed.netloc.replace("www.", "")

    home_texto, home_tree = await visitar(client, base, retornar_tree=True)
    out: dict[str, str] = {}
    trees: list = [home_tree]
    if home_texto and len(home_texto) > 50:
        out[base] = home_texto
    if home_tree is None:
        return out, {}

    internos = _extrair_links_internos(home_tree, base, dominio, max_links=6)
    if internos:
        resultados = await asyncio.gather(
            *[visitar(client, u, retornar_tree=True) for u in internos]
        )
        for u, (t, tree) in zip(internos, resultados):
            trees.append(tree)
            if t and len(t) > 50:
                out[u] = t

    socials = _extrair_social_links(trees)
    return out, socials


# ============================================================================
# Busca de URLs (descoberta de linkedin/vagas). NUNCA rodamos detect() no
# resultado da busca — so usamos as URLs.
#
# DDG (html.duckduckgo.com) esta 100% bloqueado pros IPs do GitHub Actions:
# todo dork voltava vazio e ainda gastava ~10s de timeout/lead. Trocamos
# por Bing (tolera scraping de datacenter) + CIRCUIT BREAKER: depois de
# algumas falhas seguidas, desliga a busca pro resto da run — assim a run
# nao fica arrastando em timeout. O detector de sinais nao depende disso:
# ele roda no conteudo do SITE da empresa, que funciona normal.
# ============================================================================

# circuit breaker — estado do processo
_busca_falhas = 0
_busca_desativada = False
_BUSCA_MAX_FALHAS = 4


async def buscar_urls(client: httpx.AsyncClient, query: str, max_urls: int = 5,
                      filtro_dominio: str = "") -> list[str]:
    """
    Busca no Bing e retorna SO AS URLs dos resultados organicos.
    filtro_dominio: se setado, so retorna URLs que contem essa string.
    Circuit breaker: apos varias falhas seguidas, para de tentar (run rapida).
    """
    global _busca_falhas, _busca_desativada
    if _busca_desativada:
        return []
    try:
        r = await client.get(
            f"https://www.bing.com/search?q={quote(query)}&setlang=pt-br&cc=br",
            timeout=6.0,
        )
        if r.status_code != 200:
            _busca_falhas += 1
            if _busca_falhas >= _BUSCA_MAX_FALHAS:
                _busca_desativada = True
                log.warning("busca desativada (circuit breaker) — Bing bloqueado")
            return []
        tree = HTMLParser(r.text)
        urls: list[str] = []
        for link in tree.css("li.b_algo h2 a, li.b_algo a.tilk"):
            href = link.attributes.get("href", "") or ""
            if not href.startswith("http"):
                continue
            if "bing.com" in href or "microsoft.com/" in href:
                continue
            if filtro_dominio and filtro_dominio not in href:
                continue
            if href not in urls:
                urls.append(href)
            if len(urls) >= max_urls:
                break
        # achou resultado -> zera contador de falhas
        if urls:
            _busca_falhas = 0
        return urls
    except Exception as e:
        _busca_falhas += 1
        if _busca_falhas >= _BUSCA_MAX_FALHAS:
            _busca_desativada = True
            log.warning("busca desativada (circuit breaker) — Bing inacessivel")
        log.debug(f"buscar_urls falhou: {e}")
        return []


# alias de compat — codigo antigo chamava ddg_buscar_urls
ddg_buscar_urls = buscar_urls


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
        site_task = coletar_site(client, site_url) if site_url else _empty_site()
        outras_tasks = [visitar(client, u) for _, u in paginas_pra_visitar]
        site_result, *outras_textos = await asyncio.gather(site_task, *outras_tasks)

        # site_result = (textos_dict, socials_dict)
        site_dict, site_socials = site_result if isinstance(site_result, tuple) else ({}, {})
        for u, t in site_dict.items():
            res.textos_por_url[u] = t
        if site_dict:
            res.fontes_consultadas.append("site")

        # Perfis sociais achados no HTML do site (cabecalho/rodape/contato).
        # So preenche o que ainda nao tinhamos vindo do enrich/DDG.
        if site_socials.get("instagram") and not res.instagram_url:
            res.instagram_url = site_socials["instagram"]
        if site_socials.get("facebook") and not res.facebook_url:
            res.facebook_url = site_socials["facebook"]
        if site_socials.get("linkedin") and not res.linkedin_url:
            res.linkedin_url = site_socials["linkedin"]
        res.tiktok_url = site_socials.get("tiktok", "")

        # Fallback: se nem o enrich nem o site deram Instagram, procura via
        # DDG dork e valida pela bio (cruza nome/cidade).
        if not res.instagram_url:
            ig_url, ig_bio = await _descobrir_instagram(client, nome, cidade)
            if ig_url:
                res.instagram_url = ig_url
                res.instagram_bio = ig_bio
                res.fontes_consultadas.append("instagram_dork")

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
            # fallback via dorks — so se a busca ainda nao foi desativada
            # (senao gasta 4 timeouts a toa)
            if not cnpj_data and not _busca_desativada:
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
    # DEDUP por KEYWORD: cada keyword vira UM unico sinal, com n_ocorrencias
    # contando quantas vezes apareceu. Score conta a keyword 1x so.
    # ("agendamento online" 5x = 1 sinal "(5x)", nao 5 sinais.)
    sinais_por_kw: dict = {}   # (categoria, palavra_chave) -> InterestSignal
    categorias_aplicadas = set()
    boost_total = 0
    for url, texto in res.textos_por_url.items():
        sinais_da_pagina, _ = detect(texto)
        for s in sinais_da_pagina:
            chave = (s.categoria, s.palavra_chave)
            if chave in sinais_por_kw:
                sinais_por_kw[chave].n_ocorrencias += 1
                continue

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
            sinais_por_kw[chave] = s
            if s.categoria not in categorias_aplicadas:
                categorias_aplicadas.add(s.categoria)
                boost_total += s.boost

    res.sinais = list(sinais_por_kw.values())
    res.boost_score = min(boost_total, 100)

    log.info(
        f"  deep_osint_v2.2: {len(res.fontes_consultadas)} fontes "
        f"({len(res.textos_por_url)} paginas visitadas), "
        f"{len(res.sinais)} sinais ({len(categorias_aplicadas)} cat), boost +{res.boost_score}, "
        f"linkedin={bool(res.linkedin_url)} vagas={len(res.vaga_urls)} cnpj={bool(res.cnpj_data)}"
    )
    return res


async def _empty_site() -> tuple[dict, dict]:
    return {}, {}


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


async def _descobrir_instagram(client: httpx.AsyncClient, nome: str,
                               cidade: str) -> tuple[str, str]:
    """
    Fallback: quando o site nao tem link pro Instagram, procura o perfil
    via DDG dork (site:instagram.com "nome" "cidade") e pega a bio publica.
    Cross-check leve: confere se tokens do nome OU a cidade aparecem na
    bio/handle — assim sabemos que eh o perfil certo, nao um xara.
    Retorna (url, bio). url="" se nao achou ou se nao bateu nada.
    """
    try:
        dork = f'site:instagram.com "{nome}"' + (f' "{cidade}"' if cidade else "")
        urls = await buscar_urls(client, dork, max_urls=3, filtro_dominio="instagram.com/")
        # pega o 1o que parece perfil (nao /p/ /reel/ etc)
        url = ""
        for u in urls:
            if _eh_perfil_social(u, "instagram"):
                url = u.split("?")[0]
                break
        if not url:
            return "", ""
        perfil = await instagram.coletar_perfil(client, url)
        bio = perfil.get("bio", "") or ""
        alvo = (url + " " + bio).lower()
        nome_tokens = [t for t in re.findall(r"\w+", nome.lower()) if len(t) >= 4]
        cidade_tok = cidade.split()[0].lower() if cidade else ""
        bate = (
            any(t in alvo for t in nome_tokens)
            or (cidade_tok and cidade_tok in alvo)
        )
        # se nao tem token util pra checar, confia no dork (ja era nome+cidade)
        if bate or not nome_tokens:
            return url, bio
        log.debug(f"  instagram {url} nao cruzou com '{nome}' — descartado")
        return "", ""
    except Exception as e:
        log.debug(f"_descobrir_instagram falhou: {e}")
        return "", ""
