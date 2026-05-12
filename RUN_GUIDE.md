# PM Automation System — Complete Run Guide

## Architecture Overview

This system implements the **PM Automation System** exactly as designed in the two provided diagrams:

| Diagram Layer | What it does |
|---|---|
| **Client Layer** | Supervisor / Technician / Manager / Security roles enforced via Azure AD RBAC |
| **FastAPI Application** | REST API — generate, history, library, machines, manual upload, checklist, export |
| **Core Modules** | PM Library (5 machines, 228 tasks, 48 intervals), Document Generator (PDF/DOCX/XLSX), Storage, History, Analytics |
| **RAG Pipeline** | PDF → Classify → Find Chapter → Chunk (500w/103w) → Embed → Vector Store → RAG Retrieve Top 10 → AI Extract → Engineer Review → Library |
| **AI Models Layer** | Dual provider: IBM watsonx.ai (Data Flow Diagram) **or** OpenAI (Architecture Diagram) — switchable via env var |
| **Storage (Azure)** | Azure Blob, Azure SQL DB, FTP Server, Azure Key Vault, Azure AI Search |
| **Monitoring** | Azure App Insights, Key Vault, HTTPS/TLS, Audit Logs, Threat Detection |

---

## AI Models — Which Are Used?

### IBM watsonx.ai (Data Flow Diagram — Primary)

Set `AI_PROVIDER=watsonx` in `.env`.

| Role | Model ID | Where Used |
|------|----------|------------|
| Classification | `ibm/granite-13b-instruct-v2` | Detect manufacturer (Krones/Other) from PDF first page |
| Embedding | `ibm/slate-125m-english-rtrvr` (1024 dims) | Embed PDF chunks → Azure AI Search vector store |
| Generation | `ibm/granite-3b-code-instruct` | Extract structured JSON PM tasks from manual chunks |
| Analytics | `ibm/granite-13b-instruct-v2` | Predict next PM due dates, analyse overdue patterns |

**IAM Authentication** — IBM watsonx requires an IAM Bearer token obtained by exchanging the API key at `https://iam.cloud.ibm.com/identity/token`. The system handles this automatically via `app/rag/watsonx_auth.py`. Tokens are cached and refreshed before expiry.

