-- Synthetic QA only; no grants are inferred for existing documents.
CREATE TABLE nawa_synthetic_document_grants (
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    membership_generation bigint NOT NULL,
    identity_generation bigint NOT NULL,
    tenant_generation bigint NOT NULL,
    active boolean NOT NULL,
    PRIMARY KEY (tenant_id, document_id, subject_hash),
    FOREIGN KEY (tenant_id, document_id) REFERENCES nawa_intake_documents(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE nawa_synthetic_document_grant_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    subject_hash char(64) NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    actor_hash char(64) NOT NULL CHECK (actor_hash ~ '^[0-9a-f]{64}$'),
    action text NOT NULL CHECK (action IN ('grant', 'revoke')),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, document_id) REFERENCES nawa_intake_documents(tenant_id, id) ON DELETE RESTRICT
);
