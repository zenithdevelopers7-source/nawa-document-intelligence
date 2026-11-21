-- Synthetic QA only. Questions are stored in plaintext; never use real data.
CREATE TABLE nawa_synthetic_review_requests (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_hash char(64) NOT NULL CHECK (owner_hash ~ '^[0-9a-f]{64}$'),
    request_key char(32) NOT NULL CHECK (request_key ~ '^[0-9a-f]{32}$'),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 300),
    document_id uuid NOT NULL,
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    status text NOT NULL CHECK (status = 'pending_review'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, owner_hash, request_key),
    FOREIGN KEY (tenant_id, document_id) REFERENCES nawa_intake_documents(tenant_id, id) ON DELETE RESTRICT
);
