"""
Enricher PROFUNDO de leads GMaps.

Cascata de 6 tecnicas pra arrancar telefone de cada empresa:

1. Re-visita Google Maps na empresa especifica e clica no detail panel
   (onde telefone, site, endereco completo aparecem visiveis)
2. Visita o site oficial da empresa (extraido do GMaps), parseia:
   - tag <a href="tel:..."> e <a href="https://wa.me/...">
   - JSON-LD schema.org telephone
   - Footer e pagina /contato
3. DuckDuckGo dork: "<nome>" "<cidade>" whatsapp OR telefone
4. Busca Instagram via DDG: "<nome>" "<cidade>" site:instagram.com
   Depois visita o perfil e pega WhatsApp da bio
5. Busca Facebook via DDG: site:facebook.com
6. Regex em TODO texto coletado nas etapas anteriores (catch-all)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import httpx
from playwright.async_api import Page
from selectolax.parser import HTMLParser

from enrichers.extractors import ContactInfo, extract_all

log = logging.getLogger("enricher.gmaps_deep")


@dataclass
class GMapsDeepResult:
    telefone: str = ""
    whatsapp_url: str = ""
    email: str = ""
    site: str = ""
    endereco_completo: str = ""
    instagram_url: str = ""
    facebook_url: str = ""
    contatos_extras: ContactInfo = field(default_factory=lambda: ContactInfo([], [], [], []))
    fontes_usadas: list[str] = field(default_factory=list)


def _clean_site(url: str) -> str:
    """Remove redirects do Google Maps."""
    if not url:
        return ""
    if "google.com/url" in url or "google.com/maps" in url:
        # extrai query param q= ou url=
        m = re.search(r"[?&](?:q|url)=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1))
    return url


# ============================================================================
# TECNICA 1: Detail panel do Google Maps
# ============================================================================

async def buscar_no_gmaps(page: Page, nome: str, cidade: str) -> dict:
    """
    Re-busca a empresa especifica no Google Maps e clica no primeiro
    resultado. Retorna {telefone, site, endereco, instagram, facebook}.
    """
    query = f"{nome} {cidade}"
    url = f"https://www.google.com/maps/search/{quote(query)}"
    log.info(f"  [gmaps] {query}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.warning(f"  [gmaps] timeout: {e}")
        return {}

    await asyncio.sleep(3)

    # Tenta clicar no primeiro resultado (se a busca retornou lista)
    try:
        first = await page.query_selector("a.hfpxzc")
        if first:
            await first.click()
            await asyncio.sleep(2)
    except Exception:
        pass

    # Detail panel renderizado — extrai tudo
    data = await page.evaluate(
        """() => {
            const result = {};
            // Telefone: aparece em button com aria-label "Telefone: ..."
            const telBtn = document.querySelector('button[data-item-id*="phone:"], button[aria-label*="elefone"]');
            if (telBtn) {
                const txt = telBtn.getAttribute('aria-label') || telBtn.textContent || '';
                result.telefone = txt.replace(/^(Telefone:|Phone:)/i, '').trim();
            }
            // Tambem tentar via data-item-id
            const allButtons = Array.from(document.querySelectorAll('button[data-item-id]'));
            for (const b of allButtons) {
                const id = b.getAttribute('data-item-id') || '';
                if (id.startsWith('phone:tel:') && !result.telefone) {
                    result.telefone = id.replace('phone:tel:', '');
                }
            }
            // Site: link com data-item-id="authority"
            const siteEl = document.querySelector('a[data-item-id="authority"]');
            if (siteEl) result.site = siteEl.href;
            // Endereco
            const addrBtn = document.querySelector('button[data-item-id="address"]');
            if (addrBtn) {
                result.endereco = (addrBtn.getAttribute('aria-label') || '').replace(/^(Endereco:|Address:)/i, '').trim();
            }
            // Outros links (instagram, facebook, etc na sessao redes sociais)
            const allLinks = Array.from(document.querySelectorAll('a[href]'));
            for (const a of allLinks) {
                const h = a.href || '';
                if (!result.instagram && h.includes('instagram.com/')) result.instagram = h;
                if (!result.facebook && h.includes('facebook.com/')) result.facebook = h;
                if (!result.whatsapp && (h.includes('wa.me/') || h.includes('api.whatsapp.com'))) result.whatsapp = h;
            }
            return result;
        }"""
    )

    if data.get("site"):
        data["site"] = _clean_site(data["site"])

    log.info(f"  [gmaps] => tel={data.get('telefone')!r} site={data.get('site')!r}")
    return data


# ============================================================================
# TECNICA 2: Site oficial da empresa
# ============================================================================

async def visitar_site(site_url: str) -> tuple[ContactInfo, str]:
    """
    Visita o site, pega home + tenta /contato, /contact, /sobre.
    Retorna (contatos_achados, texto_completo_pra_regex).
    """
    if not site_url:
        return ContactInfo([], [], [], []), ""

    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url

    contatos_agregados = []
    texto_total = ""

    paths_to_try = ["", "/contato", "/contact", "/contatos", "/sobre", "/about", "/fale-conosco"]

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ProspectorBot/1.0)"},
    ) as client:
        for path in paths_to_try:
            try:
                parsed = urlparse(site_url)
                full_url = f"{parsed.scheme}://{parsed.netloc}{path}"
                r = await client.get(full_url)
                if r.status_code != 200:
                    continue
                html = r.text
            except Exception:
                continue

            # Extrai tudo
            tree = HTMLParser(html)

            # 2.1: <a href="tel:...">
            for a in tree.css("a[href^='tel:']"):
                tel = a.attributes.get("href", "").replace("tel:", "").strip()
                if tel:
                    texto_total += f" tel:{tel}"

            # 2.2: <a href="wa.me/...">
            for a in tree.css("a[href*='wa.me'], a[href*='api.whatsapp.com'], a[href*='whatsapp.com/send']"):
                wa = a.attributes.get("href", "")
                texto_total += f" {wa}"

            # 2.3: <a href="mailto:...">
            for a in tree.css("a[href^='mailto:']"):
                em = a.attributes.get("href", "").replace("mailto:", "").split("?")[0].strip()
                texto_total += f" {em}"

            # 2.4: JSON-LD schema.org (estruturado)
            for script in tree.css("script[type='application/ld+json']"):
                try:
                    payload = json.loads(script.text())
                    items = payload if isinstance(payload, list) else [payload]
                    for item in items:
                        if isinstance(item, dict):
                            tel = item.get("telephone") or ""
                            em = item.get("email") or ""
                            texto_total += f" {tel} {em}"
                except Exception:
                    pass

            # 2.5: regex em todo texto da pagina (catch-all)
            page_text = tree.text() or ""
            texto_total += " " + page_text[:30000]

            await asyncio.sleep(0.5)

            # Se ja achamos algum tel/email, podemos parar de visitar paths extras
            quick_check = extract_all(texto_total)
            if quick_check.telefones and quick_check.emails:
                break

    return extract_all(texto_total), texto_total[:5000]


# ============================================================================
# TECNICA 3 + 4 + 5: DDG dorks pra IG, FB, Whatsapp
# ============================================================================

async def ddg_dork_phone(nome: str, cidade: str) -> tuple[list[str], ContactInfo]:
    """Dorks especificos pra achar contato + perfis sociais."""
    queries = [
        f'"{nome}" "{cidade}" (whatsapp OR telefone OR contato OR "+55")',
        f'"{nome}" "{cidade}" site:instagram.com',
        f'"{nome}" "{cidade}" site:facebook.com',
    ]

    urls_found = []
    snippets = []

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    ) as client:
        for q in queries:
            try:
                r = await client.get(
                    f"https://html.duckduckgo.com/html/?q={quote(q)}&kl=br-pt"
                )
                if r.status_code != 200:
                    continue
                tree = HTMLParser(r.text)
                for result in tree.css(".result")[:5]:
                    link = result.css_first("a.result__a")
                    snip = result.css_first(".result__snippet")
                    if link:
                        href = link.attributes.get("href", "")
                        m = re.search(r"uddg=([^&]+)", href)
                        if m:
                            from urllib.parse import unquote
                            href = unquote(m.group(1))
                        if href.startswith("http"):
                            urls_found.append(href)
                    if snip:
                        snippets.append(snip.text(strip=True))
                await asyncio.sleep(1.5)
            except Exception as e:
                log.warning(f"  [ddg] {q}: {e}")

    contatos = extract_all(" ".join(snippets))
    return urls_found, contatos


# ============================================================================
# TECNICA 6: Visitar perfil Instagram/Facebook se achados
# ============================================================================

async def extrair_bio_instagram(client: httpx.AsyncClient, url: str) -> str:
    """Tenta puxar bio do Instagram (que so vezes tem WhatsApp)."""
    try:
        r = await client.get(url, timeout=10.0)
        if r.status_code != 200:
            return ""
        # Instagram embute meta tags com bio
        tree = HTMLParser(r.text)
        meta_desc = tree.css_first("meta[name='description']")
        if meta_desc:
            return meta_desc.attributes.get("content", "")
    except Exception:
        return ""
    return ""


# ============================================================================
# ORQUESTRADOR
# ============================================================================

async def enriquecer_gmaps_lead(page: Page, nome: str, cidade: str) -> GMapsDeepResult:
    """Pipeline completo: roda todas as tecnicas em sequencia."""
    result = GMapsDeepResult()

    # Tecnica 1: Re-busca no GMaps com clique no detail panel
    try:
        gmaps_data = await buscar_no_gmaps(page, nome, cidade)
        if gmaps_data.get("telefone"):
            result.telefone = gmaps_data["telefone"]
            result.fontes_usadas.append("gmaps_detail")
        if gmaps_data.get("site"):
            result.site = gmaps_data["site"]
        if gmaps_data.get("endereco"):
            result.endereco_completo = gmaps_data["endereco"]
        if gmaps_data.get("instagram"):
            result.instagram_url = gmaps_data["instagram"]
        if gmaps_data.get("facebook"):
            result.facebook_url = gmaps_data["facebook"]
        if gmaps_data.get("whatsapp"):
            result.whatsapp_url = gmaps_data["whatsapp"]
            result.fontes_usadas.append("gmaps_whatsapp_link")
    except Exception as e:
        log.warning(f"tecnica 1 falhou: {e}")

    # Tecnica 2: Site oficial (se gmaps deu url)
    if result.site and not result.telefone:
        try:
            site_contatos, _ = await visitar_site(result.site)
            if site_contatos.telefones:
                result.telefone = site_contatos.telefones[0]
                result.fontes_usadas.append("site_oficial")
            if site_contatos.emails and not result.email:
                result.email = site_contatos.emails[0]
            if site_contatos.whatsapp_urls and not result.whatsapp_url:
                result.whatsapp_url = site_contatos.whatsapp_urls[0]
        except Exception as e:
            log.warning(f"tecnica 2 falhou: {e}")

    # Tecnica 3-5: DDG dorks (so se ainda nao achamos tel)
    if not result.telefone:
        try:
            urls, contatos_ddg = await ddg_dork_phone(nome, cidade)
            if contatos_ddg.telefones:
                result.telefone = contatos_ddg.telefones[0]
                result.fontes_usadas.append("ddg_dork")
            if contatos_ddg.emails and not result.email:
                result.email = contatos_ddg.emails[0]
            result.contatos_extras = contatos_ddg

            # Pega Instagram/Facebook das URLs encontradas
            for u in urls:
                if "instagram.com" in u and not result.instagram_url:
                    result.instagram_url = u
                if "facebook.com" in u and not result.facebook_url:
                    result.facebook_url = u
        except Exception as e:
            log.warning(f"tecnica 3 falhou: {e}")

    # Tecnica 6: Visita bio Instagram (se achado)
    if result.instagram_url and not result.telefone:
        try:
            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
                bio = await extrair_bio_instagram(client, result.instagram_url)
                if bio:
                    contatos_bio = extract_all(bio)
                    if contatos_bio.telefones:
                        result.telefone = contatos_bio.telefones[0]
                        result.fontes_usadas.append("instagram_bio")
        except Exception as e:
            log.warning(f"tecnica 6 falhou: {e}")

    log.info(
        f"    => tel={result.telefone!r} site={bool(result.site)} "
        f"ig={bool(result.instagram_url)} fb={bool(result.facebook_url)} "
        f"fontes={result.fontes_usadas}"
    )
    return result