**How to get credentials:**
1. Go to [cloud.ibm.com](https://cloud.ibm.com)
2. Create a watsonx.ai project
3. Manage → Access (IAM) → API Keys → Create API key
4. Copy Project ID from the project settings

### OpenAI (Architecture Diagram — Alternative)

Set `AI_PROVIDER=openai` in `.env`.

| Role | Model ID | Where Used |
|------|----------|------------|
| Classification | `gpt-4o-mini` | Detect manufacturer from PDF |
| Embedding | `text-embedding-3-large` (3072 dims) | Embed PDF chunks |
| Generation | `gpt-4o` (128k context) | Extract structured JSON PM tasks |

### Switching providers:
```env
# IBM watsonx.ai (Data Flow Diagram):
AI_PROVIDER=watsonx
WATSONX_API_KEY=your-ibm-iam-api-key
WATSONX_PROJECT_ID=your-project-id

# OpenAI:
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

---

## Quick Start — Local Development (No Cloud Required)

### Prerequisites
- Python 3.11+ 
- pip

### Step 1 — Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2 — Configure Environment
```bash
# Copy example env
copy .env.example .env

# Minimum for local dev (no AI, no Azure, everything else is optional):
# APP_ENV=development      ← already set
# DEV_API_KEY=dev-...      ← already set
# DEFAULT_STORAGE_TARGET=local  ← already set
# AI_PROVIDER=openai       ← set OPENAI_API_KEY only if you want RAG pipeline
```

### Step 3 — Run the Server
```bash
# Recommended (with hot-reload):
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Or via main.py:
python app/main.py
```

The server:
1. Loads secrets from Azure Key Vault (skipped if `AZURE_KEY_VAULT_URL` is blank)
2. Creates SQLite database at `data/pm_automation.db`
3. Auto-seeds PM library from `data/pm_library.json` (5 machines, all tasks)
4. Starts on port 8000

### Step 4 — Open the Dashboard
```
http://localhost:8000/frontend/index.html
```
- Enter any email address
- Select role: **Manager** for full access
- Click **Sign In**

### Step 5 — Get a Dev API Token (for direct API testing)
```bash
curl -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@factory.com","name":"Admin","role":"Manager"}'
```
Copy `access_token` from the response.

---

## Seed & Generate All PM Documents

```bash
# Seed database from pm_library.json (also runs automatically on startup)
python scripts/seed_database.py

# Generate all 16 PM checklists as PDF
python scripts/generate_all_pms.py --format pdf

# Generate as DOCX
python scripts/generate_all_pms.py --format docx

# Generate as XLSX
python scripts/generate_all_pms.py --format xlsx
```
Output goes to: `output/pm-docs/<machine>/<year>/<month>/`

---

## RAG Pipeline — Upload a Machine Manual

The RAG pipeline processes a machine manual PDF into PM tasks following the Data Flow Diagram:

```
PDF Upload → Pre-Classify (Krones/Other) → Find Chapter (TOC scan or RAG search)
→ Chunk (500 words / 103 word overlap) → Embed (1024 dims or 3072 dims)
→ Vector Store (Azure AI Search) → RAG Retrieve Top 10 chunks
→ AI Extract (Granite / GPT-4o) → Validate JSON → Engineer Review → Add to Library
```

### Prerequisites for RAG
- Set `AI_PROVIDER` and the corresponding API key
- For vector search: set `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_API_KEY`
  - Without Azure AI Search, the pipeline still works — top 10 chunks are selected linearly

### Upload via dashboard
1. Log in as **Engineer** or **Manager**
2. Navigate to **Upload Manual**
3. Select a PDF (up to 50 MB)
4. Optionally select the target machine
5. Click **Upload & Process**
6. Watch the queue — status progresses: `UPLOADED → CLASSIFYING → CHUNKING → EMBEDDING → EXTRACTING → PENDING_REVIEW`
7. Click **Approve** to add extracted tasks to the PM Library

### Upload via API
```bash
TOKEN=your-token

# Upload PDF
curl -X POST http://localhost:8000/api/manual/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/krones_contiform_manual.pdf" \
  -F "machine_id=CONTIFORM-C3-L3"

# Check pipeline status (use manual_id from upload response)
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/manual/uploads/{manual_id}

# Approve extracted tasks
curl -X POST http://localhost:8000/api/manual/uploads/{manual_id}/approve \
  -H "Authorization: Bearer $TOKEN" \
  -F "machine_id=CONTIFORM-C3-L3"
```

---

## API Reference

### Authentication
All endpoints (except `/health`) require `Authorization: Bearer <token>` header.

In dev mode, you can also use `X-API-Key: dev-secret-key-change-in-prod`.

| Method | Endpoint | Min Role | Description |
|--------|----------|----------|-------------|
| POST | `/dev/token` | Dev only | Get dev JWT token |
| GET | `/health` | None | Health check |
| POST | `/api/generate` | Technician | Generate PM document (PDF/DOCX/XLSX) |
| GET | `/api/history` | All | List PM history |
| GET | `/api/history/dashboard` | All | Dashboard + AI analytics |
| POST | `/api/history/{id}/approve` | Supervisor+ | Approve a PM record |
| GET | `/api/library` | All | Full PM library summary |
| GET | `/api/library/{machine}/{hours}` | All | Tasks for specific interval |
| POST | `/api/library/tasks` | Engineer+ | Add tasks manually |
| POST | `/api/library/hours` | Manager | Update machine hours |
| GET | `/api/machines` | All | List registered machines |
| POST | `/api/machines` | Engineer+ | Register new machine |
| PATCH | `/api/machines/{id}` | Engineer+ | Update machine |
| POST | `/api/manual/upload` | Engineer+ | Upload PDF → RAG pipeline |
| GET | `/api/manual/uploads` | Engineer+ | Upload queue |
| GET | `/api/manual/uploads/{id}` | Engineer+ | Pipeline status |
| POST | `/api/manual/uploads/{id}/approve` | Engineer | Approve extracted tasks |
| GET | `/api/checklist/{record_id}` | All | Get checklist state |
| POST | `/api/checklist/{record_id}` | Technician | Submit filled checklist |
| GET | `/api/export/history/csv` | Manager | Export history CSV |
| GET | `/api/export/library/csv` | Manager | Export library CSV |
| GET | `/api/export/audit-logs/csv` | Manager | Export audit log CSV |
| GET | `/api/download/{machine}/{year}/{month}/{file}` | All | Download PM document |
| GET | `/docs` | Dev only | Swagger UI |

### Generate PM Example
```bash
curl -X POST http://localhost:8000/api/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "machine_id": "CONTIFORM-C3-L3",
    "interval_hours": 120,
    "work_order": "WO-2026-001",
    "technician_name": "Ahmed Khan",
    "output_format": "pdf",
    "storage_target": "local"
  }'
