# Identity review: synthetic milestone

Checked 20 November 2026. This is a review aid for [draft PR #2](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/pull/2), not a security certification or an authorization to deploy.

## Evidence at exact revisions

| Evidence | Observed result | Boundary |
| --- | --- | --- |
| [PR #2 head `96c63b8`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/96c63b87da855c9e26ea8887f315c5b94edfde8b), [synthetic workflow](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43868834031) | PostgreSQL, synthetic Keycloak flow and demo jobs passed. | The Keycloak check used an automated client; it does not demonstrate company-trusted browser sign-in. |
| [Later stacked revision `9b70fa5`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/9b70fa549805c8754a0d9a95377a62e6a4444364), [synthetic browser run](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/44680650579) | Disposable CI browser test used separate analyst and reviewer contexts. | Different revision from #2; the CI-only CA is not evidence of company trust or staging. |
| [Phase 12 guide at #2](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/blob/96c63b87da855c9e26ea8887f315c5b94edfde8b/demo/README_PHASE12.md) | Documents local-session, membership and operating boundaries. | No independent security assessment or production acceptance. |

## Decisions and checks still needed

1. **Browser trust and endpoint:** identify an approved hostname and certificate from the responsible organisation, then validate sign-in in the intended environment with two separate synthetic users. Keep the certificate private key and credentials out of the repository. A personal identity document is not a server certificate.
2. **Session and revocation:** decide how sessions work across processes and how provider-side account disablement, expiry, logout and outage affect access. The current prototype uses process-local sessions; local membership checks do not immediately propagate every provider-side change.
3. **Tenant ownership and legacy data:** review server-owned membership and database access for each protected operation. Existing records have no tenant assignment; define a reviewed migration and isolated restore test before changing them. Application-level checks are not database row-level security.
4. **Security and operating review:** inspect identity-provider configuration, callback validation, cookie/CSRF settings, privilege boundaries, provisioning, offboarding and secret handling. Name an accountable reviewer and record findings and dispositions before proposing a staging or production release.
5. **Integration after review:** review the exact #2 diff and checks, then update and retest dependent drafts in order. Do not assume the later stacked browser test ran against #2 alone or that a passing synthetic test resolves these decisions.

The local browser certificate step was deferred; no company-approved certificate or staging validation is evidenced here. The original NAWA workspace, records and existing database volumes are outside this review.

## Session deadline follow-up

At integration-draft revision [`0bec81d`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/0bec81da2427e74d8fc5d2de3cac8e52ae663e80), the synthetic login flow anchors the verified identity-token deadline before session issuance and rechecks the remaining time after the membership lookup. A deliberately delayed lookup is rejected if the token deadline has passed; a separate test checks the shortened session lifetime. [Synthetic CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/44889693746) passed on that revision. This does not establish provider-side revocation, distributed sessions, trusted company certificates or a staging deployment.
