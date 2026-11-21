# Phase 11: PostgreSQL persistence (synthetic development)

This is an opt-in synthetic single-user development mode. It is not authenticated, not a secure audit system, and not approved for confidential or production data. The Phase 10 memory-only mode remains the default.

## Configuration

The optional `compose.postgres.yaml` adds a PostgreSQL 17 service with a Compose-managed named volume and no published database port. It uses the local ignored `.env.postgres` file for development-only credentials. This is a **different volume** from the standalone `nawa-postgres` container's `nawa_pg_data`; it does not automatically copy existing data between them. Review the configuration and protect the unencrypted local credential file. Never commit or share it.

An example configuration check, without printing secrets:

```powershell
docker compose -f compose.yaml -f compose.postgres.yaml config --quiet
```

Once ready to replace a stopped memory-only demo, start the opt-in stack from the repository root:

```powershell
docker compose -f compose.yaml -f compose.postgres.yaml up --build -d
docker compose -f compose.yaml -f compose.postgres.yaml ps
```

Open `http://127.0.0.1:8765` on the same computer. The interface identifies the active storage mode. Stopping or recreating the prior in-memory application discards its in-memory records; this stack does not migrate them. The database starts empty in its new Compose-managed volume.

Do not start the override against a running Phase 10 demo without first understanding that recreating its application container clears in-memory quotations and reviews. A separate image and database test schema can be used for verification without changing the live dashboard. Do not run `docker compose down -v` or delete a database volume without an explicit, verified data-retention decision.

On startup in database mode, migrations are applied transactionally with stored checksums. Quotation JSON includes extraction evidence; review ledgers are updated under row locks with revision checks. Original uploaded files are not persisted. Hash-linked ledgers detect some accidental modification; they are not tamper-proof audit records. Actor IDs remain self-asserted.

Loading the same sample or file more than once creates separate quotation records; deduplication is not implemented. This mode preserves extracted fields and evidence, not the original uploaded bytes. It does not provide a data-retention policy or a production backup service.

## Backup and restore considerations

The isolated QA exercise created a PostgreSQL custom-format `pg_dump`, copied it to a uniquely named file outside the container, restored that saved file into a new QA database with `pg_restore`, and replayed the restored review through the application. This was a manual synthetic-data recovery exercise, not an automated backup service. For a development backup, select an approved private destination, create a fresh custom-format dump with `pg_dump -Fc`, copy the completed dump outside the container, and protect it according to the data's sensitivity. Verify the backup by restoring into a separate, newly created database and checking quotations and review replay. Include any separately managed originals and configuration in a complete recovery plan; this application does not store the originals. Document encryption, retention, ownership, scheduled backups and restore access before using sensitive data.

## Current acceptance status

Integration tests cover repeated migrations, database-backed English/Arabic records, concurrent decisions, invalid approval, stale revision and application-process restart. Local isolated QA verified an application-plus-database restart, a safe HTTP 503 while the QA database was stopped and recovery afterward, and a host-saved dump restored into a separate QA database with the quotation and review revision replayed by the application. These manual checks used synthetic data. The merged main commit [`823565f`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/823565f7c44c15b07719b91670fe8bbfb8d96521) passed [synthetic demo and PostgreSQL CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038065) and [Docker smoke CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038120). These results are evidence for the synthetic development scope, not an independent security audit or production-readiness approval. Authentication, source-file persistence, operational backups, access controls and production migration planning remain open.

The `/api/ready` endpoint checks access to the configured storage and is used by the Docker health check. This is a basic readiness check, not a full data-integrity or backup verification.

