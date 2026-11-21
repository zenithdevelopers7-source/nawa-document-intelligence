# Synthetic answer-level evaluation

Run from the `demo` directory:

```powershell
python -m nawa_demo.synthetic_answer_evaluation --cases samples/retrieval/synthetic_answer_cases.json
```

This 12-case hand-authored fixture exercises the offline extractive function only. It has four cases labeled answerable and eight labeled for abstention, with English and Arabic present dates, paraphrases, explicit absence, unknown or pending values, a conflicting date and instruction-like source text. Labels are illustrative and were not independently reviewed. No real model or portal flow is exercised.

One local run reported six false answers among the eight abstention-labeled cases, two false abstentions among the four answerable cases, and no wrong expected excerpts among the answered positive cases. In particular, the current fixed-phrase rule catches the included "not specified" examples but labels unknown, pending, conflicting and instruction-like examples as answered. The instruction-like case tests output labeling only; it does not show that an instruction was executed. Paraphrases without matching tokens were withheld despite the fixture label.

These results are not an estimate of production accuracy or a safety certification. They show why lexical coverage and a short phrase list cannot establish semantic support. Keep generated answers and source text subject to human review. An independently reviewed bilingual dataset, a documented adjudication process, relevant adversarial cases and evaluation of the actual deployed pipeline remain open.

The offline CLI also provides an opt-in `--review-only` handoff. For example, from `demo`:

```powershell
python -m nawa_demo.qa_cli --tenant demo-a --principal reviewer-a --question "reviewer VAT deadline" --review-only
```

This mode returns no answer or answer citations. It shows up to five explicitly untrusted passage previews, marks truncation, and records a hash of the full passage text, not of the original source file. It does not call a model. A test confirms that all 12 fixture questions return no automatic answer in this mode, including the instruction-like source case; this does not establish that reviewers will interpret the evidence correctly. This CLI uses a static synthetic index, does not recheck identity or source freshness, and is not connected to the portal. It does not fix the default extractive mode's measured semantic failures or prevent infrastructure from logging data. Do not use it with confidential documents.

An opt-in synthetic HTTP review handoff formats results from the separate session-scoped retrieval experiment. It returns no answer or answer citations and labels previews as untrusted; tests use disposable PostgreSQL and browser identity fixtures to check grants and revocation. This does not create a review task or a final decision, and the handler is not enabled in the running portal. Authorization and source checks can race changes after the final read, infrastructure may log request bodies, and a UI would need to render untrusted text safely. Independent security and human-factors review remain open.

Separately, a bounded process-local `SyntheticReviewQueue` prototype creates pending requests from a current synthetic principal and a rechecked passage. It records the question and passage metadata plus a hash of passage text, not a generated answer or an original-file hash. Repeated requests from the same actor with the same key and content return one request; a different payload with the same key is rejected. Reading requires the owner or a reviewer who also has current passage access. Synthetic HTTP create and show endpoints are opt-in only in the disposable CI identity stack and require origin, CSRF and session checks. Tests cover concurrent retries, role and tenant checks, changed source and revoked access.

The original in-memory queue loses requests on process restart. A separate opt-in CI-only PostgreSQL experiment stores synthetic pending requests and lets a fresh process read them. It stores questions in plaintext and must never receive confidential or real data. The synthetic migration must be applied explicitly to a disposable test database; it is not an approved migration for an existing database. Neither queue is connected to the user's running portal, external agents or notifications, and neither supports completion or approval. Source and permission checks can race subsequent changes, backups can restore stale state, and no durable event or notification system exists. Reviewer delegation and retention policy are not approved. This is not an auditable or production-ready task system.
