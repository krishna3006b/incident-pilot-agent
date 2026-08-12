-- IncidentPilot Supabase Database Schema
-- Enable pgvector extension for RAG search
CREATE EXTENSION IF NOT EXISTS vector;

-- Repositories Table
CREATE TABLE IF NOT EXISTS repositories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL UNIQUE,
    github_url VARCHAR(255) NOT NULL,
    default_branch VARCHAR(100) DEFAULT 'main',
    language VARCHAR(50) DEFAULT 'python',
    build_command VARCHAR(255),
    test_command VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Incidents Table
CREATE TABLE IF NOT EXISTS incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_number SERIAL UNIQUE,
    title VARCHAR(255) NOT NULL,
    service_name VARCHAR(100) NOT NULL,
    repository_id UUID REFERENCES repositories(id) ON DELETE SET NULL,
    severity VARCHAR(20) DEFAULT 'P2', -- P1, P2, P3, P4
    status VARCHAR(50) DEFAULT 'RECEIVED', -- RECEIVED, VALIDATING, INVESTIGATING, DIAGNOSING, FIXING, TESTING, PR_READY, WAITING_FOR_HUMAN_INPUT, MERGED, FAILED
    confidence FLOAT DEFAULT 0.0,
    summary TEXT,
    root_cause TEXT,
    candidate_patch TEXT,
    pr_url VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Agent Runs Table
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID REFERENCES incidents(id) ON DELETE CASCADE,
    state_history JSONB DEFAULT '[]'::jsonb,
    step_count INT DEFAULT 0,
    total_tokens INT DEFAULT 0,
    estimated_cost_usd NUMERIC(10, 4) DEFAULT 0.0000,
    status VARCHAR(50) DEFAULT 'RUNNING', -- RUNNING, COMPLETED, FAILED, TIMED_OUT
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Knowledge Base Table (Code, Past Incidents, Engineering Docs with pgvector embeddings)
CREATE TABLE IF NOT EXISTS knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id UUID REFERENCES repositories(id) ON DELETE CASCADE,
    document_type VARCHAR(50) NOT NULL, -- 'code', 'incident_resolution', 'runbook'
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding VECTOR(384), -- 384 dimensions for all-MiniLM-L6-v2 embeddings
    resolution_status VARCHAR(50) DEFAULT 'VERIFIED', -- VERIFIED, PROPOSED, FAILED
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create HNSW index for fast vector similarity search
CREATE INDEX IF NOT EXISTS knowledge_base_embedding_idx ON knowledge_base USING hnsw (embedding vector_cosine_ops);

-- Insert Sample Demo Repository
INSERT INTO repositories (name, github_url, default_branch, language, build_command, test_command)
VALUES ('payment-service', 'https://github.com/example/payment-service', 'main', 'python', 'pip install -r requirements.txt', 'pytest')
ON CONFLICT (name) DO NOTHING;
