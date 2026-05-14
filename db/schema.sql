-- Schema do banco (Neon Postgres)
-- Rodar uma vez via db/migrate.py ou copiar/colar no SQL editor do Neon.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- HTML cru de toda pagina raspada (camada de seguranca contra extracao errada)
CREATE TABLE IF NOT EXISTS raw_pages (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    url          TEXT NOT NULL,
    html         TEXT,                          -- HTML como veio (compactacao fica por conta do Postgres TOAST)
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, url, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_raw_pages_source ON raw_pages(source);
CREATE INDEX IF NOT EXISTS idx_raw_pages_fetched ON raw_pages(fetched_at DESC);

-- Leads consolidados e normalizados
CREATE TABLE IF NOT EXISTS leads (
    id                BIGSERIAL PRIMARY KEY,
    fingerprint       TEXT UNIQUE NOT NULL,    -- hash(telefone || email || nome+source) — chave de dedup
    source            TEXT NOT NULL,           -- 'workana', '99freelas', 'gmaps', 'instagram', ...
    source_url        TEXT,
    nome              TEXT,
    telefone          TEXT,
    whatsapp_valido   BOOLEAN,                 -- validado posteriormente
    email             TEXT,
    email_valido      BOOLEAN,
    cnpj              TEXT,
    nicho             TEXT,                    -- imobiliaria, clinica, advocacia, ...
    cidade            TEXT,                    -- texto livre vindo do scrape
    cidade_tag        TEXT,                    -- enum: sao-luis | sao-paulo | curitiba | rio-de-janeiro | outras
    estado            TEXT,
    score_temperatura INT NOT NULL DEFAULT 0,  -- 0-100
    score_breakdown   JSONB,                   -- { "base": 40, "cidade": 15, "intent": 50, "nicho": 10 }
    raw_payload       JSONB,                   -- tudo que extraimos antes de normalizar
    status            TEXT NOT NULL DEFAULT 'novo', -- novo, contatado, respondeu, fechou, descartado
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_contacted_at TIMESTAMPTZ,
    last_deep_dive_at TIMESTAMPTZ,                -- ultima vez que rodou OSINT profundo
    notes             TEXT
);
-- Coluna nova (idempotente) — ALTER em vez de DROP/CREATE
ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_deep_dive_at TIMESTAMPTZ;
-- Falso lead: concorrente nichado / ja tem servico semelhante. Linha fica vermelha na tabela.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS falso_lead BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS falso_lead_motivo TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_score   ON leads(score_temperatura DESC);
CREATE INDEX IF NOT EXISTS idx_leads_cidade  ON leads(cidade_tag);
CREATE INDEX IF NOT EXISTS idx_leads_nicho   ON leads(nicho);
CREATE INDEX IF NOT EXISTS idx_leads_source  ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_status  ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_seen    ON leads(last_seen_at DESC);

-- Sinais de intencao explicita capturados (post pedindo servico)
CREATE TABLE IF NOT EXISTS intent_signals (
    id            BIGSERIAL PRIMARY KEY,
    lead_id       BIGINT REFERENCES leads(id) ON DELETE CASCADE,
    categoria     TEXT NOT NULL,               -- A_site | B_app | C_whatsapp | D_ia | E_software | F_generica
    palavra_chave TEXT NOT NULL,
    trecho_texto  TEXT,
    source_url    TEXT,
    boost         INT NOT NULL,                -- pontos somados ao score
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_intent_lead ON intent_signals(lead_id);

-- Log de execucoes de scraper
CREATE TABLE IF NOT EXISTS scrape_runs (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    pages_ok        INT NOT NULL DEFAULT 0,
    pages_failed    INT NOT NULL DEFAULT 0,
    leads_new       INT NOT NULL DEFAULT 0,
    leads_updated   INT NOT NULL DEFAULT 0,
    error_summary   TEXT,
    metadata        JSONB
);
CREATE INDEX IF NOT EXISTS idx_runs_source_started ON scrape_runs(source, started_at DESC);

-- Perfis sociais encontrados via OSINT (cross-reference)
CREATE TABLE IF NOT EXISTS social_profiles (
    id          BIGSERIAL PRIMARY KEY,
    lead_id     BIGINT REFERENCES leads(id) ON DELETE CASCADE,
    plataforma  TEXT NOT NULL,            -- instagram, github, twitter, facebook, linkedin, ...
    url         TEXT NOT NULL,
    fonte       TEXT NOT NULL,            -- username_pivot | dork | manual
    confianca   INT NOT NULL DEFAULT 50,  -- 0-100, quao confiantes estamos que e a pessoa certa
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (lead_id, plataforma, url)
);
CREATE INDEX IF NOT EXISTS idx_social_lead ON social_profiles(lead_id);

-- Falhas pra revisar manualmente
CREATE TABLE IF NOT EXISTS dead_letter (
    id          BIGSERIAL PRIMARY KEY,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    last_error  TEXT,
    retries     INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
