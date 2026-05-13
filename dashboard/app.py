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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import streamlit as st

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
                   status, raw_payload, notes,
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
            SELECT categoria, palavra_chave, trecho_texto, source_url, boost, captured_at
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


@st.dialog("📋 Ficha completa do lead", width="large")
def show_lead_dialog(lead_id: int) -> None:
    lead = load_lead_full(lead_id)
    if not lead:
        st.error("Lead não encontrado.")
        return

    # Cabecalho
    score = int(lead["score_temperatura"])
    score_emoji = "🔥" if score >= 75 else "🟡" if score >= 50 else "🧊"
    st.markdown(f"### {score_emoji} {lead['nome'] or '(sem nome)'} — Score **{score}%**")

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
        st.caption("Cada sinal tem link 🔗 pra você verificar a fonte e confirmar se é real.")
        for s in interest:
            cat = s["categoria"].replace("interest_", "")
            url_link = ""
            if s.get("source_url"):
                url_link = f' · <a href="{s["source_url"]}" target="_blank" style="color:#81c784;">🔗 ver fonte</a>'
            st.markdown(
                f"""<div style="background:#1a3a1a;border-left:3px solid #4caf50;padding:8px 12px;margin:4px 0;border-radius:4px;">
                <strong>🎯 {cat}</strong> · keyword: <code>{s['palavra_chave']}</code> · <code>+{s['boost']}</code>{url_link}<br>
                <span style="color:#999;font-size:13px;">"<i>{(s['trecho_texto'] or '')[:200]}</i>"</span>
                </div>""",
                unsafe_allow_html=True,
            )

    if intent_actual:
        st.markdown("#### 🔥 Intent declarado (post pedindo serviço)")
        for s in intent_actual:
            url_link = ""
            if s.get("source_url"):
                url_link = f' · <a href="{s["source_url"]}" target="_blank" style="color:#ef9a9a;">🔗 ver post</a>'
            st.markdown(
                f"""<div style="background:#3a1a1a;border-left:3px solid #f44336;padding:8px 12px;margin:4px 0;border-radius:4px;">
                <strong>🔥 {s['categoria']}</strong> · keyword: <code>{s['palavra_chave']}</code> · <code>+{s['boost']}</code>{url_link}<br>
                <span style="color:#999;font-size:13px;">"<i>{(s['trecho_texto'] or '')[:200]}</i>"</span>
                </div>""",
                unsafe_allow_html=True,
            )

    if not sinais:
        st.caption("⚠️ Nenhum sinal de intent/interesse detectado ainda. Use 'Aprofundar OSINT' pra investigar.")

    st.divider()

    # Perfis sociais
    socials = lead.get("social_profiles") or []
    if socials:
        st.markdown("#### 🌐 Perfis sociais cruzados")
        for sp in socials:
            st.markdown(f"- **{sp['plataforma']}** · [abrir]({sp['url']}) · confiança {sp['confianca']}% · via `{sp['fonte']}`")

    st.divider()

    # Quadro societario (do CNPJ via BrasilAPI)
    rp_for_socios = lead.get("raw_payload") or {}
    if isinstance(rp_for_socios, str):
        try:
            rp_for_socios = json.loads(rp_for_socios)
        except Exception:
            rp_for_socios = {}
    cnpj_data = (rp_for_socios.get("deep_osint_v2") or {}).get("cnpj_data") or {}
    if cnpj_data and cnpj_data.get("socios"):
        st.markdown("#### 👥 Quadro societário (Receita Federal via BrasilAPI)")
        st.caption(
            f"**{cnpj_data.get('razao_social') or '—'}** ({cnpj_data.get('nome_fantasia') or 'sem fantasia'}) · "
            f"CNAE: {cnpj_data.get('cnae_principal') or '—'} · "
            f"Porte: {cnpj_data.get('porte') or '—'}"
        )
        for s in cnpj_data["socios"]:
            st.markdown(
                f"- **{s['nome']}** — {s['qualificacao']} (desde {s['data_entrada'] or '—'})"
            )

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

@st.cache_data(ttl=5, show_spinner=False)
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