```

### Dashboard (with AI Analytics)
```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/history/dashboard
```
Returns: stats, overdue PMs, recent PMs, schedule, AI-predicted next due dates.

---

## Run Tests

```bash
# All tests
pytest tests/ -v --asyncio-mode=auto

# With coverage report
pytest tests/ -v --cov=app --cov-report=term-missing --asyncio-mode=auto

# Individual test files
pytest tests/test_generate.py -v --asyncio-mode=auto
pytest tests/test_library.py  -v --asyncio-mode=auto
pytest tests/test_auth.py     -v --asyncio-mode=auto
pytest tests/test_history.py  -v --asyncio-mode=auto

# Security scan (Bandit — same as CI/CD pipeline)
bandit -r app/ --severity-level medium
```

---

## Docker

```bash
# Build and run (easiest)
docker-compose up --build

# Build image manually
docker build -t pm-automation:latest .

# Run container (dev mode, local storage)
docker run -p 8000:8000 \
  -e APP_ENV=development \
  -e DEV_API_KEY=dev-secret-key-change-in-prod \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/output:/app/output \
  pm-automation:latest

# Run with watsonx:
docker run -p 8000:8000 \
  -e APP_ENV=development \
  -e AI_PROVIDER=watsonx \
  -e WATSONX_API_KEY=your-iam-api-key \
  -e WATSONX_PROJECT_ID=your-project-id \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/output:/app/output \
  pm-automation:latest
```

---

## Production Deployment — Azure App Service

### Step 1 — Create Azure Resources
```bash
az login
az group create --name pm-automation-rg --location uksouth

az appservice plan create \
  --name pm-automation-plan \
  --resource-group pm-automation-rg \
  --sku B2 --is-linux

az webapp create \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --plan pm-automation-plan \
  --runtime "PYTHON|3.12"
```

### Step 2 — Create Azure SQL Database
```bash
az sql server create \
  --name pm-sql-server \
  --resource-group pm-automation-rg \
  --admin-user pmadmin \
  --admin-password "$(openssl rand -base64 24)"

az sql db create \
  --server pm-sql-server \
  --resource-group pm-automation-rg \
  --name pm_automation \
  --edition Basic
```

### Step 3 — Create Azure AI Search (for RAG)
```bash
az search service create \
  --name pm-search \
  --resource-group pm-automation-rg \
  --sku basic

# Create the index (run once after service is deployed)
# The index schema requires: id, manual_id, text, page_start, page_end, source_file, content_vector
```

### Step 4 — Store Secrets in Key Vault
```bash
az keyvault create \
  --name pm-keyvault \
  --resource-group pm-automation-rg

