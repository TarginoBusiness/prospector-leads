"""
Dashboard de leads — Streamlit (com Fragments pra live update silencioso).

Roda local com:  streamlit run dashboard/app.py
Roda em prod:    Streamlit Community Cloud.

ARQUITETURA:
- O painel TOP (metricas, recém-chegados, progresso) usa @st.fragment(run_every=5)
  → atualiza sozinho a cada 5s SEM rerodar a pagina inteira.
- Filtros + tabela ficam FORA do fragment → so atualizam quando voce interage.
- Resultado: zero flicker, zero reset de filtros, zero 'Running...' piscando.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, JsCode

from db.connection import get_conn

st.set_page_config(
    page_title="Prospector Leads",
    page_icon="🔥",
    layout="wide",
)


if "DATABASE_URL" in st.secrets:
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]

GH_TOKEN = st.secrets.get("GITHUB_TOKEN", "")
GH_REPO = "TarginoBusiness/prospector-leads"


# CSS forte: mata o dim, mata indicadores, animacao na progress bar
st.markdown(
    """
    <style>
    @keyframes flash-yellow {
        0%   { background-color: #fff59d; }
        50%  { background-color: #ffeb3b; }
        100% { background-color: transparent; }
    }
    .new-lead-card {
        background: linear-gradient(90deg, #fff59d 0%, #ffffff 70%);
        border-left: 4px solid #fbc02d;
        padding: 10px 14px;
        margin: 6px 0;
        border-radius: 6px;
        animation: flash-yellow 3s ease-out;
    }

    /* ====== Esconde TODOS os indicadores de carregamento ====== */
    div[data-testid="stStatusWidget"],
    div[data-testid="stDecoration"],
    div[data-testid="stToolbar"],
    .stSpinner,
    [data-testid="stHeader"] [data-testid="stStatusWidget"],
    [class*="StatusWidget"],
    [class*="status-widget"] {
        display: none !important;
        visibility: hidden !important;
    }
    header[data-testid="stHeader"] {
        background: transparent;
        height: 0 !important;
    }

    /* ====== MATA O DIM (overlay escuro durante refresh do fragment) ====== */
    /* Streamlit aplica data-stale="true" + opacity baixa quando o fragment
       esta rerodando. Forcamos opacity:1 sempre. */
    [data-stale="true"],
    [data-stale="false"] {
        opacity: 1 !important;
        transition: none !important;
    }
    .stApp [class*="running"],
    .stApp [class*="stale"] {
        opacity: 1 !important;
        filter: none !important;
    }
    /* Mata todas as transicoes de opacidade do streamlit */
    div[data-testid="stAppViewContainer"] *,
    div[data-testid="stMain"] * {
        transition-property: none !important;
    }

    /* ====== Progress bar CUSTOM bonita com animacao stripes ====== */
    .custom-progress-wrapper {
        background: #1a1a1a;
        border-radius: 10px;
        height: 28px;
        padding: 3px;
        margin: 12px 0;
        box-shadow: inset 0 1px 3px rgba(0,0,0,0.5);
    }
    .custom-progress-bar {
        height: 100%;
        border-radius: 8px;
        background: linear-gradient(90deg, #ff4b4b, #ff8c42, #ffa726);
        background-size: 200% 200%;
        position: relative;
        overflow: hidden;
        animation: gradient-shift 3s ease infinite;
        transition: width 0.6s cubic-bezier(0.25, 0.46, 0.45, 0.94);
        box-shadow: 0 0 8px rgba(255, 75, 75, 0.5);
    }
    .custom-progress-bar::after {
        content: "";
        position: absolute;
        inset: 0;
        background-image: linear-gradient(
            45deg,
            rgba(255,255,255,0.2) 25%, transparent 25%,
            transparent 50%, rgba(255,255,255,0.2) 50%,
            rgba(255,255,255,0.2) 75%, transparent 75%, transparent
        );
        background-size: 28px 28px;
        animation: progress-stripes 1.2s linear infinite;
        border-radius: 8px;
    }
    .custom-progress-label {
        display: flex;
        justify-content: space-between;
        margin: 6px 4px;
        color: #e0e0e0;
        font-size: 14px;
        font-weight: 500;
    }
    @keyframes gradient-shift {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    @keyframes progress-stripes {
        from { background-position: 28px 0; }
        to   { background-position: 0 0; }
    }

    /* ============ TABELA HTML PURA — fluida e leve ============ */
    .leads-table-container {
        height: 650px;
        overflow-y: auto;
        border: 1px solid #2a2a2a;
        border-radius: 6px;
        background: #0e1117;
    }
    .leads-html-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .leads-html-table thead th {
        position: sticky;
        top: 0;
        background: #1a1a1a;
        color: #aaa;
        font-size: 10.5px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        padding: 8px 8px;
        border-bottom: 2px solid #444;
        border-right: 1px solid #2a2a2a;
        text-align: left;
        z-index: 2;
    }
    .leads-html-table thead th:last-child { border-right: none; }
    .leads-html-table tbody td {
        padding: 6px 8px;
        border-bottom: 1px solid #232323;
        border-right: 1px solid #232323;
        vertical-align: middle;
        color: #e0e0e0;
    }
    .leads-html-table tbody td:last-child { border-right: none; }
    .leads-html-table tbody tr:hover { background: rgba(255, 75, 75, 0.05); }
    .ficha-btn {
        display: inline-block;
        background: #ff4b4b;
        color: white !important;
        text-decoration: none !important;
        padding: 4px 10px;
        border-radius: 5px;
        font-size: 13px;
        font-weight: 600;
        line-height: 1;
        transition: background 0.15s;
    }
    .ficha-btn:hover { background: #ff8c42; }
    .score-bar-outer {
        background: #1a1a1a;
        border-radius: 4px;
        height: 18px;
        position: relative;
        overflow: hidden;
        min-width: 80px;
    }
    .score-bar-fill { height: 100%; }
    .score-bar-label {
        position: absolute; top: 0; left: 0; right: 0;
        text-align: center; line-height: 18px;
        font-size: 11px; font-weight: 600; color: #fff;
        text-shadow: 0 0 3px rgba(0,0,0,0.7);
    }
    .sinal-count { color: #4caf50; font-weight: 600; }
    .sinal-cats { color: #888; font-size: 10px; line-height: 1.2; display: block; margin-top: 2px; }
    .tel-link { color: #4caf50; text-decoration: none; }
    .tel-link:hover { text-decoration: underline; }
    .empty-cell { color: #555; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=30, show_spinner=False)
def load_lead_full(lead_id: int) -> dict:
    """Carrega ficha completa do lead — usado pelo modal."""
    with get_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, source, source_url, nome, telefone, email, cnpj, nicho,
                   cidade_tag, cidade, estado, score_temperatura, score_breakdown,
                   status, raw_payload, notes, falso_lead, falso_lead_motivo,
                   first_seen_at, last_seen_at, last_contacted_at, last_deep_dive_at
            FROM leads WHERE id = %s
            """,
            (lead_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        cols = [d.name for d in cur.description]
        lead = dict(zip(cols, row))

        cur.execute(
            """
            SELECT categoria, palavra_chave, trecho_texto, source_url, boost,
                   n_ocorrencias, captured_at
            FROM intent_signals WHERE lead_id = %s ORDER BY captured_at DESC
            """,
            (lead_id,),
        )
        cols2 = [d.name for d in cur.description]
        lead["intent_signals"] = [dict(zip(cols2, r)) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT plataforma, url, fonte, confianca, discovered_at
            FROM social_profiles WHERE lead_id = %s ORDER BY discovered_at DESC
            """,
            (lead_id,),
        )
        cols3 = [d.name for d in cur.description]
        lead["social_profiles"] = [dict(zip(cols3, r)) for r in cur.fetchall()]

    return lead


def _wa_url(tel):
    if not tel:
        return None
    clean = "".join(c for c in str(tel) if c.isdigit() or c == "+")
    return f"https://wa.me/{clean.lstrip('+')}"


# Categorias de DEMANDA EXPLICITA — peso maximo no score. A empresa esta
# literalmente contratando pra atendimento OU pediu o que vendemos.
# Card AMARELO na ficha (pisca atencao).
CATEGORIAS_DEMANDA = {
    "vaga_atendimento",
    "necessidade_explicita",
    "vaga_tech",
    "dor_explicita",
}


def _frag_encode(val: str) -> str:
    """Percent-encode pra Chrome Text Fragment — encoda tambem - , & que tem
    significado especial no parser do fragment."""
    from urllib.parse import quote
    return quote(val, safe="").replace("-", "%2D").replace("&", "%26")


def _text_fragment_url(base_url: str, trecho: str, keyword: str = "") -> str:
    """
    Monta URL com Chrome Text Fragment que DESTACA e SCROLLA ate a
    mencao — igual quando voce aperta Ctrl+F.

    ESTRATEGIA (do mais confiavel pro menos):
      1. A keyword EXATAMENTE como aparece na pagina (com acento/maiuscula).
         Curta, exata, num unico text-node → casa quase sempre. Eh
         literalmente o comportamento do Ctrl+F.
      2. Forma de RANGE (text=inicio,fim) — robusta a <span>/<br> no meio.
      3. So a URL, sem fragment.

    Chrome casa case-insensitive mas eh SENSIVEL a acento — por isso a
    keyword tem que vir do texto da pagina, nao da forma normalizada
    do config.
    """
    from textutil import keyword_como_na_pagina

    if not base_url:
        return ""
    base_url = base_url.split("#")[0]  # tira fragment pre-existente
    trecho = (trecho or "").strip()

    # 1. keyword exata como na pagina
    kw_pagina = keyword_como_na_pagina(trecho, keyword)
    if kw_pagina:
        return f"{base_url}#:~:text={_frag_encode(kw_pagina)}"

    # 2. range com inicio/fim do trecho
    limpo = re.sub(r"[<>{}|\\#]", " ", trecho)
    limpo = re.sub(r"\s+", " ", limpo).strip()
    if limpo:
        palavras = limpo.split(" ")
        if len(palavras) <= 6:
            return f"{base_url}#:~:text={_frag_encode(limpo)}"
        start = " ".join(palavras[:4])
        end = " ".join(palavras[-3:])
        return f"{base_url}#:~:text={_frag_encode(start)},{_frag_encode(end)}"

    # 3. fallback: keyword crua (pode nao casar se a pagina tem acento)
    if keyword:
        kw = re.sub(r"\s+", " ", keyword).strip()
        if kw:
            return f"{base_url}#:~:text={_frag_encode(kw)}"
    return base_url


@st.dialog("📋 Ficha completa do lead", width="large")
def show_lead_dialog(lead_id: int) -> None:
    # Remove overlay de loading (injetado pelo btn_renderer no AgGrid)
    st.markdown(
        """<script>
        (function() {
            try {
                const tw = window.top || window.parent;
                const ov = tw.document.getElementById('loading-ficha-overlay');
                if (ov) ov.remove();
            } catch (e) {}
        })();
        </script>""",
        unsafe_allow_html=True,
    )
    lead = load_lead_full(lead_id)
    if not lead:
        st.error("Lead não encontrado.")
        return

    # Cabecalho — mostra raw + relativo
    raw_score = int(lead["score_temperatura"])
    # Calcula relativo na hora baseado em todos os leads
    try:
        with get_conn() as c, c.cursor() as cur:
            cur.execute("SELECT MIN(score_temperatura), MAX(score_temperatura) FROM leads")
            mn, mx = cur.fetchone()
            mn = mn or 0
            mx = mx or 100
            rng = max(mx - mn, 1)
            rel_score = int(round(10 + 90 * (raw_score - mn) / rng))
    except Exception:
        rel_score = raw_score

    score_emoji = "🔥" if rel_score >= 90 else "🟡" if rel_score >= 60 else "🧊"
    st.markdown(
        f"### {score_emoji} {lead['nome'] or '(sem nome)'} — "
        f"**{rel_score}%** no ranking <span style='color:#888;font-size:14px;'>(raw: {raw_score} pts)</span>",
        unsafe_allow_html=True,
    )

    # Banner vermelho se for FALSO LEAD
    if lead.get("falso_lead"):
        st.markdown(
            f"""<div style="background:#3a1414;border-left:4px solid #c62828;padding:8px 12px;
            margin:4px 0 8px;border-radius:4px;color:#ff8a80;font-weight:600;">
            🚫 FALSO LEAD — {lead.get('falso_lead_motivo') or 'motivo não informado'}
            </div>""",
            unsafe_allow_html=True,
        )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**📞 Telefone:** " + (lead["telefone"] or "—"))
        if lead["telefone"]:
            wa = _wa_url(lead["telefone"])
            st.markdown(f"[💬 Abrir no WhatsApp]({wa})")
        st.markdown("**📧 Email:** " + (lead["email"] or "—"))
        st.markdown("**🏢 CNPJ:** " + (lead["cnpj"] or "—"))
    with col2:
        st.markdown(f"**📍 Cidade:** {lead['cidade_tag'] or '—'} ({lead['cidade'] or 'sem detalhe'})")
        st.markdown(f"**🏷️ Nicho:** {lead['nicho'] or '—'}")
        st.markdown(f"**🌐 Fonte:** `{lead['source']}`")
        st.markdown(f"**📊 Status:** `{lead['status']}`")

    # ====== parse raw_payload (usado por varios blocos abaixo) ======
    raw_payload = lead.get("raw_payload") or {}
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except Exception:
            raw_payload = {}
    dov2 = raw_payload.get("deep_osint_v2") or {}
    deep_enrich = raw_payload.get("deep_enrich") or {}
    gmaps_rp = raw_payload.get("gmaps") or {}
    cnpj_data = dov2.get("cnpj_data") or {}

    # ====== 📇 Contatos & Perfis (tudo que conseguimos cruzar) ======
    st.divider()
    st.markdown("#### 📇 Contatos & Perfis")

    socials = lead.get("social_profiles") or []
    def _sp_url(plat_keys, url_substr=None):
        """Acha a URL de um perfil em social_profiles por plataforma ou substring."""
        for sp in socials:
            plat = (sp.get("plataforma") or "").lower()
            url = sp.get("url") or ""
            if plat in plat_keys:
                return url
            if url_substr and url_substr in url.lower():
                return url
        return ""

    endereco = (
        (cnpj_data.get("endereco_completo") or "").strip()
        or (deep_enrich.get("endereco") or "").strip()
        or (gmaps_rp.get("endereco") or "").strip()
    )
    responsavel = ""
    if cnpj_data.get("socios"):
        responsavel = cnpj_data["socios"][0].get("nome") or ""

    insta = _sp_url({"instagram"}, "instagram.com") or deep_enrich.get("instagram", "")
    face = _sp_url({"facebook"}, "facebook.com") or deep_enrich.get("facebook", "")
    linkedin = _sp_url({"linkedin"}, "linkedin.com") or dov2.get("linkedin_url", "")
    site = _sp_url({"site"}, None) or deep_enrich.get("site", "")
    getninjas = _sp_url(set(), "getninjas")
    workana = _sp_url(set(), "workana")

    cc1, cc2 = st.columns(2)
    with cc1:
        st.markdown(f"**📧 Email:** {lead['email'] or '—'}")
        st.markdown(f"**🏢 CNPJ:** {lead['cnpj'] or cnpj_data.get('cnpj') or '—'}")
        st.markdown(f"**👤 Responsável:** {responsavel or '—'}")
        st.markdown(f"**📌 Endereço:** {endereco or '—'}")
    with cc2:
        def _link(label, url):
            return f"{label} [abrir]({url})" if url else f"{label} —"
        st.markdown("**📸 Instagram:** " + ("[abrir](%s)" % insta if insta else "—"))
        st.markdown("**📘 Facebook:** " + ("[abrir](%s)" % face if face else "—"))
        st.markdown("**💼 LinkedIn:** " + ("[abrir](%s)" % linkedin if linkedin else "—"))
        st.markdown("**🌐 Site:** " + ("[abrir](%s)" % site if site else "—"))
    if getninjas or workana:
        extra = []
        if getninjas:
            extra.append(f"[GetNinjas]({getninjas})")
        if workana:
            extra.append(f"[Workana]({workana})")
        st.markdown("**🧰 Plataformas de serviço:** " + " · ".join(extra))

    ig_bio = (dov2.get("instagram_bio") or "").strip()
    if ig_bio:
        st.caption(f"📸 Bio do Instagram: _{ig_bio[:280]}_")

    # Contatos extras colhidos do site (/contato, /sobre, rodapé)
    contatos = dov2.get("contatos") or {}
    extra_emails = [e for e in (contatos.get("emails") or []) if e != lead.get("email")]
    extra_tels = [t for t in (contatos.get("telefones") or []) if t != lead.get("telefone")]
    if extra_emails or extra_tels:
        partes = []
        if extra_emails:
            partes.append("📧 " + ", ".join(extra_emails[:4]))
        if extra_tels:
            partes.append("📞 " + ", ".join(extra_tels[:4]))
        st.caption("Outros contatos colhidos do site: " + " · ".join(partes))

    if cnpj_data:
        st.divider()
        st.markdown("#### 🏢 Dados oficiais (Receita Federal · BrasilAPI)")
        rc1, rc2 = st.columns(2)
        with rc1:
            st.markdown(f"**Razão social:** {cnpj_data.get('razao_social') or '—'}")
            st.markdown(f"**Nome fantasia:** {cnpj_data.get('nome_fantasia') or '—'}")
            st.markdown(f"**CNPJ:** {cnpj_data.get('cnpj') or lead.get('cnpj') or '—'}")
            st.markdown(f"**Situação:** {cnpj_data.get('situacao') or '—'}")
        with rc2:
            st.markdown(f"**📧 Email (Receita):** {cnpj_data.get('email') or '—'}")
            st.markdown(f"**📞 Telefone (Receita):** {cnpj_data.get('telefone_receita') or '—'}")
            st.markdown(f"**Atividade (CNAE):** {cnpj_data.get('cnae_principal') or '—'}")
            st.markdown(f"**Abertura:** {cnpj_data.get('data_abertura') or '—'}")
        socios = cnpj_data.get("socios") or []
        if socios:
            st.markdown("**👤 Responsáveis / quadro societário:**")
            for so in socios:
                qual = so.get("qualificacao") or ""
                st.markdown(f"- **{so.get('nome') or '—'}**" + (f" — _{qual}_" if qual else ""))
        if cnpj_data.get("match_fraco"):
            st.caption("⚠️ Match de endereço fraco — confira se o CNPJ é mesmo dessa empresa.")

    st.divider()

    # Score breakdown
    st.markdown("#### 🧮 Como o score foi calculado")
    breakdown = lead.get("score_breakdown") or {}
    if isinstance(breakdown, str):
        try:
            breakdown = json.loads(breakdown)
        except Exception:
            breakdown = {}
    if breakdown:
        for k, v in breakdown.items():
            sign = "➕" if int(v) > 0 else "➖"
            st.markdown(f"- {sign} **{k}**: {v}")
    else:
        st.caption("Sem breakdown registrado.")

    st.divider()

    # Sinais de interesse
    sinais = lead.get("intent_signals") or []
    interest = [s for s in sinais if (s["categoria"] or "").startswith("interest_")]
    intent_actual = [s for s in sinais if not (s["categoria"] or "").startswith("interest_")]

    if interest:
        st.markdown("#### 🎯 Sinais de interesse detectados (Deep OSINT)")
        st.caption("👆 Clica em **🔗 Abrir fonte** pra ver a página (Chrome destaca a menção em amarelo, igual Ctrl+F). "
                   "🔥 Cards **amarelos** = demanda explícita (contratando atendimento / pediram o que vendemos) = peso máximo.")
        # demanda explicita primeiro — eh o sinal mais forte
        interest_ord = sorted(
            interest,
            key=lambda s: (0 if (s["categoria"] or "").replace("interest_", "") in CATEGORIAS_DEMANDA else 1,
                           -int(s.get("boost") or 0)),
        )
        for s in interest_ord:
            cat = s["categoria"].replace("interest_", "")
            is_demanda = cat in CATEGORIAS_DEMANDA
            base_url = s.get("source_url") or ""
            trecho = (s.get("trecho_texto") or "").strip()
            # URL com Chrome Text Fragment (keyword exata da página → destaca + scrolla)
            href = _text_fragment_url(base_url, trecho, s.get("palavra_chave", ""))

            preview = trecho[:200] if trecho else "(sem trecho)"
            n_ocor = int(s.get("n_ocorrencias") or 1)
            ocor_txt = f" ({n_ocor}x)" if n_ocor > 1 else ""

            # Paleta: AMARELO pra demanda explícita, verde pro resto
            if is_demanda:
                card_bg, card_border, txt_col, code_bg = "#3a3413", "#ffeb3b", "#ffe082", "#2a2607"
                btn_bg = "#f9a825"
                emoji, label = "🔥", f"DEMANDA EXPLÍCITA · {cat}"
            else:
                card_bg, card_border, txt_col, code_bg = "#1a3a1a", "#4caf50", "#81c784", "#0a2a0a"
                btn_bg = "#2e7d32"
                emoji, label = "🎯", cat

            link_html = (
                f'<a href="{href}" target="_blank" style="background:{btn_bg};color:#1a1a1a;text-decoration:none;'
                f'padding:3px 9px;border-radius:3px;font-size:11px;font-weight:700;display:inline-block;margin-left:8px;">'
                f'🔗 Abrir fonte com destaque 🟡'
                f'</a>'
                if href else
                '<span style="color:#666;font-size:11px;margin-left:8px;">(sem URL da fonte)</span>'
            )

            st.markdown(
                f"""<div style="background:{card_bg};border-left:4px solid {card_border};padding:10px 12px;margin:6px 0;border-radius:4px;">
                <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
                  <strong style="color:{txt_col};">{emoji} {label}</strong>
                  <span style="color:#aaa;">·</span>
                  <span style="font-size:12px;color:#aaa;">keyword:</span>
                  <code style="background:{code_bg};color:{txt_col};padding:1px 5px;border-radius:3px;">{s['palavra_chave']}{ocor_txt}</code>
                  <code style="background:{code_bg};color:{txt_col};padding:1px 5px;border-radius:3px;">+{s['boost']}</code>
                  {link_html}
                </div>
                <div style="color:#bbb;font-size:13px;margin-top:6px;font-style:italic;">"{preview}"</div>
                </div>""",
                unsafe_allow_html=True,
            )

    if intent_actual:
        st.markdown("#### 🔥 Intent declarado (post pedindo serviço)")
        st.caption("👆 Clica em **🔗 Abrir post** pra ver a página original.")
        for s in intent_actual:
            base_url = s.get("source_url") or ""
            trecho = (s.get("trecho_texto") or "").strip()
            href = _text_fragment_url(base_url, trecho, s.get("palavra_chave", ""))

            preview = trecho[:200] if trecho else "(sem trecho)"
            link_html = (
                f'<a href="{href}" target="_blank" style="background:#c62828;color:white;text-decoration:none;'
                f'padding:3px 9px;border-radius:3px;font-size:11px;font-weight:600;display:inline-block;margin-left:8px;">'
                f'🔗 Abrir post com destaque 🟡'
                f'</a>'
                if href else
                '<span style="color:#666;font-size:11px;margin-left:8px;">(sem URL da fonte)</span>'
            )

            st.markdown(
                f"""<div style="background:#3a1a1a;border-left:3px solid #f44336;padding:10px 12px;margin:6px 0;border-radius:4px;">
                <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
                  <strong style="color:#ef9a9a;">🔥 {s['categoria']}</strong>
                  <span style="color:#aaa;">·</span>
                  <span style="font-size:12px;color:#aaa;">keyword:</span>
                  <code style="background:#2a0a0a;color:#ef9a9a;padding:1px 5px;border-radius:3px;">{s['palavra_chave']}</code>
                  <code style="background:#2a0a0a;color:#ef9a9a;padding:1px 5px;border-radius:3px;">+{s['boost']}</code>
                  {link_html}
                </div>
                <div style="color:#999;font-size:13px;margin-top:6px;font-style:italic;">"{preview}"</div>
                </div>""",
                unsafe_allow_html=True,
            )

    if not sinais:
        st.caption("⚠️ Nenhum sinal de intent/interesse detectado ainda. Use 'Aprofundar OSINT' pra investigar.")

    st.divider()

    # Perfis sociais — detalhe (confiança + fonte). O resumo já está em "Contatos & Perfis".
    if socials:
        with st.expander(f"🌐 Perfis sociais cruzados ({len(socials)}) — detalhe", expanded=False):
            for sp in socials:
                st.markdown(f"- **{sp['plataforma']}** · [abrir]({sp['url']}) · confiança {sp['confianca']}% · via `{sp['fonte']}`")

    # rp_for_socios mantido por compat com os blocos abaixo
    rp_for_socios = raw_payload

    # Reclame Aqui (pain points)
    reclame = (rp_for_socios.get("deep_osint_v2") or {}).get("reclame_aqui") or {}
    if reclame.get("n_resultados"):
        st.markdown("#### 😡 Reclame Aqui (pain points)")
        st.warning(
            f"**{reclame['n_resultados']} reclamações encontradas** — pista forte de que precisa de automação."
        )
        for u in reclame.get("urls_top3", []):
            st.markdown(f"- [{u}]({u})")

    # News mentions
    news = (rp_for_socios.get("deep_osint_v2") or {}).get("news_mentions") or {}
    if news.get("n_urls"):
        st.markdown("#### 📰 Menções em portais de notícia")
        for u in news.get("urls_top5", []):
            st.markdown(f"- [{u}]({u})")

    # Reverse phone (pra confirmar identidade)
    rev = (rp_for_socios.get("deep_osint_v2") or {}).get("reverse_phone") or {}
    if rev.get("nomes_encontrados"):
        st.markdown("#### 📞 Quem mais usa esse telefone? (reverse lookup)")
        for nome_alt in rev["nomes_encontrados"]:
            st.markdown(f"- {nome_alt}")

    # Raw payload (para auditoria)
    with st.expander("🔍 Dados brutos coletados (raw_payload)"):
        rp = lead.get("raw_payload") or {}
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except Exception:
                rp = {}
        st.json(rp, expanded=False)

    # Datas
    st.markdown("#### 🕐 Timeline")
    st.caption(
        f"Visto 1ª vez: {lead['first_seen_at']} · "
        f"Atualizado: {lead['last_seen_at']} · "
        f"OSINT: {lead['last_deep_dive_at'] or 'nunca'} · "
        f"Contatado: {lead['last_contacted_at'] or 'nunca'}"
    )

    # Acoes
    st.markdown("#### ⚡ Ações")
    aco1, aco2, aco3 = st.columns(3)
    with aco1:
        if st.button("✅ Marcar contatado", use_container_width=True, key=f"mark_contacted_{lead_id}"):
            with get_conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET status='contatado', last_contacted_at=NOW() WHERE id=%s",
                    (lead_id,),
                )
            load_lead_full.clear()
            load_leads_full.clear()
            st.success("Lead marcado como contatado.")
            st.rerun()
    with aco2:
        if st.button("❌ Descartar", use_container_width=True, key=f"discard_{lead_id}"):
            with get_conn() as c, c.cursor() as cur:
                cur.execute("UPDATE leads SET status='descartado' WHERE id=%s", (lead_id,))
            load_lead_full.clear()
            load_leads_full.clear()
            st.success("Lead descartado.")
            st.rerun()
    with aco3:
        if st.button("🔄 Re-aprofundar OSINT", use_container_width=True, key=f"redeep_{lead_id}"):
            with get_conn() as c, c.cursor() as cur:
                cur.execute("UPDATE leads SET last_deep_dive_at=NULL WHERE id=%s", (lead_id,))
            ok, msg = trigger_workflow("deep-osint.yml", {"limit": "5"})
            if ok:
                st.success("Re-OSINT disparado! Vai entrar na próxima fila.")
            else:
                st.error(msg)

    # ====== Falso Lead (concorrente / ja tem servico) — linha fica vermelha ======
    st.markdown("##### 🚫 Falso Lead")
    if lead.get("falso_lead"):
        st.caption(f"Marcado como falso lead — motivo: **{lead.get('falso_lead_motivo') or '—'}**")
        if st.button("↩️ Desmarcar falso lead", use_container_width=True, key=f"unfalso_{lead_id}"):
            with get_conn() as c, c.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET falso_lead=FALSE, falso_lead_motivo=NULL WHERE id=%s",
                    (lead_id,),
                )
            load_lead_full.clear()
            load_leads_full.clear()
            st.success("Desmarcado.")
            st.rerun()
    else:
        st.caption("Marca leads que não servem (concorrente nichado, já tem serviço semelhante). "
                   "A linha dele fica **vermelha** na tabela.")
        fc1, fc2 = st.columns([2, 1])
        with fc1:
            motivo = st.selectbox(
                "Motivo",
                ["É concorrente", "Já possui serviço semelhante", "Fora do perfil", "Outro"],
                key=f"falso_motivo_{lead_id}",
                label_visibility="collapsed",
            )
        with fc2:
            if st.button("🚫 Falso Lead", use_container_width=True, key=f"falso_{lead_id}"):
                with get_conn() as c, c.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET falso_lead=TRUE, falso_lead_motivo=%s WHERE id=%s",
                        (motivo, lead_id),
                    )
                load_lead_full.clear()
                load_leads_full.clear()
                st.success(f"Marcado como falso lead: {motivo}")
                st.rerun()


def render_progress_bar(label: str, pct: float, sub: str = "") -> None:
    """Progress bar custom com animacao bonita."""
    pct_int = int(pct * 100)
    html = f"""
    <div class="custom-progress-label"><span>{label}</span><span>{pct_int}%</span></div>
    <div class="custom-progress-wrapper">
        <div class="custom-progress-bar" style="width: {pct_int}%"></div>
    </div>
    """
    if sub:
        html += f'<div style="color:#999;font-size:12px;margin:4px 4px 0;">{sub}</div>'
    st.markdown(html, unsafe_allow_html=True)


# ============== Data loaders ==============

@st.cache_data(ttl=15, show_spinner=False)
def load_leads_compact() -> pd.DataFrame:
    """Versao reduzida pro live panel — so o que ele mostra."""
    sql = """
        SELECT id, nome, telefone, source, nicho, cidade_tag,
               score_temperatura, first_seen_at
        FROM leads
        ORDER BY id DESC
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=120, show_spinner=False)
def load_leads_full() -> pd.DataFrame:
    """Versao completa pra tabela principal — cache maior pq nao precisa live."""
    sql = """
        SELECT
            l.id, l.source, l.source_url, l.nome, l.telefone, l.email, l.cnpj,
            l.nicho, l.cidade_tag, l.cidade, l.estado,
            l.score_temperatura, l.score_breakdown,
            l.status, l.first_seen_at, l.last_seen_at, l.last_contacted_at,
            l.last_deep_dive_at, l.notes,
            l.falso_lead, l.falso_lead_motivo,
            -- Extrai responsavel principal do quadro societario (BrasilAPI via deep_osint_v2)
            (l.raw_payload #>> '{deep_osint_v2,cnpj_data,socios,0,nome}') AS responsavel,
            COALESCE(s.interest_count, 0) AS interest_count,
            s.interest_categorias
        FROM leads l
        LEFT JOIN (
            SELECT lead_id,
                   COUNT(*) AS interest_count,
                   STRING_AGG(DISTINCT REPLACE(categoria, 'interest_', ''), ', ') AS interest_categorias
            FROM intent_signals
            WHERE categoria LIKE 'interest_%'
            GROUP BY lead_id
        ) s ON s.lead_id = l.id
        ORDER BY l.score_temperatura DESC, l.last_seen_at DESC
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=30, show_spinner=False)
def load_social_profiles() -> pd.DataFrame:
    sql = """
        SELECT lead_id, plataforma, url, fonte, confianca, discovered_at
        FROM social_profiles ORDER BY discovered_at DESC
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=10, show_spinner=False)
def load_runs() -> pd.DataFrame:
    sql = """
        SELECT id, source, started_at, ended_at,
               pages_ok, pages_failed, leads_new, leads_updated, error_summary
        FROM scrape_runs ORDER BY started_at DESC LIMIT 50
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=5, show_spinner=False)
def load_active_runs() -> list[dict]:
    """
    TODOS os runs ATIVOS (sem ended_at). Olha ate 5h atras pra cobrir
    jobs longos (mass enrich + deep osint v2 podem rodar ~3-4h cada).
    Retorna lista — pode haver multiplos rodando em paralelo.
    """
    sql = """
        SELECT id, source, started_at, pages_ok, pages_failed,
               leads_new, leads_updated, metadata
        FROM scrape_runs
        WHERE ended_at IS NULL
          AND started_at > NOW() - INTERVAL '5 hours'
        ORDER BY started_at DESC
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ============== GitHub API ==============

def trigger_workflow(workflow_file: str, inputs: dict | None = None) -> tuple[bool, str]:
    if not GH_TOKEN:
        return False, "GITHUB_TOKEN não configurado nos Secrets do Streamlit"
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"token {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": "main", "inputs": inputs or {}},
            timeout=10,
        )
        if r.status_code == 204:
            return True, "Workflow disparado ✅"
        return False, f"GitHub respondeu {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Erro: {e}"


@st.cache_data(ttl=10, show_spinner=False)
def get_latest_workflow_run(workflow_file: str) -> dict | None:
    if not GH_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_file}/runs?per_page=1"
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if r.status_code == 200:
            runs = r.json().get("workflow_runs", [])
            return runs[0] if runs else None
    except Exception:
        pass
    return None


# ============== LIVE FRAGMENT (atualiza sozinho a cada 5s) ==============

@st.fragment(run_every=10)
def painel_ao_vivo():
    """
    Atualiza sozinho a cada 5s SEM rerodar a pagina inteira.
    Mostra: barra de progresso (se tem scrape ativo), métricas com delta,
    box de recém-chegados com flash amarelo.
    """
    df = load_leads_compact()

    # Barras de progresso pra TODOS os runs ativos (pode haver múltiplos em paralelo)
    active_runs = load_active_runs()
    labels = {
        "gmaps":          ("🗺️", "Google Maps"),
        "workana":        ("🕵️", "Workana"),
        "99freelas":      ("💼", "99Freelas"),
        "enrich_gmaps":   ("📞", "Enriquecendo telefones"),
        "deep_osint":     ("🔍", "Deep OSINT v1"),
        "deep_osint_v2":  ("🕵️‍♀️", "Deep OSINT v2 (8 fontes)"),
    }
    # ETAs em segundos quando não temos total_leads no metadata
    eta_fallback = {
        "gmaps": 2700, "workana": 180, "99freelas": 180,
        "enrich_gmaps": 12000, "deep_osint": 7200, "deep_osint_v2": 12000,
    }

    for active in active_runs:
        elapsed = (datetime.now(timezone.utc) - active["started_at"]).total_seconds()
        emoji, descricao = labels.get(active["source"], ("⏳", active["source"]))
        meta = active.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        # Progresso REAL baseado em pages_ok / total_leads (quando temos)
        total_target = meta.get("total_leads") or meta.get("limit")
        if total_target and active["pages_ok"] > 0:
            progress = min(active["pages_ok"] / int(total_target), 0.99)
            eta_total = int(elapsed / progress) if progress > 0.01 else int(eta_fallback.get(active["source"], 600))
            eta_restante = max(0, eta_total - int(elapsed))
            sub_extra = f" · ETA: {eta_restante//60}min restantes"
        else:
            # Fallback: estimativa de tempo
            eta = eta_fallback.get(active["source"], 600)
            progress = min(elapsed / eta, 0.97)
            sub_extra = ""

        # Sub-texto contextual
        if active["source"] in ("deep_osint", "deep_osint_v2"):
            sub = (
                f"⏱️ {int(elapsed//60)}min · "
                f"🎯 {active['leads_new']} c/ sinais · "
                f"○ {active['leads_updated']} sem sinais · "
                f"📊 {active['pages_ok']}/{total_target or '?'} processados"
                + sub_extra
            )
        elif active["source"] == "enrich_gmaps":
            sub = (
                f"⏱️ {int(elapsed//60)}min · "
                f"📞 {active['leads_new']} com tel · "
                f"🌐 {active['leads_updated']} parciais · "
                f"📊 {active['pages_ok']}/{total_target or '?'} processados"
                + sub_extra
            )
        else:
            sub = (
                f"⏱️ {int(elapsed//60)}min · "
                f"✨ {active['leads_new']} novos · "
                f"🔄 {active['leads_updated']} atualizados · "
                f"📄 {active['pages_ok']} páginas OK"
                + sub_extra
            )

        render_progress_bar(
            label=f"{emoji} {descricao}",
            pct=progress,
            sub=sub,
        )

    # Detecta leads novos
    if df.empty:
        st.info("Nenhum lead ainda. Clica em **🚀 Buscar mais leads** acima.")
        return

    current_max_id = int(df["id"].max())
    prev_max_id = st.session_state.get("prev_max_id")
    if prev_max_id is None:
        st.session_state["prev_max_id"] = current_max_id
        prev_max_id = current_max_id

    novos_ids = set(df[df["id"] > prev_max_id]["id"].tolist()) if current_max_id > prev_max_id else set()

    # Mantém ids "ainda piscando" por 3 segundos
    flash_state = st.session_state.get("flash_ids", {})
    now = time.time()
    for nid in novos_ids:
        flash_state[nid] = now
    flash_state = {i: t for i, t in flash_state.items() if now - t < 3}
    st.session_state["flash_ids"] = flash_state
    st.session_state["prev_max_id"] = current_max_id

    leads_piscando = list(flash_state.keys())

    # Métricas com delta — usa score RAW pra contar quentes (acima de 60 raw = top 25% provavelmente)
    raw_min_m = int(df["score_temperatura"].min()) if len(df) else 0
    raw_max_m = int(df["score_temperatura"].max()) if len(df) else 100
    raw_range_m = max(raw_max_m - raw_min_m, 1)
    # "Quente" = score relativo >= 60% (top 40% do ranking)
    threshold_quente_raw = raw_min_m + (raw_range_m * 50) / 90  # rel 60% ↔ raw (~min + 55% do range)

    col1, col2, col3, col4, col5 = st.columns(5)
    delta_total = len(novos_ids) if novos_ids else None
    col1.metric("Total de leads", len(df), delta=delta_total)
    col2.metric("Score médio", f"{df['score_temperatura'].mean():.0f} pts")
    col3.metric("Top do ranking (≥60% rel)", int((df["score_temperatura"] >= threshold_quente_raw).sum()))
    col4.metric("Com telefone 📱", int(df["telefone"].notna().sum()))
    col5.metric(
        "Novos hoje",
        int((df["first_seen_at"].dt.date == pd.Timestamp.now(tz="UTC").date()).sum()),
    )

    # Box "Recém-chegados" com flash
    if leads_piscando:
        st.markdown("##### 🆕 Recém-chegados")
        df_novos = df[df["id"].isin(leads_piscando)].head(8)
        for _, r in df_novos.iterrows():
            score = int(r["score_temperatura"])
            tel = r["telefone"] or "—"
            cidade = r["cidade_tag"] or "—"
            nicho = r["nicho"] or "—"
            st.markdown(
                f"""<div class="new-lead-card">
                <strong>🔥 {score}%</strong> · <strong>{r['nome'] or 'sem nome'}</strong>
                · <code>{r['source']}</code> · 📍 {cidade} · 🏷️ {nicho} · 📞 {tel}
                </div>""",
                unsafe_allow_html=True,
            )


# ============== UI ==============

st.title("🔥 Prospector Leads")
st.caption("Painel ao vivo (atualiza silenciosamente a cada 5s). Filtros e tabela só atualizam quando você interage.")

aba_leads, aba_dossie, aba_saude = st.tabs(["📋 Leads", "🕵️ Dossiê OSINT", "🩺 Saúde dos scrapers"])

with aba_leads:
    # ====== Botoes de acao ======
    btn1, btn2, _ = st.columns([1, 1, 2])
    with btn1:
        if st.button("🚀 Buscar +100 leads", use_container_width=True, type="primary"):
            ok, msg = trigger_workflow("scrape-gmaps.yml", {"limit": "100"})
            st.session_state["last_trigger_msg"] = (ok, msg, "scrape-gmaps.yml", time.time())

    with btn2:
        if st.button("🔍 Aprofundar OSINT", use_container_width=True, help="Aprofunda todos os leads não verificados nos últimos 7 dias, buscando sinais de interesse em IA/automação/dev."):
            ok, msg = trigger_workflow("deep-osint.yml", {"limit": "1000"})
            st.session_state["last_trigger_msg"] = (ok, msg, "deep-osint.yml", time.time())

    if "last_trigger_msg" in st.session_state:
        ok, msg, wf, when = st.session_state["last_trigger_msg"]
        if time.time() - when < 30:
            if ok:
                st.success(f"{msg} ({wf})")
            else:
                st.error(msg)

    st.divider()

    # ====== PAINEL AO VIVO (atualiza sozinho via fragment) ======
    painel_ao_vivo()

    st.divider()

    # ====== Tabela principal (FORA do fragment — só atualiza quando filtros mudam) ======
    df = load_leads_full()
    if df.empty:
        st.stop()

    fc1, fc2, fc3, fc4, fc5, fc6 = st.columns([2, 2, 2, 1, 1, 1])
    score_min_rel = fc1.slider("Score mínimo (relativo %)", 0, 100, 50, step=5, help="100% = top do ranking, 10% = última posição")
    cidades_disp = ["(todas)"] + sorted([c for c in df["cidade_tag"].dropna().unique()])
    cidade_sel = fc2.multiselect("Cidade", cidades_disp, default=["(todas)"])
    nichos_disp = ["(todos)"] + sorted([n for n in df["nicho"].dropna().unique()])
    nicho_sel = fc3.multiselect("Nicho", nichos_disp, default=["(todos)"])
    status_sel = fc4.selectbox("Status", ["todos", "novo", "contatado", "respondeu", "fechou", "descartado"])
    so_com_tel = fc5.checkbox("📱 Só com telefone", value=False)
    so_com_sinal = fc6.checkbox("🎯 Apenas com alto interesse declarado", value=False, help="Só leads que têm ao menos 1 sinal de interesse detectado via Deep OSINT")

    # Converte score_min_rel pra raw threshold pra filtrar
    raw_min_filt = int(df["score_temperatura"].min()) if len(df) else 0
    raw_max_filt = int(df["score_temperatura"].max()) if len(df) else 100
    raw_range_filt = max(raw_max_filt - raw_min_filt, 1)
    # rel = 10 + 90*(raw-min)/range  ⇒  raw_threshold = min + range*(rel-10)/90
    raw_threshold = raw_min_filt + raw_range_filt * max(0, score_min_rel - 10) / 90

    flt = df["score_temperatura"] >= raw_threshold
    if "(todas)" not in cidade_sel and cidade_sel:
        flt &= df["cidade_tag"].isin(cidade_sel)
    if "(todos)" not in nicho_sel and nicho_sel:
        flt &= df["nicho"].isin(nicho_sel)
    if status_sel != "todos":
        flt &= df["status"] == status_sel
    if so_com_tel:
        flt &= df["telefone"].notna()
    if so_com_sinal:
        flt &= df["interest_count"] > 0

    df_f = df[flt].copy()

    def _wa_link(tel):
        if pd.isna(tel) or not tel:
            return None
        clean = "".join(c for c in str(tel) if c.isdigit() or c == "+")
        return f"https://wa.me/{clean.lstrip('+')}"

    df_f["whatsapp_link"] = df_f["telefone"].apply(_wa_link)

    col_count, col_refresh = st.columns([3, 1])
    col_count.write(f"**{len(df_f)}** leads no filtro.")
    if col_refresh.button("🔄 Atualizar tabela", use_container_width=True):
        load_leads_full.clear()
        st.rerun()

    # Score relativo: 100% = top 1 raw, 10% = ultimo. Linear baseado em min/max RAW.
    raw_min = int(df["score_temperatura"].min()) if len(df) else 0
    raw_max = int(df["score_temperatura"].max()) if len(df) else 100
    raw_range = max(raw_max - raw_min, 1)

    def _score_relativo(raw_int: int) -> int:
        return int(round(10 + 90 * (raw_int - raw_min) / raw_range))

    st.caption(
        f"📊 **{len(df_f)} leads** no filtro · "
        f"Score relativo (100% = top {raw_max} pts, 10% = base {raw_min} pts) · "
        f"💡 Clica em **📋** em qualquer linha da tabela pra abrir a ficha completa."
    )

    # Verifica query param "lead_id" (backup pra clicks de fora)
    qp_lead_id = st.query_params.get("lead_id")
    if qp_lead_id:
        try:
            lead_id_to_open = int(qp_lead_id)
            del st.query_params["lead_id"]
            show_lead_dialog(lead_id_to_open)
        except (ValueError, KeyError):
            pass

    # ====== TABELA AGGRID — scroll virtual + botao por linha ======

    # Prepara DataFrame pra exibicao no AgGrid
    df_grid = df_f[[
        "id", "score_temperatura", "nome", "telefone", "cidade_tag", "nicho",
        "interest_count", "interest_categorias", "responsavel",
        "falso_lead", "falso_lead_motivo"
    ]].copy()
    df_grid["score_rel"] = df_grid["score_temperatura"].apply(_score_relativo)
    df_grid["telefone"] = df_grid["telefone"].fillna("—")
    df_grid["responsavel"] = df_grid["responsavel"].fillna("—")
    df_grid["interest_categorias"] = df_grid["interest_categorias"].fillna("")
    df_grid["nicho"] = df_grid["nicho"].fillna("—")
    df_grid["cidade_tag"] = df_grid["cidade_tag"].fillna("—")
    df_grid["falso_lead"] = df_grid["falso_lead"].fillna(False).astype(bool)
    df_grid["falso_lead_motivo"] = df_grid["falso_lead_motivo"].fillna("")

    # Cell renderer pro botao 📋 — click mostra spinner IMEDIATO + dispara selection
    btn_renderer = JsCode("""
    class BtnRenderer {
        init(params) {
            this.params = params;
            this.eGui = document.createElement('button');
            this.eGui.innerHTML = '📋';
            this.eGui.style.cssText = `
                background: #ff4b4b; color: white; border: none;
                padding: 4px 10px; border-radius: 4px;
                font-size: 14px; cursor: pointer; line-height: 1;
                transition: background 0.15s, transform 0.1s;
            `;
            this.eGui.onmouseenter = () => {
                if (!this.eGui.disabled) {
                    this.eGui.style.background = '#ff8c42';
                    this.eGui.style.transform = 'scale(1.1)';
                }
            };
            this.eGui.onmouseleave = () => {
                if (!this.eGui.disabled) {
                    this.eGui.style.background = '#ff4b4b';
                    this.eGui.style.transform = 'scale(1)';
                }
            };
            this.eGui.addEventListener('click', () => {
                // Feedback visual INSTANTANEO no proprio botao
                this.eGui.innerHTML = '⏳';
                this.eGui.style.background = '#666';
                this.eGui.style.cursor = 'wait';
                this.eGui.disabled = true;
                // Tenta mostrar overlay full-page (se sandbox permitir)
                try {
                    const tw = window.top || window.parent;
                    const doc = tw.document;
                    if (!doc.getElementById('loading-ficha-overlay')) {
                        const ov = doc.createElement('div');
                        ov.id = 'loading-ficha-overlay';
                        ov.style.cssText = `
                            position: fixed; inset: 0;
                            background: rgba(0,0,0,0.55);
                            z-index: 999999;
                            display: flex; align-items: center; justify-content: center;
                            color: white; font-size: 28px;
                            backdrop-filter: blur(2px);
                        `;
                        ov.innerHTML = `
                            <div style="display:flex;flex-direction:column;align-items:center;gap:14px;">
                              <div style="width:48px;height:48px;border:4px solid #ff4b4b;border-top-color:transparent;border-radius:50%;animation:spinner 0.8s linear infinite;"></div>
                              <div style="font-size:16px;">Abrindo ficha do lead...</div>
                            </div>
                            <style>@keyframes spinner { to { transform: rotate(360deg); } }</style>
                        `;
                        doc.body.appendChild(ov);
                        // Auto-remove apos 5s caso algo trave
                        setTimeout(() => {
                            const o = doc.getElementById('loading-ficha-overlay');
                            if (o) o.remove();
                        }, 5000);
                    }
                } catch (e) { /* sandbox bloqueou, sem problema */ }
                params.node.setSelected(true, true);
            });
        }
        getGui() { return this.eGui; }
        refresh() { return false; }
    }
    """)

    # Score progress bar — CLASS renderer (constroi DOM, nao retorna string)
    score_renderer = JsCode("""
    class ScoreRenderer {
        init(params) {
            const score = params.value || 0;
            let grad, prefix;
            if (score >= 90) { prefix = '🔥 '; grad = 'linear-gradient(90deg,#ff4b4b,#ff8c42)'; }
            else if (score >= 60) { prefix = ''; grad = 'linear-gradient(90deg,#ff8c42,#ffa726)'; }
            else if (score >= 30) { prefix = ''; grad = 'linear-gradient(90deg,#fbc02d,#fdd835)'; }
            else { prefix = ''; grad = 'linear-gradient(90deg,#546e7a,#78909c)'; }
            this.eGui = document.createElement('div');
            this.eGui.style.cssText = 'background:#1a1a1a;border-radius:4px;height:18px;position:relative;overflow:hidden;margin-top:3px;min-width:80px;';
            const fill = document.createElement('div');
            fill.style.cssText = `background:${grad};height:100%;width:${score}%;`;
            const label = document.createElement('div');
            label.style.cssText = 'position:absolute;top:0;left:0;right:0;text-align:center;line-height:18px;font-size:11px;font-weight:600;color:#fff;text-shadow:0 0 3px rgba(0,0,0,0.7);';
            label.textContent = `${prefix}${score}%`;
            this.eGui.appendChild(fill);
            this.eGui.appendChild(label);
        }
        getGui() { return this.eGui; }
        refresh() { return false; }
    }
    """)

    # Sinais — CLASS renderer
    sinais_renderer = JsCode("""
    class SinaisRenderer {
        init(params) {
            const count = params.data.interest_count || 0;
            const cats = params.data.interest_categorias || '';
            this.eGui = document.createElement('div');
            if (count > 0) {
                const numSpan = document.createElement('span');
                numSpan.style.cssText = 'color:#4caf50;font-weight:600;';
                numSpan.textContent = `🎯 ${count}`;
                this.eGui.appendChild(numSpan);
                if (cats) {
                    const catsSpan = document.createElement('span');
                    catsSpan.style.cssText = 'color:#888;font-size:10px;display:block;line-height:1.2;margin-top:2px;';
                    catsSpan.textContent = `(${cats})`;
                    this.eGui.appendChild(catsSpan);
                }
            } else {
                this.eGui.innerHTML = '';
                this.eGui.style.color = '#555';
                this.eGui.textContent = '○ 0';
            }
        }
        getGui() { return this.eGui; }
        refresh() { return false; }
    }
    """)

    # Telefone — CLASS renderer (link clicavel pro wa.me)
    tel_renderer = JsCode("""
    class TelRenderer {
        init(params) {
            const tel = params.value;
            if (!tel || tel === '—' || tel === '') {
                this.eGui = document.createElement('span');
                this.eGui.style.color = '#555';
                this.eGui.textContent = '—';
            } else {
                this.eGui = document.createElement('a');
                const clean = String(tel).replace(/[^\\d+]/g, '').replace(/^\\+/, '');
                this.eGui.href = 'https://wa.me/' + clean;
                this.eGui.target = '_blank';
                this.eGui.style.cssText = 'color:#4caf50;text-decoration:none;';
                this.eGui.textContent = tel;
            }
        }
        getGui() { return this.eGui; }
        refresh() { return false; }
    }
    """)

    gb = GridOptionsBuilder.from_dataframe(df_grid)
    gb.configure_default_column(resizable=True, sortable=True, filter=True, suppressHeaderMenuButton=False)
    gb.configure_column("id", header_name="ID", width=70, hide=True)
    gb.configure_column("_btn", header_name="📋", width=60, cellRenderer=btn_renderer, suppressMenu=True, sortable=False, filter=False, pinned="left")
    gb.configure_column("score_rel", header_name="Score 🔥", width=120, cellRenderer=score_renderer, type=["numericColumn"])
    gb.configure_column("interest_count", header_name="Sinais 🎯", width=150, cellRenderer=sinais_renderer)
    gb.configure_column("interest_categorias", hide=True)
    gb.configure_column("score_temperatura", hide=True)
    gb.configure_column("nome", header_name="Nome", flex=2)
    gb.configure_column("telefone", header_name="Telefone", width=160, cellRenderer=tel_renderer)
    gb.configure_column("cidade_tag", header_name="Cidade", width=110)
    gb.configure_column("nicho", header_name="Nicho", width=140)
    gb.configure_column("responsavel", header_name="Responsável", width=160)
    gb.configure_column("falso_lead", hide=True)
    gb.configure_column("falso_lead_motivo", hide=True)
    gb.configure_selection(selection_mode="single", use_checkbox=False)

    # Adiciona coluna virtual _btn (vazia, so pro renderer)
    df_grid.insert(0, "_btn", "")

    grid_options = gb.build()
    # Linha VERMELHA pros falsos leads (concorrente / ja tem servico semelhante)
    grid_options["getRowStyle"] = JsCode("""
        function(params) {
            if (params.data && params.data.falso_lead === true) {
                return { background: 'rgba(198,40,40,0.28)', color: '#ff8a80' };
            }
            return null;
        }
    """)

    response = AgGrid(
        df_grid,
        gridOptions=grid_options,
        update_mode=GridUpdateMode.SELECTION_CHANGED,
        fit_columns_on_grid_load=False,
        allow_unsafe_jscode=True,
        theme="streamlit",
        height=650,
        key="leads_aggrid",
    )

    # Click no botao 📋 → setSelectedRows → response['selected_rows'] populado → abre dialog
    sel = response.get("selected_rows")
    if sel is not None and len(sel) > 0:
        # selected_rows pode ser DataFrame ou list de dicts dependendo da versao
        if isinstance(sel, pd.DataFrame):
            lead_id_clicked = int(sel.iloc[0]["id"])
        else:
            lead_id_clicked = int(sel[0]["id"])
        # Evita reabrir se mesmo lead já foi mostrado
        if st.session_state.get("aggrid_opened_lead") != lead_id_clicked:
            st.session_state["aggrid_opened_lead"] = lead_id_clicked
            show_lead_dialog(lead_id_clicked)


    csv = df_f.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Baixar CSV filtrado", csv, "leads.csv", "text/csv")


with aba_dossie:
    st.subheader("🕵️ Dossiê OSINT — perfis sociais cruzados")
    st.caption("Perfis encontrados via username pivoting + DuckDuckGo dorks + GMaps deep.")

    socials = load_social_profiles()
    if socials.empty:
        st.info("Nenhum perfil OSINT capturado ainda.")
    else:
        df_leads = load_leads_full()[["id", "nome", "telefone", "cidade_tag", "score_temperatura"]]
        df_leads.columns = ["lead_id", "nome", "telefone", "cidade_tag", "score"]
        merged = socials.merge(df_leads, on="lead_id", how="left")

        col1, col2 = st.columns(2)
        col1.metric("Total de perfis", len(socials))
        col2.metric("Leads com perfil OSINT", merged["lead_id"].nunique())

        plats_disp = ["(todas)"] + sorted(socials["plataforma"].unique())
        plat_sel = st.multiselect("Plataforma", plats_disp, default=["(todas)"])
        if "(todas)" not in plat_sel and plat_sel:
            merged = merged[merged["plataforma"].isin(plat_sel)]

        st.dataframe(
            merged[["lead_id", "nome", "plataforma", "url", "score", "cidade_tag", "fonte", "confianca"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "url": st.column_config.LinkColumn("Perfil", display_text="Abrir"),
                "confianca": st.column_config.ProgressColumn("Confiança", min_value=0, max_value=100, format="%d%%"),
            },
        )

with aba_saude:
    runs = load_runs()
    if runs.empty:
        st.info("Nenhuma execução registrada ainda.")
    else:
        st.subheader("Últimas 50 execuções de scrapers")
        st.dataframe(runs, use_container_width=True, hide_index=True)

    if GH_TOKEN:
        st.subheader("Últimos runs no GitHub Actions")
        for wf in ["scrape-gmaps.yml", "scrape-workana.yml", "enrich-gmaps.yml"]:
            run = get_latest_workflow_run(wf)
            if run:
                emoji = "✅" if run.get("conclusion") == "success" else ("⏳" if run["status"] == "in_progress" else "❌")
                st.write(
                    f"{emoji} `{wf}` — status: **{run['status']}** "
                    f"({run.get('conclusion') or '—'}) — "
                    f"[ver no GitHub]({run['html_url']})"
                )
    else:
        st.warning(
            "⚠️ `GITHUB_TOKEN` não configurado nos Secrets do Streamlit. "
            "Sem ele, os botões de disparo não funcionam."
        )
