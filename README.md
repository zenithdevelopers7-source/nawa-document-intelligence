# NAWA | Bilingual Document Intelligence

**Enterprise Document AI • Arabic & English OCR • Evidence-Linked Extraction • RAG • Human-in-the-Loop Review**

**Featured case study:** [NAWA Case Study — Arabic and English quotation intelligence](docs/07-case-study/NAWA_CASE_STUDY.md). Explore the implemented scope, bilingual financial example, architecture decisions, verified CI results, reproduced evaluation checks and enterprise delivery roadmap.

NAWA is an enterprise document intelligence project designed to help procurement teams process and compare Arabic and English supplier quotations.

The system combines document processing, structured information extraction, evidence-linked retrieval, deterministic financial comparisons, and human review to make procurement decisions more traceable.

**Project type:** Enterprise AI engineering portfolio  
**Domain:** Procurement and supplier quotation intelligence  
**Development role:** Sole developer  
**Data:** Synthetic documents for portfolio demonstrations  
**Status:** Development project; production readiness not established

---

## Try the synthetic text demo

From the repository root with Python 3, compare two bundled synthetic quotes:

```powershell
python -m demo.nawa_demo demo/samples/english_quote.txt demo/samples/arabic_quote.txt
```

To see an invalid VAT calculation excluded from the price-only ranking:

```powershell
python -m demo.nawa_demo demo/samples/english_quote.txt demo/samples/incorrect_vat_quote.txt
```

The demo uses fixed-format UTF-8 text, deterministic AED/VAT checks and source-line evidence. It is not an OCR accuracy benchmark or a procurement award decision. See [demo setup and limitations](demo/README.md), [synthetic samples](demo/samples/) and the [local browser dashboard](demo/README_PHASE9.md).

## Implementation and evidence status