# Store all secrets — never hardcode these
az keyvault secret set --vault-name pm-keyvault --name "pm-app-secret-key"                    --value "$(openssl rand -hex 32)"
az keyvault secret set --vault-name pm-keyvault --name "pm-database-url"                       --value "mssql+aioodbc://..."
az keyvault secret set --vault-name pm-keyvault --name "pm-openai-api-key"                     --value "sk-..."
az keyvault secret set --vault-name pm-keyvault --name "pm-watsonx-api-key"                    --value "your-ibm-iam-api-key"
az keyvault secret set --vault-name pm-keyvault --name "pm-azure-storage-connection-string"    --value "DefaultEndpointsProtocol=..."
az keyvault secret set --vault-name pm-keyvault --name "pm-azure-search-api-key"               --value "..."
az keyvault secret set --vault-name pm-keyvault --name "pm-azure-ad-client-secret"             --value "..."
```

### Step 5 — Enable Managed Identity & Key Vault Access
```bash
# Assign system-managed identity to the web app
az webapp identity assign \
  --name pm-automation-api \
  --resource-group pm-automation-rg

# Grant identity access to Key Vault
IDENTITY=$(az webapp identity show --name pm-automation-api --resource-group pm-automation-rg --query principalId -o tsv)
az keyvault set-policy \
  --name pm-keyvault \
  --object-id $IDENTITY \
  --secret-permissions get list
```

### Step 6 — Set App Settings (non-secret config only)
```bash
az webapp config appsettings set \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --settings \
    APP_ENV=production \
    AZURE_KEY_VAULT_URL=https://pm-keyvault.vault.azure.net/ \
    AI_PROVIDER=watsonx \
    WATSONX_PROJECT_ID=your-project-id \
    WATSONX_URL=https://us-south.ml.cloud.ibm.com \
    AZURE_AD_TENANT_ID=your-tenant-id \
    AZURE_AD_CLIENT_ID=your-client-id \
    AZURE_STORAGE_ACCOUNT_NAME=pmstorageaccount \
    AZURE_STORAGE_CONTAINER_NAME=pm-docs \
    AZURE_SEARCH_ENDPOINT=https://pm-search.search.windows.net \
    AZURE_SEARCH_INDEX_NAME=pm-manuals \
    DEFAULT_STORAGE_TARGET=azure \
    ALLOWED_ORIGINS=https://pm-automation-api.azurewebsites.net \
    RATE_LIMIT_PER_MINUTE=100
