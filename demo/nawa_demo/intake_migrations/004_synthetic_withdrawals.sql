-- Synthetic QA only. A withdrawal is one-way in this experiment.
CREATE TABLE nawa_synthetic_withdrawals (
    tenant_id text NOT NULL,
    document_id uuid NOT NULL,
    actor_hash char(64) NOT NULL CHECK (actor_hash ~ '^[0-9a-f]{64}$'),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES nawa_intake_documents(tenant_id, id) ON DELETE RESTRICT
);
