"""
Dashboard de leads — Streamlit.

Roda local com:  streamlit run dashboard/app.py
Roda em prod:    Streamlit Community Cloud (conecta no repo, deploy auto).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Permite importar `db.*` quando rodando via streamlit
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from db.connection import get_conn

st.set_page_config(
    page_title="Prospector Leads",
    page_icon="🔥",
    layout="wide",
)


# Streamlit Cloud passa secrets via st.secrets; jogamos pro env pra db/connection ler
if "DATABASE_URL" in st.secrets:
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]


@st.cache_data(ttl=60)
def load_leads() -> pd.DataFrame:
    sql = """
        SELECT id, source, source_url, nome, telefone, email, cnpj,
               nicho, cidade_tag, cidade, estado,
               score_temperatura, score_breakdown,
               status, first_seen_at, last_seen_at, last_contacted_at, notes
        FROM leads
        ORDER BY score_temperatura DESC, last_seen_at DESC
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=60)
def load_runs() -> pd.DataFrame:
    sql = """
        SELECT id, source, started_at, ended_at,
               pages_ok, pages_failed, leads_new, leads_updated, error_summary
        FROM scrape_runs
        ORDER BY started_at DESC
        LIMIT 50
    """
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


# ------- UI ---------

st.title("🔥 Prospector Leads")
st.caption("Webscraping continuo de clientes potenciais. Quanto mais quente o lead (score %), mais urgente o contato.")

aba_leads, aba_saude = st.tabs(["📋 Leads", "🩺 Saude dos scrapers"])

with aba_leads:
    df = load_leads()

    if df.empty:
        st.info("Nenhum lead ainda. Rode um scraper: `python -m scrapers.workana`")
        st.stop()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total de leads", len(df))
    col2.metric("Score medio", f"{df['score_temperatura'].mean():.0f}")
    col3.metric("Leads quentes (>=60%)", int((df["score_temperatura"] >= 60).sum()))
    col4.metric("Leads novos hoje", int((df["first_seen_at"].dt.date == pd.Timestamp.now(tz="UTC").date()).sum()))

    st.divider()

    # Filtros
    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 1])
    score_min = fc1.slider("Score mínimo (%)", 0, 100, 50, step=5)
    cidades_disp = ["(todas)"] + sorted([c for c in df["cidade_tag"].dropna().unique()])
    cidade_sel = fc2.multiselect("Cidade", cidades_disp, default=["(todas)"])
    nichos_disp = ["(todos)"] + sorted([n for n in df["nicho"].dropna().unique()])
    nicho_sel = fc3.multiselect("Nicho", nichos_disp, default=["(todos)"])
    status_sel = fc4.selectbox("Status", ["todos", "novo", "contatado", "respondeu", "fechou", "descartado"])

    flt = df["score_temperatura"] >= score_min
    if "(todas)" not in cidade_sel and cidade_sel:
        flt &= df["cidade_tag"].isin(cidade_sel)
    if "(todos)" not in nicho_sel and nicho_sel:
        flt &= df["nicho"].isin(nicho_sel)
    if status_sel != "todos":
        flt &= df["status"] == status_sel

    df_f = df[flt].copy()
    st.write(f"**{len(df_f)}** leads no filtro.")

    # Tabela
    show_cols = [
        "score_temperatura", "cidade_tag", "nicho", "source",
        "nome", "telefone", "email", "status",
        "source_url", "first_seen_at", "last_seen_at",
    ]
    st.dataframe(
        df_f[show_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "score_temperatura": st.column_config.ProgressColumn(
                "Score 🔥", min_value=0, max_value=100, format="%d%%"
            ),
            "source_url": st.column_config.LinkColumn("Origem"),
            "first_seen_at": st.column_config.DatetimeColumn("Visto 1a vez"),
            "last_seen_at": st.column_config.DatetimeColumn("Atualizado"),
        },
    )

    # Export
    csv = df_f.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Baixar CSV filtrado", csv, "leads.csv", "text/csv")

with aba_saude:
    runs = load_runs()
    if runs.empty:
        st.info("Nenhuma execucao registrada ainda.")
    else:
        st.subheader("Ultimas 50 execucoes de scrapers")
        st.dataframe(runs, use_container_width=True, hide_index=True)
