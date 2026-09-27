# Internal Portal Administration Backend (MongoDB)

FastAPI backend modeled on the reference administration portal, with MongoDB replacing PostgreSQL.

## Security

- Microsoft Entra ID OAuth with an optional configurable email-domain allowlist
- Short-lived signed JWT sessions in `HttpOnly` cookies
- CSRF double-submit validation for cookie-authenticated mutations
- Token revocation on logout, MongoDB TTL cleanup, RBAC/PBAC permission dependencies
- Exact-origin credentialed CORS, trusted-host validation, secure response headers and rate limiting
- Soft deletion, immutable audit events, unique identities, production startup checks
- Secrets are environment-only; Swagger is disabled in production

## Run

```powershell
Copy-Item .env.example .env
# Set the Atlas URI, public HTTPS origins, strong secrets, and Microsoft OAuth credentials.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
python -m uvicorn server:app --host 127.0.0.1 --port 8002
```

Production mode fails startup if MongoDB, secure cookie settings, HTTPS origins, strong secrets, or Microsoft OAuth credentials are missing or invalid. API documentation endpoints are disabled.

For localhost development only, `APP_ENV=development` and `DEV_AUTH_BYPASS=true` may be used with loopback-only frontend, CORS, and trusted-host values. This bypasses Microsoft sign-in but never MongoDB. The application rejects public development origins and rejects the bypass whenever `APP_ENV=production`.

Seed the first administrator after configuring MongoDB:

```powershell
$env:ADMIN_EMAIL="admin@example.com"
python scripts/seed_admin.py
```

MongoDB creates the required collections and indexes during application startup. Use a replica set in production for durability and transactions, TLS-enabled `mongodb+srv://` credentials from a secret manager, network allowlists, encryption at rest, and a least-privilege database user.

Set `ALLOWED_EMAIL_DOMAINS` to a comma-separated list such as `example.com,example.org` to restrict Microsoft sign-in. Leave it empty to accept any email identity verified by the configured Microsoft tenant.

## Receipts, RFI, OCR, and Google Drive

### Deployment size

`requirements.txt` is the smaller serverless install: it includes RapidOCR and
ONNX Runtime, but excludes PaddlePaddle and PaddleOCR and their large dependency
tree. Set `OCR_ENGINE=rapid` in the deployment environment. `.vercelignore`
excludes local virtual environments, model caches, uploads, and development files.
Redeploy without the previous build cache after changing the dependency set;
verify the resulting function size in the deployment build output.

Drive storage uses the REST API with `google-auth[requests]` for service-account
authentication. The large `google-api-python-client` discovery library is not
needed for folder creation, file uploads, or downloads and is excluded from dependencies.

Local development uses `requirements-local.txt`, which adds PaddleOCR
and keeps `OCR_ENGINE=paddle` as the primary engine with RapidOCR as its fallback.
Installing into an existing environment does not uninstall old dependencies;
use a fresh environment when measuring the smaller install.

Receipt and RFI image uploads use lightweight PaddleOCR PP-OCRv5 mobile text detection and recognition locally, with RapidOCR as an automatic fallback; no paid OCR service is required. Optional orientation, document-layout, table, formula, seal, and page-unwarping models are not loaded. Documents can be categorized as gas, restaurant/client, maintenance, job expense, or other, and linked to a job, quote, or estimate reference. The reporting endpoint returns weekly, monthly, yearly, category, and profit/loss totals.

OCR previews run as durable background jobs. `POST /receipts/preview` returns a job ID immediately, and `GET /receipts/preview/{job_id}` returns `queued`, `processing`, `completed`, or `failed`. Temporary uploads remain in MongoDB GridFS so work survives a browser refresh; they are deleted after confirmation or automatically after one hour.

Without Drive configuration, files are stored under `uploads/Receipts/<year>/<category>` or `uploads/RFI/<year>/<category>`. To use Google Drive:

1. Create a Google Cloud service account and enable the Google Drive API.
2. Create a Drive folder and share it with the service account email as an editor.
3. Put the service account JSON on one line in `GOOGLE_DRIVE_CREDENTIALS_JSON` and set the shared folder ID in `GOOGLE_DRIVE_ROOT_FOLDER_ID`.

The backend then creates `Receipts/<year>/<category>` and `RFI/<year>/<category>` automatically.

Receipt endpoints require the `RECEIPTS_READ`, `RECEIPTS_CREATE`, `RECEIPTS_UPDATE`, and `RECEIPTS_DELETE` permissions. Super administrators automatically have access.

## Jobs

`GET /jobs` lists active job records, `POST /jobs` creates a job with a unique code, and `GET /jobs/{id}` groups its receipts, RFIs, quotes, and drawings. Receipt and RFI uploads attach to a job with `link_type=job` and `link_id=<job code>`; the API validates the job and normalizes the code. Quotes and drawings are PDF or image files uploaded to `POST /jobs/{id}/files` with `kind=quote` or `kind=drawing`. Local files are stored below `uploads/Jobs/<job ID>/<kind>`; Drive files are stored below `Jobs/<job code>/Quotes` or `Jobs/<job code>/Drawings`. Job routes use `JOBS_READ`, `JOBS_CREATE`, and `JOBS_UPDATE` permissions. Uploads keep the existing MIME and size limits, and RFIs remain excluded from financial reports.

Structured quotes can also be created with `POST /jobs/{id}/quotes` and edited with `PATCH /jobs/{id}/quotes/{quote_id}`. Each quote stores multiple items with name, description, quantity, unit price, source URL, and calculated totals. `POST /jobs/{id}/quotes/extract-item` accepts a public product URL and uses Beautiful Soup to read JSON-LD product data or page metadata for the item name, description, and price. The extractor rejects local/private hosts and oversized or non-HTML pages. Uploaded quote files remain available alongside structured quotes.

RFI uploads can include `rfi_number`, `rfi_subject`, `rfi_question`, `rfi_to`, and `rfi_due_at` fields. `GET /receipts/{rfi_id}/responses` lists responses, and `POST /receipts/{rfi_id}/responses` adds a dated answer with a responder name and optional PDF or image attachment. Response files can be opened at `GET /receipts/{rfi_id}/responses/{response_id}/file`. These endpoints verify the parent is an active RFI, require receipt read or update permission, audit added responses, and keep RFIs outside financial totals.

`POST /jobs/{job_id}/rfis` creates a structured RFI from its number, subject, question, recipient, request date, and optional response due date without requiring an uploaded file. Existing RFI scans can still be uploaded through the receipt flow and linked to the job.