```

### Step 7 — Deploy
```bash
az webapp up \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --runtime PYTHON:3.12
```

### Step 8 — Verify
```bash
curl https://pm-automation-api.azurewebsites.net/health
```

---

## Production Checklist

Before going live, verify:

- [ ] `APP_ENV=production` set (disables `/docs`, `/dev/token`, enables security headers)
- [ ] `APP_SECRET_KEY` is a strong 32+ char random secret (not the default)
- [ ] `AZURE_KEY_VAULT_URL` set — all sensitive secrets stored there, NOT in env vars
- [ ] `AZURE_AD_TENANT_ID` + `AZURE_AD_CLIENT_ID` set — real Azure AD SSO enforced
- [ ] `ALLOWED_ORIGINS` set to your actual domain(s)
- [ ] `DEFAULT_STORAGE_TARGET=azure` — PM documents stored in Azure Blob, not local
- [ ] `DATABASE_URL` points to Azure SQL (not SQLite)
- [ ] `APPLICATIONINSIGHTS_CONNECTION_STRING` set — monitoring enabled
- [ ] CI/CD pipeline (`.github/workflows/ci-cd.yml`) passing — Bandit → Docker → Azure
- [ ] TLS 1.3 enforced at APIM gateway level
- [ ] Azure AD Conditional Access policies configured for MFA enforcement
- [ ] Managed Identity assigned to App Service (no passwords in code or config)

---

## Project Structure

```
Niagra_Project/
  app/
    api/routes/         FastAPI route handlers
      generate.py       POST /api/generate — build PM document
      history.py        GET /api/history + dashboard with AI analytics
      library.py        GET /api/library — PM task library
      machines.py       Machine CRUD
      manual.py         POST /api/manual/upload — RAG pipeline trigger
      checklist.py      Fill & submit PM checklist
      export.py         CSV exports (Manager only)
      download.py       File download serving
    auth/
      azure_ad.py       Azure AD JWT validation + dev token
      rbac.py           Role-based permissions (Supervisor/Technician/Manager/Engineer)
    core/
      document_generator.py  PDF (ReportLab) / DOCX / XLSX generation
      pm_library.py     Seed + query PM task library
      storage_module.py Upload to Azure Blob / SFTP / local
      history_module.py Build dashboard data
      analytics.py      IBM watsonx.ai analytics (or rule-based fallback)
      key_vault.py      Azure Key Vault secret loading at startup
      app_insights.py   Request logging middleware + App Insights init
    rag/
      pipeline.py       Orchestrate full RAG pipeline (Stage A + B)
      classifier.py     Keyword + AI manufacturer detection
      chunker.py        500w/103w word-based chunking
      embedder.py       watsonx slate-125m or OpenAI text-embedding-3-large
      retriever.py      Azure AI Search vector index + query
      extractor.py      Granite-3b / GPT-4o JSON task extraction
      watsonx_auth.py   IBM IAM token helper (API key → Bearer token)
    db/
      models.py         SQLAlchemy models (Machine, Task, PMRecord, AuditLog, ...)
      crud.py           All database operations
      database.py       Async SQLAlchemy engine setup
    schemas/            Pydantic request/response models
    utils/
      audit.py          Immutable GMP audit log writer
      error_handler.py  Global exception handlers
      security.py       File hash, path traversal protection, filename sanitization
    config.py           Pydantic Settings (all env vars)
    dependencies.py     FastAPI dependency injection (auth + DB)
    main.py             FastAPI app — middleware, routes, lifespan
  data/
    pm_library.json     5 machines, 228 tasks, 48 intervals (source of truth)
    pm_automation.db    SQLite dev database (auto-created)
  frontend/
    index.html          Login page (dev form + Azure SSO button)
    dashboard.html      Full SPA dashboard (all 4 roles)
    static/css/         Styles
    static/js/app.js    Dashboard logic (API calls, role-based UI)
  scripts/
    seed_database.py    Load pm_library.json into database
    generate_all_pms.py Generate all 16 PM PDFs in one shot
  tests/                pytest test suite
  .github/workflows/    CI/CD: Bandit security scan → Docker build → Azure deploy
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example          Template — copy to .env and fill in values
  RUN_GUIDE.md          This file
```

---

## Troubleshooting

### Server won't start
```
ERROR: Cannot import 'aiosqlite'
```
Run: `pip install aiosqlite`

### IBM watsonx returns 401
- The raw API key must NOT be passed as `Authorization: Bearer <api_key>` directly
- The system exchanges it for an IAM token via `https://iam.cloud.ibm.com/identity/token`
- Check that `WATSONX_API_KEY` is your IBM Cloud IAM API key (from cloud.ibm.com → IAM → API Keys), NOT a service credential

### IBM watsonx returns 400 / "model not found"
- `WATSONX_URL` must match the region where your project is (e.g. `https://us-south.ml.cloud.ibm.com`)
- `WATSONX_PROJECT_ID` must be the correct project UUID from your watsonx.ai project settings

### Generate PM returns "No tasks found"
- The PM Library has been seeded (check `/api/library`)
- The machine_id and interval_hours must match exactly what's in the library
- Example valid combos: `CONTIFORM-C3-L3` / `120`, `BOTTLECODER-L3` / `240`

### Download URL returns 404
- Storage target was `local` and the file was generated correctly
- URL pattern: `/api/download/{machine_id}/{year}/{month}/{filename}`
- Files stored at: `output/pm-docs/{machine_id}/{year}/{month}/{filename}`

### RAG pipeline stuck at EMBEDDING
- Azure AI Search endpoint not set — pipeline falls back to linear top-10 selection (still works)
- Check `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_API_KEY` in `.env`

### Tests fail with "no event loop"
```bash
pytest tests/ -v --asyncio-mode=auto
```
The `--asyncio-mode=auto` flag is required.
