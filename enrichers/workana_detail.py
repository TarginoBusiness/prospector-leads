"""
Enriquece um lead do Workana visitando a pagina de detalhe do projeto.

Coleta:
  - Texto COMPLETO da descricao (lista so mostra preview)
  - Nome/username do cliente
  - Cidade/estado do cliente (Workana mostra publicamente)
  - Pais
  - Idioma, recursos requeridos
  - Roda extractors regex em todo texto coletado
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.async_api import Page

from enrichers.extractors import ContactInfo, extract_all

log = logging.getLogger("enricher.workana_detail")


@dataclass
class WorkanaDetail:
    descricao_completa: str = ""
    cliente_nome: str = ""
    cliente_username: str = ""
    cliente_cidade: str = ""
    cliente_estado: str = ""
    cliente_pais: str = ""
    skills: list[str] = field(default_factory=list)
    contatos: ContactInfo = field(default_factory=lambda: ContactInfo([], [], [], []))


async def enriquecer(page: Page, project_url: str) -> WorkanaDetail | None:
    """Visita pagina do projeto e extrai todos os dados publicos disponiveis."""
    log.info(f"  enriquecendo {project_url}")
    try:
        await page.goto(project_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.warning(f"  timeout: {e}")
        return None

    # Espera o titulo aparecer
    try:
        await page.wait_for_selector("h1, .project-title", timeout=10000)
    except Exception:
        log.warning("  pagina nao renderizou (selector h1 nao apareceu)")
        return None

    detail = WorkanaDetail()

    # Descricao completa (varios seletores possiveis ao longo do tempo)
    detail.descricao_completa = await page.evaluate(
        """() => {
            const sels = [
                '.project-description',
                '.description',
                '.project-details .body',
                '[class*="description"]',
                'article .body',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.textContent.trim().length > 30) return el.textContent.trim();
            }
            return '';
        }"""
    )

    # Bloco do cliente (nome, cidade, etc)
    cliente_info = await page.evaluate(
        """() => {
            const block = document.querySelector('.user-info, .client-info, [class*="client"]');
            if (!block) return {};
            const text = block.textContent || '';
            // username pode estar em link /usuarios/<id>
            const link = block.querySelector("a[href*='/usuarios/'], a[href*='/freelancers/']");
            return {
                nome: (block.querySelector('h2, h3, .name, [class*="name"]') || {}).textContent?.trim() || '',
                username: link ? (link.getAttribute('href') || '').split('/').pop() : '',
                texto_completo: text.trim()
            };
        }"""
    )

    detail.cliente_nome = cliente_info.get("nome", "")
    detail.cliente_username = cliente_info.get("username", "")
    cliente_texto = cliente_info.get("texto_completo", "")

    # Cidade/estado/pais — Workana mostra "Cidade, Pais"
    # Tenta extrair via regex do texto do cliente
    import re

    # Padrao tipico: "Sao Luis, Brasil" ou "Sao Paulo - SP, Brasil"
    loc_match = re.search(
        r"([A-ZÀ-ÚŸ][a-zà-ÿA-Z\s]+)(?:\s*[-,]\s*([A-Z]{2}))?,?\s*(Brasil|Argentina|Mexico|Espanha|Portugal)",
        cliente_texto,
    )
    if loc_match:
        detail.cliente_cidade = (loc_match.group(1) or "").strip()
        detail.cliente_estado = (loc_match.group(2) or "").strip()
        detail.cliente_pais = (loc_match.group(3) or "").strip()

    # Skills/tags
    detail.skills = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.skills a, .tags a, [class*="skill"] a'))
            .map(a => (a.textContent || '').trim())
            .filter(s => s.length > 0 && s.length < 40)
            .slice(0, 30)"""
    )

    # Pega HTML completo + texto bruto e roda extractors em CIMA de tudo
    full_text = await page.evaluate(
        "() => document.body.innerText || document.body.textContent || ''"
    )
    detail.contatos = extract_all(full_text)

    log.info(
        f"  cliente='{detail.cliente_nome}' cidade='{detail.cliente_cidade}' "
        f"tels={detail.contatos.telefones} emails={len(detail.contatos.emails)} "
        f"cnpjs={detail.contatos.cnpjs}"
    )
    return detail
