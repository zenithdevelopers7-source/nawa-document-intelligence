# Worker origin and result acceptance: decision draft

Status: **open for architecture and security review**. This document proposes evaluation criteria; it does not select or implement a production identity system. Scope is the NAWA document-intelligence drafts, not the separate v0.6.6 workspace.

## Problem and current evidence

An extraction result must not become trusted merely because its own JSON says it came from an approved worker. The Phase 15 prototype has a copied origin marker, not independently established worker identity. A separate [synthetic HMAC receipt experiment](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/blob/9b70fa549805c8754a0d9a95377a62e6a4444364/experiments/phase15/synthetic_worker_receipt.py) binds test identifiers and hashes. Its [disposable PostgreSQL ledger experiment](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/blob/9b70fa549805c8754a0d9a95377a62e6a4444364/experiments/phase15/synthetic_pg_receipt_ledger.py) explores one-time consumption. Neither experiment authenticates a deployed worker or protects a shared signing key from a verifier that holds it.

## Decision to make

The accountable platform and security owners should choose how a trusted coordinator authorizes each job, how worker identity is established independently of the worker's result, and where result acceptance and one-time use are enforced. Compare at least:

| Candidate | Benefit | Questions before selection |
| --- | --- | --- |
| Trusted coordinator with authenticated, isolated worker identity | Identity and job assignment can be checked at a service boundary. | Which platform issues identity? How are workload access, isolation, revocation and compromise handled? |
| Signed worker result with a separately managed verification key | A verifier need not hold the private signing key. | Who can sign, how is key custody protected, and how are rotation, revocation and compromised keys handled? A signature alone does not prove the extraction is correct. |
| Shared-key receipt | Simple experimental message integrity. | Every holder can generate receipts; how would key distribution and separation be controlled? The existing prototype does not answer this. |

These options can be combined, but no option is approved by this document. Record the chosen deployment boundary, owners, failure policy and tests in a reviewed decision before integration.

## Minimum acceptance contract to evaluate

- A server-owned job record binds tenant, document and version, original source digest, job/attempt identifier and the worker authorization. Client-submitted roles and result-supplied origin strings do not establish identity.
- The acceptance service authenticates the worker or verifies an approved signature, confirms the result corresponds to the assigned source and attempt, and applies tenant and job-state checks. Define permitted engine and model identities separately from result claims.
- Reject missing, altered, expired, revoked, cross-tenant or repeated results. Define a transaction and recovery design for result acceptance, one-time consumption and promotion; document the consistency boundary between the database and retained source files.
- A worker-origin check establishes only the stated origin and integrity under its threat assumptions. OCR text, financial amounts and generated claims remain untrusted until the applicable deterministic validation and human-review workflow is completed.
- Decide how keys or workload credentials are provisioned, rotated, disabled and audited without placing secrets in the repository, logs, review output or client browser.

## Evidence required before an acceptance gate

Use isolated synthetic environments to test valid results and positive controls, cross-tenant and wrong-job attempts, altered source/result, unauthorized or revoked worker, duplicate concurrent delivery, failure between transaction steps, restart, and recovery from both consistent and stale backups. Document what happens if the worker or a verifier is compromised, and obtain an independent security review. Capture exact revision, environment and test results; do not treat fixture tests as evidence for a real deployment.

Open owner decisions include the staging platform, identity provider for workloads, key custody, database/storage recovery model, and accountable reviewer. The current OCR path remains review-only; no origin or promotion guarantee is claimed.