@st.cache_data(ttl=30, show_spinner=False)
def load_leads_full() -> pd.DataFrame:
    """Versao completa pra tabela principal — cache maior pq nao precisa live."""
    sql = """
        SELECT
            l.id, l.source, l.source_url, l.nome, l.telefone, l.email, l.cnpj,
            l.nicho, l.cidade_tag, l.cidade, l.estado,
            l.score_temperatura, l.score_breakdown,
            l.status, l.first_seen_at, l.last_seen_at, l.last_contacted_at,
            l.last_deep_dive_at, l.notes,
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
def load_active_run() -> dict | None:
    """
    Run ATIVO de scraper. Filtra rows antigas (mais de 1h sem updates)
    pra nao mostrar barra fantasma de runs cancelados/orfaos.
    """
    sql = """
        SELECT id, source, started_at, pages_ok, pages_failed, leads_new, leads_updated
        FROM scrape_runs
        WHERE ended_at IS NULL
          AND started_at > NOW() - INTERVAL '1 hour'
        ORDER BY started_at DESC LIMIT 1
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))


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

    # Barra de progresso APENAS se tem scrape/enrich/osint ativo (filtra stale)
    active = load_active_run()
    if active:
        elapsed = (datetime.now(timezone.utc) - active["started_at"]).total_seconds()
        # ETA em segundos por source
        eta_map = {
            "gmaps":          2700,   # ~45min
            "workana":         180,
            "99freelas":       180,
            "enrich_gmaps":    600,   # 100 leads × 6s
            "deep_osint":     1200,   # 100 leads × 12s
            "deep_osint_v2":  1800,   # 100 leads × 18s (mais fontes)
        }
        labels = {
            "gmaps":          ("🗺️", "Buscando empresas no Google Maps"),
            "workana":        ("🕵️", "Caçando intent quente no Workana"),
            "99freelas":      ("💼", "Caçando intent no 99Freelas"),
            "enrich_gmaps":   ("📞", "Enriquecendo leads com cascata de técnicas"),
            "deep_osint":     ("🔍", "Aprofundando OSINT (sinais de interesse)"),
            "deep_osint_v2":  ("🕵️‍♀️", "Aprofundando OSINT v2 (8 fontes + perfis sociais + CNPJ)"),
        }
        eta = eta_map.get(active["source"], 600)
        emoji, descricao = labels.get(active["source"], ("⏳", active["source"]))
        progress = min(elapsed / eta, 0.97)

        # Sub-texto adaptado pra cada tipo
        if active["source"] in ("deep_osint", "deep_osint_v2"):
            sub = (
                f"⏱️ {int(elapsed)}s · "
                f"🎯 {active['leads_new']} c/ sinais · "
                f"○ {active['leads_updated']} sem sinais · "
                f"📊 {active['pages_ok']} leads processados"
            )
        elif active["source"] == "enrich_gmaps":
            sub = (
                f"⏱️ {int(elapsed)}s · "
                f"📞 {active['leads_new']} com tel · "
                f"🌐 {active['leads_updated']} parciais · "
                f"📊 {active['pages_ok']} processados"
            )
        else:
            sub = (
                f"⏱️ {int(elapsed)}s · "
                f"✨ {active['leads_new']} novos · "
                f"🔄 {active['leads_updated']} atualizados · "
                f"📄 {active['pages_ok']} OK"
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

    # Métricas com delta
    col1, col2, col3, col4, col5 = st.columns(5)
    delta_total = len(novos_ids) if novos_ids else None
    col1.metric("Total de leads", len(df), delta=delta_total)
    col2.metric("Score médio", f"{df['score_temperatura'].mean():.0f}")
    col3.metric("Quentes (≥60%)", int((df["score_temperatura"] >= 60).sum()))
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
    score_min = fc1.slider("Score mínimo (%)", 0, 100, 50, step=5)
    cidades_disp = ["(todas)"] + sorted([c for c in df["cidade_tag"].dropna().unique()])
    cidade_sel = fc2.multiselect("Cidade", cidades_disp, default=["(todas)"])
    nichos_disp = ["(todos)"] + sorted([n for n in df["nicho"].dropna().unique()])
    nicho_sel = fc3.multiselect("Nicho", nichos_disp, default=["(todos)"])
    status_sel = fc4.selectbox("Status", ["todos", "novo", "contatado", "respondeu", "fechou", "descartado"])
    so_com_tel = fc5.checkbox("📱 Só com telefone", value=False)
    so_com_sinal = fc6.checkbox("🎯 Apenas com alto interesse declarado", value=False, help="Só leads que têm ao menos 1 sinal de interesse detectado via Deep OSINT")

    flt = df["score_temperatura"] >= score_min
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

    st.caption(f"📊 **{len(df_f)} leads** no filtro · Tabela com scroll fluido (HTML nativo).")

    # Verifica query param "lead_id" — abre ficha se setado (click no botao 📋)
    qp_lead_id = st.query_params.get("lead_id")
    if qp_lead_id:
        try:
            lead_id_to_open = int(qp_lead_id)
            # Limpa o param pra nao reabrir em reload futuro
            del st.query_params["lead_id"]
            show_lead_dialog(lead_id_to_open)
        except (ValueError, KeyError):
            pass

    # Gera HTML da tabela em UMA so string (rapido — 1 markdown render)
    def _esc(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        s = str(v)
        return s.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    rows_html = []
    for _, row in df_f.iterrows():
        lead_id_row = int(row["id"])
        score = int(row["score_temperatura"])

        if score >= 90:
            prefix, grad = "🔥 ", "linear-gradient(90deg,#ff4b4b,#ff8c42)"
        elif score >= 60:
            prefix, grad = "", "linear-gradient(90deg,#ff8c42,#ffa726)"
        elif score >= 30:
            prefix, grad = "", "linear-gradient(90deg,#fbc02d,#fdd835)"
        else:
            prefix, grad = "", "linear-gradient(90deg,#546e7a,#78909c)"

        score_html = (
            f'<div class="score-bar-outer">'
            f'<div class="score-bar-fill" style="background:{grad};width:{score}%;"></div>'
            f'<div class="score-bar-label">{prefix}{score}%</div>'
            f'</div>'
        )

        sinais = int(row.get("interest_count") or 0)
        cats = row.get("interest_categorias")
        if sinais > 0:
            cats_part = f'<span class="sinal-cats">({_esc(cats)})</span>' if cats and not pd.isna(cats) else ""
            sinais_html = f'<span class="sinal-count">🎯 {sinais}</span>{cats_part}'
        else:
            sinais_html = '<span class="empty-cell">○ 0</span>'

        tel = row["telefone"]
        if tel and not pd.isna(tel):
            wa = _wa_url(tel)
            tel_html = f'<a href="{wa}" target="_blank" class="tel-link">{_esc(tel)}</a>'
        else:
            tel_html = '<span class="empty-cell">—</span>'

        resp = row.get("responsavel")
        resp_html = _esc(resp) if resp and not pd.isna(resp) else '<span class="empty-cell">—</span>'

        rows_html.append(
            f'<tr>'
            f'<td><a href="?lead_id={lead_id_row}" class="ficha-btn">📋</a></td>'
            f'<td>{score_html}</td>'
            f'<td>{sinais_html}</td>'
            f'<td>{_esc(row["cidade_tag"]) or "—"}</td>'
            f'<td style="font-size:11px;">{_esc(row["nicho"]) or "—"}</td>'
            f'<td><strong>{_esc(row["nome"]) or "(sem nome)"}</strong></td>'
            f'<td>{tel_html}</td>'
            f'<td style="font-size:11px;">{resp_html}</td>'
            f'</tr>'
        )

    table_html = (
        '<div class="leads-table-container">'
        '<table class="leads-html-table">'
        '<thead><tr>'
        '<th style="width:55px;">📋</th>'
        '<th style="width:110px;">Score 🔥</th>'
        '<th style="width:140px;">Sinais 🎯</th>'
        '<th style="width:90px;">Cidade</th>'
        '<th style="width:120px;">Nicho</th>'
        '<th>Nome</th>'
        '<th style="width:140px;">Telefone</th>'
        '<th style="width:130px;">Responsável</th>'
        '</tr></thead>'
        '<tbody>' + "".join(rows_html) + '</tbody>'
        '</table>'
        '</div>'
    )
    st.markdown(table_html, unsafe_allow_html=True)

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
