-- IncidentPilot v2 Supabase Database Schema
-- Knowledge-First Architecture with pgvector, AST Symbols, Evidence, and Durable Jobs

-- Enable pgvector extension for RAG search
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- REPOSITORY METADATA (replaces old 'repositories' table)
-- ============================================================
CREATE TABLE IF NOT EXISTS repository_metadata (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_url        TEXT NOT NULL,
    name            TEXT NOT NULL UNIQUE,
    language        TEXT,
    framework       TEXT,
    build_system    TEXT,
    entry_points    JSONB DEFAULT '[]'::jsonb,
    dependencies    JSONB DEFAULT '[]'::jsonb,
    last_indexed_sha TEXT,
    default_branch  TEXT DEFAULT 'main',
    build_command   TEXT,
    test_command    TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- REPOSITORY SYMBOLS (AST-extracted classes, functions, methods)
-- ============================================================
CREATE TABLE IF NOT EXISTS repository_symbols (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id   TEXT,
    symbol_name     TEXT NOT NULL,
    symbol_type     TEXT NOT NULL,  -- class, function, method, module, route, interface
    file_path       TEXT NOT NULL,
    start_line      INT,
    end_line        INT,
    language        TEXT,
    signature       TEXT,           -- function signature or class declaration
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_symbols_repo ON repository_symbols(repository_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON repository_symbols(symbol_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON repository_symbols(file_path);

-- ============================================================
-- REPOSITORY EDGES (call-graph / dependency relationships)
-- ============================================================
CREATE TABLE IF NOT EXISTS repository_edges (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id     TEXT,
    source_symbol_id  UUID REFERENCES repository_symbols(id) ON DELETE CASCADE,
    target_symbol_id  UUID REFERENCES repository_symbols(id) ON DELETE CASCADE,
    relationship      TEXT NOT NULL,  -- calls, imports, extends, implements, reads, writes
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON repository_edges(source_symbol_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON repository_edges(target_symbol_id);

-- ============================================================
-- CODE EMBEDDINGS (pgvector semantic chunks)
-- ============================================================
CREATE TABLE IF NOT EXISTS code_embeddings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id   TEXT,
    commit_sha      TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    language        TEXT,
    symbol_name     TEXT,
    symbol_type     TEXT,
    start_line      INT,
    end_line        INT,
    content         TEXT NOT NULL,
    embedding       VECTOR(384),
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_repo ON code_embeddings(repository_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_file ON code_embeddings(file_path);
CREATE INDEX IF NOT EXISTS code_embeddings_embedding_idx ON code_embeddings USING hnsw (embedding vector_cosine_ops);

-- ============================================================
-- INDEX JOBS (durable, resumable indexing job state)
-- ============================================================
CREATE TABLE IF NOT EXISTS index_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id       TEXT,
    status              TEXT DEFAULT 'pending',  -- pending, running, completed, failed
    progress            INT DEFAULT 0,
    total_files         INT,
    current_file        TEXT,
    last_successful_file TEXT,
    processed_files     JSONB DEFAULT '[]'::jsonb,
    failed_files        JSONB DEFAULT '[]'::jsonb,
    commit_sha          TEXT,
    error               TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- INCIDENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS incidents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_number SERIAL UNIQUE,
    title           VARCHAR(255) NOT NULL,
    service_name    VARCHAR(100) NOT NULL,
    repository_id   TEXT,
    severity        VARCHAR(20) DEFAULT 'P2',
    status          VARCHAR(50) DEFAULT 'RECEIVED',
    confidence      FLOAT DEFAULT 0.0,
    summary         TEXT,
    root_cause      TEXT,
    candidate_patch TEXT,
    pr_url          VARCHAR(255),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- EVIDENCE (first-class evidence objects with IDs)
-- ============================================================
CREATE TABLE IF NOT EXISTS evidence (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     UUID REFERENCES incidents(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,  -- stack_trace, commit, code_chunk, historical, runbook, dependency
    source          TEXT,           -- slack_webhook, pgvector_rag, github_api, resolution_memory
    reference       TEXT,           -- file path, commit sha, incident ID
    content         TEXT,
    relevance_score FLOAT DEFAULT 0.0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_id);
CREATE INDEX IF NOT EXISTS idx_evidence_type ON evidence(type);

-- ============================================================
-- AGENT RUNS
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     UUID REFERENCES incidents(id) ON DELETE CASCADE,
    state_history   JSONB DEFAULT '[]'::jsonb,
    step_count      INT DEFAULT 0,
    tool_calls_used INT DEFAULT 0,
    total_tokens    INT DEFAULT 0,
    estimated_cost_usd NUMERIC(10, 4) DEFAULT 0.0000,
    status          VARCHAR(50) DEFAULT 'RUNNING',
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- KNOWLEDGE BASE (runbooks, past resolutions)
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_base (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id   TEXT,
    document_type   VARCHAR(50) NOT NULL,
    title           VARCHAR(255) NOT NULL,
    content         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}'::jsonb,
    embedding       VECTOR(384),
    resolution_status VARCHAR(50) DEFAULT 'VERIFIED',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_base_embedding_idx ON knowledge_base USING hnsw (embedding vector_cosine_ops);

-- ============================================================
-- RPC: Semantic search over code embeddings
-- ============================================================
DROP FUNCTION IF EXISTS match_code(VECTOR(384), INT, UUID);
DROP FUNCTION IF EXISTS match_code(VECTOR(384), INT, TEXT);

CREATE OR REPLACE FUNCTION match_code(
    query_embedding VECTOR(384),
    match_count INT DEFAULT 10,
    repo_id TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    file_path TEXT,
    symbol_name TEXT,
    symbol_type TEXT,
    content TEXT,
    similarity FLOAT
) AS $$
    SELECT
        ce.id,
        ce.file_path,
        ce.symbol_name,
        ce.symbol_type,
        ce.content,
        1 - (ce.embedding <=> query_embedding) AS similarity
    FROM code_embeddings ce
    WHERE (repo_id IS NULL OR ce.repository_id = repo_id)
    ORDER BY ce.embedding <=> query_embedding
    LIMIT match_count;
$$ LANGUAGE sql;

-- ============================================================
-- SEED DATA
-- ============================================================
INSERT INTO repository_metadata (name, repo_url, language, framework, build_system, default_branch, build_command, test_command)
VALUES (
    'ordering-system',
    'https://github.com/krishna3006b/ordering-system',
    'TypeScript',
    'Next.js',
    'npm',
    'main',
    'npm run build',
    'npm test'
)
ON CONFLICT (name) DO NOTHING;
