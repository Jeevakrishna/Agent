-- 001_init.sql — PRCA core schema
-- pgvector extension is created by the docker-entrypoint init script (init-db.sql)
-- so we do not recreate it here.

-- ---------------------------------------------------------------------------
-- jurisdictions — geographic/administrative hierarchy (country -> state -> county -> city)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jurisdictions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    level text NOT NULL CHECK (level IN ('country', 'state', 'county', 'city')),
    parent_id uuid REFERENCES jurisdictions(id)
);

CREATE INDEX IF NOT EXISTS idx_jurisdictions_parent_id
    ON jurisdictions(parent_id);

CREATE INDEX IF NOT EXISTS idx_jurisdictions_level
    ON jurisdictions(level);

-- ---------------------------------------------------------------------------
-- rules — versioned regulatory text with vector embeddings (all-MiniLM-L6-v2, 384 dims)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    jurisdiction_id uuid NOT NULL REFERENCES jurisdictions(id),
    code_section text NOT NULL,
    text text NOT NULL,
    embedding vector(384),
    effective_date date,
    superseded_by uuid REFERENCES rules(id),
    source_url text NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rules_jurisdiction_id
    ON rules(jurisdiction_id);

CREATE INDEX IF NOT EXISTS idx_rules_superseded_by
    ON rules(superseded_by);

CREATE INDEX IF NOT EXISTS idx_rules_code_section
    ON rules(code_section);

-- pgvector cosine-similarity index for ANN search
CREATE INDEX IF NOT EXISTS idx_rules_embedding_ivfflat
    ON rules USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- rule_changes — audit trail of detected regulatory updates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rule_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id uuid NOT NULL REFERENCES rules(id),
    change_type text NOT NULL CHECK (change_type IN ('new', 'amended', 'repealed', 'clarification')),
    old_text text,
    new_text text NOT NULL,
    detected_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rule_changes_rule_id
    ON rule_changes(rule_id);

CREATE INDEX IF NOT EXISTS idx_rule_changes_detected_at
    ON rule_changes(detected_at);

CREATE INDEX IF NOT EXISTS idx_rule_changes_change_type
    ON rule_changes(change_type);

-- ---------------------------------------------------------------------------
-- projects — engineering / construction projects
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    jurisdiction_id uuid NOT NULL REFERENCES jurisdictions(id),
    occupancy_type text,
    metadata jsonb DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_projects_jurisdiction_id
    ON projects(jurisdiction_id);

CREATE INDEX IF NOT EXISTS idx_projects_occupancy_type
    ON projects(occupancy_type);

-- ---------------------------------------------------------------------------
-- design_snapshots — structured captures of a project's design at a point in time
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS design_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    source_agent text NOT NULL DEFAULT 'manual',
    payload jsonb NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_design_snapshots_project_id
    ON design_snapshots(project_id);

CREATE INDEX IF NOT EXISTS idx_design_snapshots_created_at
    ON design_snapshots(created_at);

-- ---------------------------------------------------------------------------
-- compliance_findings — output of the LangGraph agent's impact analysis
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compliance_findings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    rule_change_id uuid REFERENCES rule_changes(id),
    status text NOT NULL CHECK (status IN ('compliant', 'flagged', 'needs_review')),
    confidence numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    explanation text NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_compliance_findings_project_id
    ON compliance_findings(project_id);

CREATE INDEX IF NOT EXISTS idx_compliance_findings_rule_change_id
    ON compliance_findings(rule_change_id);

CREATE INDEX IF NOT EXISTS idx_compliance_findings_status
    ON compliance_findings(status);

CREATE INDEX IF NOT EXISTS idx_compliance_findings_created_at
    ON compliance_findings(created_at);
