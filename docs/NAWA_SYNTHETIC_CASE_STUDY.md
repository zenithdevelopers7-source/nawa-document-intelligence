# NAWA: bilingual quotation review with traceable evidence

**Portfolio status:** synthetic development prototype and open drafts, checked 20 November 2026. This case study describes verified project evidence, not a production deployment or a procurement award system.

## Business problem

Supplier quotations can use different layouts, Arabic and English text, and inconsistent financial figures. A reviewer needs to inspect the source, understand VAT and total checks, compare eligible quotations and record a decision without treating generated or extracted text as an approval.

## Demonstrated approach

The [main-branch synthetic demo](../README.md#try-the-synthetic-text-demo) extracts fields from fixed-format text and applies deterministic AED/VAT checks. It can compare eligible quotations and show source-line evidence. The local dashboard and optional PostgreSQL persistence are development features; main does not include the later identity and intake drafts.

The **unmerged integration draft** explores the following synthetic journey:

```text
Browser sign-in → tenant-scoped intake → retained source and extraction
    → source-linked correction → separate reviewer → comparison
```

The draft separates analyst and reviewer roles. A configurable source-checking mode compares a cited line against a retained, hash-checked synthetic TXT or text-layer PDF source. A line match establishes an exact extracted-text match at the check time; it does not establish that a correction is semantically correct or that PDF text matches what a person sees. Review decisions and financial calculations remain distinct from model output. See the [review-aware comparison notes](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/blob/a5d767228cf4a5007f4dadf720bfb17db2dc7af9/demo/README_REVIEW_COMPARISON.md).

## Evidence to show

| Claim | Evidence and limit |
| --- | --- |
| Runnable quotation comparison | [Main revision `823565f`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/823565f7c44c15b07719b91670fe8bbfb8d96521): two synthetic CLI examples and eight core tests were run on Windows. The result is a price-only synthetic comparison, not a purchase decision. |
| Identity and tenant controls | [Integration draft `799d753`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/799d7530ee901867a3decef8836d4c0fc716c45d): [synthetic browser and PostgreSQL workflow](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/44731347194) passed with separate browser identities, tenant isolation checks and reviewer separation. The browser trusted a disposable CI-only certificate. |
| Revocation and expiry | The same draft uses database-assigned generations to invalidate process-local sessions after tested local membership, identity or tenant active-state changes between requests. A fresh sign-in was tested after reactivation. Provider-side or distributed revocation and behavior after a database restore remain open. |
| Recovery | [Synthetic intake CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/44698522487) tested a quiesced database-and-file backup/restore, migration checks and a synthetic upgrade in disposable databases. A [later stale-restore experiment](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/44734054977) demonstrated that a running process could accept an old session after restoring a pre-change snapshot. Coordinated recovery remains open; neither test verifies a live backup of an existing workspace. |
| Bilingual OCR limitation | The [saved two-sample Tesseract baseline](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/blob/a5d767228cf4a5007f4dadf720bfb17db2dc7af9/experiments/phase15/baseline-result.json) recorded 6/6 exact fields for one English synthetic sample and 2/6 for one Arabic-label sample. These are not representative accuracy estimates; OCR remains review-only. |

## Architecture and governance choices to discuss

The [architecture and evidence map](NAWA_ARCHITECTURE_MAP.md) shows the synthetic users, components, quotation journey and remaining trust boundaries.

- Resolve tenant and role from server-side identity and membership; the draft uses local, process-bound sessions and application-level checks, not a verified production tenancy boundary.
- Treat source documents and OCR text as untrusted data. Keep original source evidence distinct from a human correction and require a separate reviewer for approval in the tested workflow.
- Use deterministic checks for financial calculations. The retrieval and claim-support experiments are not an automatic financial or approval gate.
- Keep recovery claims tied to the exact tested synthetic database, source files and revision. Review migrations and recovery before any change to existing data.
- Treat worker identity, retention, independent bilingual evaluation, provider-side revocation, approved certificate trust and staging as open work. The [worker-origin decision draft](NAWA_WORKER_ORIGIN_DECISION.md) describes options rather than an approved design.

## Suggested five-minute walkthrough

1. Run the main-branch synthetic comparison and point to the source lines and VAT checks.
2. Explain which capabilities are in main and which are open drafts. Show the integrated CI run for the synthetic browser journey.
3. Explain an observed limitation: the small Arabic OCR baseline missed financial fields and therefore needs human review and an independently reviewed evaluation set.
4. Describe tenant and reviewer separation, then explain the limits of the disposable CI certificate and process-local sessions.
5. Discuss a recovery test and the remaining worker-origin and deployment decisions. State your own specific contribution and distinguish it from collaborator or automated work.

The [implementation evidence register](NAWA_IMPLEMENTATION_STATUS.md) records the current draft stack and open work. Do not describe this prototype as a production deployment or claim all project phases are complete based on these checks.
