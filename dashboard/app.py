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
    </style>
    """,
    unsafe_allow_html=True,
)


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
            "gmaps":        2700,   # ~45min
            "workana":       180,
            "99freelas":     180,
            "enrich_gmaps":  600,   # 100 leads × 6s
            "deep_osint":   1200,   # 100 leads × 12s (5 fontes por lead)
        }
        labels = {
            "gmaps":        ("🗺️", "Buscando empresas no Google Maps"),
            "workana":      ("🕵️", "Caçando intent quente no Workana"),
            "99freelas":    ("💼", "Caçando intent no 99Freelas"),
            "enrich_gmaps": ("📞", "Enriquecendo leads com cascata de técnicas"),
            "deep_osint":   ("🔍", "Aprofundando OSINT (sinais de interesse)"),
        }
        eta = eta_map.get(active["source"], 600)
        emoji, descricao = labels.get(active["source"], ("⏳", active["source"]))
        progress = min(elapsed / eta, 0.97)

        # Sub-texto adaptado pra cada tipo
        if active["source"] == "deep_osint":
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
    so_com_sinal = fc6.checkbox("🎯 Só com sinal", value=False, help="Só leads que têm ao menos 1 sinal de interesse detectado")

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

    show_cols = [
        "score_temperatura", "interest_count", "interest_categorias",
        "cidade_tag", "nicho", "source",
        "nome", "telefone", "whatsapp_link", "email", "status",
        "source_url", "first_seen_at", "last_deep_dive_at",
    ]
    st.dataframe(
        df_f[show_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "score_temperatura": st.column_config.ProgressColumn(
                "Score 🔥", min_value=0, max_value=100, format="%d%%"
            ),
            "interest_count": st.column_config.NumberColumn("Sinais 🎯", help="Quantos sinais de interesse detectados via deep OSINT"),
            "interest_categorias": st.column_config.TextColumn("Tipos de sinal", help="Categorias: ia_mencao, automacao, whatsapp_automation, vaga_tech, etc"),
            "source_url": st.column_config.LinkColumn("Origem"),
            "whatsapp_link": st.column_config.LinkColumn("WhatsApp 📱", display_text="Abrir"),
            "first_seen_at": st.column_config.DatetimeColumn("Visto 1a vez"),
            "last_deep_dive_at": st.column_config.DatetimeColumn("OSINT em"),
        },
    )

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