As checked on 20 November 2026, main includes the synthetic demo and optional Phase 11 PostgreSQL persistence. [Identity (PR #2)](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/pull/2), [intake (PR #3)](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/pull/3), [retrieval (PR #4)](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/pull/4) and [review-only OCR (PR #5)](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/pull/5) are stacked drafts, not features merged into main. See the [evidence and open-work register](docs/NAWA_IMPLEMENTATION_STATUS.md) for scope and exact revisions.

The main commit [`823565f`](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/commit/823565f7c44c15b07719b91670fe8bbfb8d96521) passed [synthetic demo/PostgreSQL CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038065) and [Docker smoke CI](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038120). These checks do not establish production readiness or general OCR accuracy.

---

## 1. Business Problem

Supplier quotation reviews often involve scanned PDFs, mixed Arabic and English content, inconsistent document layouts, and manually checked financial information.

These challenges can create:

- Time-consuming document review.
- Errors in extracted totals, VAT, and currency values.
- Difficulty tracing extracted information to its source.
- Inconsistent comparisons between supplier quotations.
- Limited auditability of manual corrections.

**NAWA addresses these challenges through document intelligence, evidence-linked extraction, and controlled human review.**

## 2. Solution Overview

The NAWA solution is organized around six capabilities:

| Capability | Purpose |
|---|---|
| Document ingestion | Accept and process supplier quotation documents |
| Bilingual OCR | Extract content from Arabic and English documents |
| Structured extraction | Identify supplier details, monetary values, and other quotation fields |
| Evidence-linked retrieval | Connect extracted information to source documents and pages |
| Quotation comparison | Identify discrepancies, including VAT and total differences |
| Human review | Allow reviewers to inspect evidence and correct disputed values |

The architecture also addresses document permissions, processing workflows, auditability, and cloud operations.

Implementation and verification status must be assessed separately for each capability.

## 3. System Architecture

NAWA's architecture documentation describes the following logical flow:

**Document Upload → Validation → OCR & Extraction → Structured Data & Evidence → Retrieval & Comparison → Human Review**

Supporting concerns include:

- Identity and access control.
- Document and version management.
- Processing jobs and failure handling.
- Database persistence.
- Retrieval and citation integrity.
- Audit records.
- Cloud deployment and monitoring.

See [System Architecture](docs/02-system-architecture/NAWA_System_Architecture.pdf) and [Architecture Decisions](docs/02-system-architecture/NAWA_Architecture_Decisions.pdf).


### NAWA Conceptual Architecture

![NAWA Document Intelligence Architecture](docs/02-system-architecture/NAWA%20Document%20Intelligence%20Architecture.png)

*Conceptual architecture for Arabic and English document processing, structured extraction, retrieval, quotation comparison, and human review.*


### Azure AI Reference Architecture

![Azure Machine Learning and Microsoft Foundry Architecture](docs/02-system-architecture/Azure_ML_Microsoft_Foundry_Architecture.png)

*Reference architecture illustrating an Azure-based document-processing and evaluation workflow. This is not a verified NAWA deployment.*

## 4. Document AI and RAG

NAWA explores a document-processing architecture that combines OCR, structured field extraction, and evidence-linked retrieval.

The retrieval design includes a lexical BM25 baseline and optional embedding-based retrieval and fusion.

Important engineering considerations include:

- Arabic and English document normalization.
- Page-linked source references.
- Extraction confidence and missing-field handling.
- Document version consistency.
- Permission-aware retrieval.
- Human verification of uncertain results.

See [Document AI & RAG](docs/03-document-ai-rag/NAWA_Document_AI_and_RAG.pdf).

## 5. Human-in-the-Loop Review

The review workflow is designed to preserve human accountability.

A reviewer should be able to:

1. Inspect a supplier quotation.
2. Review an extracted financial value.
3. Follow the evidence reference to its original page.
4. Identify a discrepancy in VAT or totals.
5. Record a correction or review decision.
6. Recalculate or refresh the affected comparison.
7. Preserve a traceable review history.

See [Review Workflow](docs/03-document-ai-rag/NAWA_Review_Workflow.pdf).

## 6. Technology and Engineering

The project documentation and reported development environment reference the following technologies:

| Area | Technology or approach |
|---|---|
| Application | Web interface, backend APIs, processing workflows |
| Database | PostgreSQL |
| Supporting services | Redis |
| Deployment | Docker and AWS EC2 development workspace |
| HTTPS ingress | Caddy |
| Authentication | AWS Cognito with Google sign-in in the reported AWS environment |
| Local identity validation | Keycloak with PKCE |
| Document intelligence | OCR, structured extraction, evidence-linked retrieval |
| Search design | BM25 baseline with optional vector retrieval |

**Note:** This table describes the reported environment and architecture. It does not independently verify that every component is currently running or publicly reproducible.

## 7. Testing and Evaluation

The project résumé reports:

- 123 passing local automated tests.
- Local backup and restore validation.
- Cognito sign-in in the AWS development workspace.

These are previously reported observations, not independently reproduced results in this repository.

### Evaluation work remaining

The following results require published benchmark evidence:

- Arabic and English OCR character error rates.
- Structured extraction accuracy.
- Retrieval Recall@5.
- Citation correctness.
- Permission-isolation test outcomes.
- Processing latency and reliability.
- Cloud recovery measurements.

The proposed bilingual evaluation pilot uses 60 synthetic or permissioned documents, with balanced Arabic, English, and mixed-language groups.

See [Evaluation Plan](docs/05-testing-evaluation/NAWA_Arabic_and_English_Evaluation_Plan.pdf) and [Evaluation Report](docs/05-testing-evaluation/NAWA_Evaluation_Report.pdf).

## 8. Security and Governance

NAWA's security design addresses:

- Authentication and role-based access.
- Tenant and document authorization boundaries.
- Source evidence permissions.
- Audit logging.
- Secure handling of uploaded documents.
- Data retention and deletion.
- Human oversight of uncertain AI outputs.

These are documented design considerations. Control-level implementation and test evidence must be linked before claiming verified enforcement.

See [Security Design](docs/04-security-governance/NAWA_Security_Design.pdf) and [AI Governance & Risk Assessment](docs/04-security-governance/NAWA_AI_Governance_and_Risk_Assessment.pdf).

## 9. Cloud and Operations

The reported AWS development environment includes containerized application services, HTTPS ingress, database infrastructure, and identity integration.

The operations documentation covers proposed procedures for:

- Deployment and rollback.
- Monitoring and alerting.
- Backup and restoration.
- Incident handling.
- Disaster recovery.
- Production readiness assessment.

A documented procedure is not equivalent to a completed operational test.

See [Cloud Operations](docs/06-cloud-devops/NAWA_Cloud_Operations.pdf).

## 10. Technical Documentation

### Business and requirements

- [Project Vision & Scope](docs/01-business-requirements/NAWA_Project_Vision_and_Scope.pdf)
- [Business Requirements Document](docs/01-business-requirements/NAWA_BRD_Download_Fixed.pdf)
- [Software Requirements Specification](docs/01-business-requirements/NAWA_SRS_Download_Fixed.pdf)

### Architecture and API design

- [System Architecture](docs/02-system-architecture/NAWA_System_Architecture.pdf)
- [Architecture Decisions](docs/02-system-architecture/NAWA_Architecture_Decisions.pdf)
- [Database Architecture & ERD](docs/02-system-architecture/NAWA_Database_Architecture_and_ERD.pdf)
- [API Design & Integration Specification](docs/02-system-architecture/NAWA_API_Design_and_Integration_Specification.pdf)

### Document AI and evaluation

- [Document AI & RAG](docs/03-document-ai-rag/NAWA_Document_AI_and_RAG.pdf)
- [OCR & Bilingual Extraction Specification](docs/03-document-ai-rag/NAWA_OCR_and_Bilingual_Extraction_Specification.pdf)
- [Arabic & English Evaluation Plan](docs/05-testing-evaluation/NAWA_Arabic_and_English_Evaluation_Plan.pdf)
- [Evaluation Report](docs/05-testing-evaluation/NAWA_Evaluation_Report.pdf)
- [Testing & Quality Assurance Strategy](docs/05-testing-evaluation/NAWA_Testing_and_Quality_Assurance_Strategy.pdf)

### Security and governance

- [Security Design](docs/04-security-governance/NAWA_Security_Design.pdf)
- [AI Governance & Risk Assessment](docs/04-security-governance/NAWA_AI_Governance_and_Risk_Assessment.pdf)
- [Review Workflow](docs/03-document-ai-rag/NAWA_Review_Workflow.pdf)

### Cloud and production engineering

- [Cloud Operations](docs/06-cloud-devops/NAWA_Cloud_Operations.pdf)
- [DevOps, CI/CD & Deployment Guide](docs/06-cloud-devops/NAWA_DevOps_CICD_and_Deployment_Guide.pdf)
- [Monitoring & Observability Architecture](docs/06-cloud-devops/NAWA_Monitoring_and_Observability_Architecture.pdf)
- [Disaster Recovery & Business Continuity](docs/06-cloud-devops/NAWA_Disaster_Recovery_and_Business_Continuity_Plan.pdf)
- [Production Readiness Assessment](docs/06-cloud-devops/NAWA_Production_Readiness_Assessment.pdf)

### Case study

- [NAWA Case Study](docs/07-case-study/NAWA_CASE_STUDY.md)

## 11. Demonstration Workflow

The intended portfolio demonstration follows this sequence:

1. Upload synthetic Arabic and English supplier quotations.
2. Inspect extracted quotation fields.
3. Follow a field citation to the original document page.
4. Compare quotation totals and VAT.
5. Review a discrepancy.
6. Record a correction.
7. Inspect the updated comparison and review history.

A recorded walkthrough and reproducible setup instructions will be added after verification.

## 12. Engineering Evidence Roadmap

| Evidence | Current publication status |
|---|---|
| Architecture and technical documentation | Available |
| Business and software requirements | Available as drafts |
| Source-code implementation | [Runnable synthetic demo](demo/); later phases are drafts and the full target enterprise platform is not established |
| Reproducible automated test run | [Core tests](demo/tests/test_core.py) and [pinned hosted test run](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038065): general job 122 run with 2 skipped; separate PostgreSQL job 2 passed |
| Bilingual OCR and extraction benchmark | Small synthetic experiments; independently reviewed evaluation pending |
| Retrieval and citation benchmark | Synthetic experiment; independent evaluation pending |
| Security verification report | Pending |
| CI/CD execution evidence | [Synthetic test workflow](.github/workflows/demo-tests.yml) and [successful Docker smoke run](https://github.com/zenithdevelopers7-source/nawa-document-intelligence/actions/runs/43840038120); no cloud promotion established |
| Application screenshots and demo | [Local synthetic dashboard guide](demo/README_PHASE9.md); current UI screenshots and recorded walkthrough remain pending |
| Production readiness approval | Not established |

This table describes evidence available through the public portfolio, not necessarily all work completed privately.

## 13. Engineering Principles

NAWA is developed around several enterprise engineering principles:

- **Traceability:** AI-generated or extracted values should reference source evidence.
- **Human accountability:** Uncertain or disputed outputs require review.
- **Deterministic validation:** Financial calculations should use explicit, testable rules.
- **Least privilege:** Users should only access authorized documents.
- **Reproducibility:** Evaluations should identify datasets, versions, and configurations.
- **Operational reliability:** Failures, retries, deployment changes, and recovery should be observable and testable.

## 14. Project Limitations

The repository includes runnable synthetic demos and technical documentation. Representative bilingual evaluation datasets, a verified staging deployment, current enterprise cloud deployment evidence, and production assurance remain open.

Architecture specifications and production plans should be interpreted as engineering design documents unless accompanied by implementation and verification evidence.

---

**NAWA demonstrates the design and engineering approach for a bilingual enterprise document intelligence system, with ongoing work to strengthen reproducible implementation, evaluation, security, and operational evidence.**


## Synthetic PDF ingestion demonstration

The [page-aware PDF demo](demo/README_PDF.md) extends the existing quotation parser to **selectable-text PDFs**. It uses PyMuPDF to extract text from each page, then reuses the existing deterministic financial validation and comparison code. Each field includes a page number and extracted-text line evidence. [Six PDF ingestion tests](demo/tests/test_pdf_ingest.py) are included, alongside small synthetic PDF fixtures.

**Limitations:** This is **not OCR**. It does not process scanned PDFs, preserve bounding-box citations, validate real-world Arabic PDF extraction, implement RAG, or constitute a production procurement system. The separate UTF-8 Arabic text demonstration remains available. In particular, multilingual PDF layout and text-order quality still require evaluation.

## Phase 3: Arabic PDF and optional scanned-document OCR

The [Phase 3 guide](demo/README_PHASE3.md) demonstrates Arabic selectable-text PDF parsing and an explicit Tesseract OCR fallback for image-only pages. The included scanned OCR regression fixture is English; Arabic scanned-document accuracy has not been validated. Evidence includes source page, extracted line and extraction method. This is a synthetic, rules-based demonstration, not a production OCR benchmark or RAG system.

## Phase 4 runnable synthetic retrieval baseline

The [`demo` retrieval module](demo/README_PHASE4.md) adds permission-filtered bilingual BM25 retrieval with document/page/version/source evidence, optional selectable-text PDF page indexing, a small three-query synthetic Recall@5 evaluation, and regression tests. It **does not** implement LLM answering, embedding search, production identity or access control, or a deployed RAG platform. Caller-provided principal/tenant values only simulate a trusted backend.

### Phase 5 runnable demo: extractive grounded Q&A

The synthetic CLI now answers questions by quoting an **authorized** retrieval passage verbatim, with page, document ID, version and source citations. Unsupported questions abstain rather than fabricate an answer. This is **deterministic extractive Q&A, not an LLM or production RAG service**. See [Phase 5 demo](demo/README_PHASE5.md).

### Phase 6 - Optional cited model-generation demo

A mock-first RAG experiment now adds an optional OpenAI generation provider to the Phase 4 permission-filtered retrieval and Phase 5 extractive QA flow. The default offline provider is deterministic; CI does **not** call external models. Citation validation requires exact excerpts from authorized retrieved passages and a conservative token-coverage heuristic (not semantic entailment). See [Phase 6 instructions](demo/README_PHASE6.md). Live API behavior, cost, authorization and production readiness are unverified.

### Phase 7 — Evaluation and security regression suite

The `demo/nawa_demo/evaluation.py` harness and `demo/nawa_demo/eval_cli.py` provide reproducible, **offline synthetic** status/citation checks with scripted adversarial responses. Run from `demo/`: `python3 -m nawa_demo.eval_cli` and `python3 -m unittest discover -s tests -v`. See [Phase 7 limitations](demo/README_PHASE7.md). These checks are not a production security assessment or validation of a live LLM.

## Phase 8: Human-in-the-loop review (synthetic offline demo)

The `demo/nawa_demo/review.py` module provides hash-linked local correction and review-decision history, evidence references, revision checks, and financial revalidation. Run with `python3 -m nawa_demo.review_cli` from `demo/`. See [Phase 8 guide](demo/README_PHASE8.md). This demonstration uses self-asserted reviewer IDs and local JSON files; it is **not** authenticated approval, a secure/tamper-proof audit system, or production procurement software.

### Phase 9: Local web dashboard (synthetic)

A dependency-light localhost dashboard makes the existing synthetic quotation extraction, AED/VAT checks, supplier comparison, source-line evidence, and human review workflow accessible through a browser. Supports bundled Arabic and English examples and synthetic UTF-8 TXT/selectable-text PDF uploads. Runs entirely on `127.0.0.1:8765`, stores session information only in memory, and does not invoke an LLM. **Not authenticated or suitable for production deployment.** See [Phase 9 demo guide](demo/README_PHASE9.md).

## Phase 10 - Local Docker dashboard demo

The synthetic dashboard from Phase 9 now has an optional Docker/Compose launch path, intended for a **single, trusted office or personal computer**. From the repository root, run `docker compose up --build -d` and open **http://127.0.0.1:8765** on that same machine. Stop it with `docker compose down`. The container is non-root, uses read-only root filesystem, and exposes the port only on host localhost. **Not a production/multiuser web service.** Full Windows and macOS prerequisites, safety boundaries, and a demo walkthrough are in [Phase 10](demo/README_PHASE10.md). A separate GitHub Actions Docker smoke workflow runs on pushes and pull requests.

## Phase 11 - Optional PostgreSQL development storage

The synthetic dashboard can optionally store extracted quotations and review ledgers in PostgreSQL across restarts. It remains unauthenticated and is not a production service; original uploaded files are not retained. Memory-only mode remains the default. See the [Phase 11 guide](demo/README_PHASE11.md) for configuration, migration, QA and recovery boundaries before switching modes. Existing in-memory records are not automatically migrated.

