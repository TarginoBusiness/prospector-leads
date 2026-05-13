# Prospector Leads

Webscraping continuo pra prospeccao ativa de clientes. Coleta nomes, telefones, WhatsApp e e-mails de empresas/pessoas com perfil de comprar:
- Automacao de WhatsApp
- Aplicativos personalizados
- Software sob medida
- Solucoes com IA

## Arquitetura

```
GitHub Actions (cron, Python) ── grava ──► Neon Postgres ◄── le ── Streamlit Cloud (dashboard)
```

Custo: R$ 0,00/mes (tudo no free tier).

## Stack

- **Scrapers:** Python, Playwright + Patchright (stealth), httpx + selectolax, Crawlee
- **Banco:** Neon (Postgres serverless free tier)
- **Dashboard:** Streamlit + Streamlit Community Cloud
- **Orquestracao:** GitHub Actions cron

## Estrutura

```
prospector-leads/
├── scrapers/         # 1 modulo por fonte (workana, 99freelas, gmaps, instagram, ...)
├── scoring/          # engine de score de temperatura + intent detector
├── db/               # schema SQL + utilitarios de conexao
├── dashboard/        # Streamlit app
├── config/           # YAMLs editaveis (keywords, niches, cities)
├── .github/workflows/# cron jobs do GH Actions
└── tests/
```

## Cidades-alvo

1. **Sao Luis - MA** (prioridade #1, boost +15% no score)
2. Sao Paulo - SP (+10%)
3. Curitiba - PR (+10%)
4. Rio de Janeiro - RJ (+10%)

## Setup local

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium firefox
cp .env.example .env             # preencha DATABASE_URL do Neon
python -m db.migrate             # cria schema
python -m scrapers.workana       # roda scraper de teste
streamlit run dashboard/app.py   # abre dashboard local
```

## Documentacao das decisoes

Cofre Obsidian: `Projeto-Webscraping de Clientes` (memorias do projeto).
