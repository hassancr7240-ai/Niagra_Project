# PM Automation System — Complete Run Guide

## AI Models — Which are used?

The system supports TWO AI providers (switchable via `AI_PROVIDER` env var):

### IBM watsonx.ai (Data Flow Diagram — Primary)
| Role | Model ID | Use In System |
|------|----------|---------------|
| Classification | `ibm/granite-13b-instruct-v2` | Detect manufacturer (Krones/Other) from PDF |
| Embedding | `ibm/slate-125m-english-rtrvr` (1024 dims) | Embed PDF chunks → Azure AI Search |
| Generation | `ibm/granite-3b-code-instruct` | Extract structured JSON tasks from manual chunks |
| Analytics | `ibm/granite-13b-instruct-v2` | Predict next PM due dates, analyse overdue patterns |

### OpenAI (Architecture Diagram — Alternative)
| Role | Model ID | Use In System |
|------|----------|---------------|
| Classification | `gpt-4o-mini` | Detect manufacturer |
| Embedding | `text-embedding-3-large` (3072 dims) | Embed PDF chunks |
| Generation | `gpt-4o` (128k context) | Extract structured JSON tasks |

### How to switch:
```env
# Use IBM watsonx.ai (matches Data Flow Diagram exactly):
AI_PROVIDER=watsonx
WATSONX_API_KEY=your-watsonx-api-key
WATSONX_PROJECT_ID=your-project-id

# Use OpenAI (matches Architecture Diagram):
AI_PROVIDER=openai
OPENAI_API_KEY=your-openai-key
```

---

## Quick Start (Local Development)

### Prerequisites
- Python 3.11+ installed
- pip packages installed (see step 1)

### Step 1 — Install Dependencies
```bash
cd pm_project
pip install -r requirements.txt
pip install aiosqlite
```

### Step 2 — Configure Environment
```bash
# Copy the example env file
copy .env.example .env

# Edit .env — minimum required for local dev (everything else is optional):
# APP_ENV=development          (already set)
# DEV_API_KEY=dev-secret-key-change-in-prod  (already set)
# DEFAULT_STORAGE_TARGET=local (already set)
# AI_PROVIDER=openai           (set to watsonx if you have IBM credentials)
# OPENAI_API_KEY=sk-...        (only needed for RAG pipeline / manual upload)
```

### Step 3 — Run the Server
```bash
# Option A: Direct uvicorn (recommended for development)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Option B: Via main.py
python app/main.py
```

### Step 4 — Open the Dashboard
```
http://localhost:8000/frontend/index.html
```
Login with any email. Choose role: **Manager** (full access).

### Step 5 — Get a Dev API Token (for API testing)
```bash
curl -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@factory.com","name":"Admin","role":"Manager"}'
```

---

## Seed Database & Generate All 16 PM PDFs

```bash
# Seed the database from pm_library.json (also runs automatically on startup)
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

## Test the API (with curl)

```bash
# Set your token
TOKEN=$(curl -s -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"email":"test@factory.com","name":"Test","role":"Manager"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Health check (no auth needed)
curl http://localhost:8000/health

# List all 5 machines
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/machines

# Get full PM library (5 machines, 146 tasks)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/library

# Get 28-task Bottle Coder 240hr checklist
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/library/BOTTLECODER-L3/240

# Get 19-task Contiform 120hr checklist (3 machine states)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/library/CONTIFORM-C3-L3/120

# Generate a PM document (PDF)
curl -X POST http://localhost:8000/api/generate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "machine_id": "BOTTLECODER-L3",
    "interval_hours": 240,
    "work_order": "WO-2026-001",
    "technician_name": "Ahmed Khan",
    "output_format": "pdf",
    "storage_target": "local"
  }'

# Dashboard with watsonx analytics
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/history/dashboard

# View PM history
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/history

# Export history as CSV (Manager only)
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/export/history/csv \
  --output pm_history.csv

# Export full task library as CSV
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/export/library/csv \
  --output pm_library.csv

# Upload a machine manual PDF (triggers RAG pipeline)
curl -X POST http://localhost:8000/api/manual/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/manual.pdf" \
  -F "machine_id=CONTIFORM-C3-L3"

# Check RAG pipeline status
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/manual/uploads/{manual_id}

# Fill checklist (Technician)
curl -X POST http://localhost:8000/api/checklist/{record_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "completed_tasks": [
      {"task_id": "...", "task_no": 10, "initialed_by": "AK", "is_done": true}
    ]
  }'
```

---

## Run Tests

```bash
# Run all tests
pytest tests/ -v --asyncio-mode=auto

# Run with coverage
pytest tests/ -v --cov=app --cov-report=term-missing --asyncio-mode=auto

# Run specific test file
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
# Build and run with Docker Compose (easiest)
docker-compose up --build

# Build image manually
docker build -t pm-automation:latest .

# Run container
docker run -p 8000:8000 \
  -e APP_ENV=development \
  -e DEV_API_KEY=dev-secret-key-change-in-prod \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/output:/app/output \
  pm-automation:latest
```

---

## Production Deployment (Azure App Service)

### Step 1 — Set up Azure resources
```bash
# Login to Azure
az login

# Create resource group
az group create --name pm-automation-rg --location uksouth

# Create App Service plan
az appservice plan create \
  --name pm-automation-plan \
  --resource-group pm-automation-rg \
  --sku B2 --is-linux

# Create Web App
az webapp create \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --plan pm-automation-plan \
  --runtime "PYTHON|3.12"
```

### Step 2 — Set Key Vault secrets (never hardcode)
```bash
# Create Key Vault
az keyvault create \
  --name pm-keyvault \
  --resource-group pm-automation-rg

# Store secrets
az keyvault secret set --vault-name pm-keyvault --name "pm-openai-api-key" --value "sk-..."
az keyvault secret set --vault-name pm-keyvault --name "pm-azure-storage-connection-string" --value "DefaultEndpoints..."
az keyvault secret set --vault-name pm-keyvault --name "pm-database-url" --value "mssql+aioodbc://..."
az keyvault secret set --vault-name pm-keyvault --name "pm-watsonx-api-key" --value "..."
```

### Step 3 — Deploy
```bash
# Deploy directly
az webapp up \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --runtime PYTHON:3.12

# Set environment variables
az webapp config appsettings set \
  --name pm-automation-api \
  --resource-group pm-automation-rg \
  --settings \
    APP_ENV=production \
    AZURE_KEY_VAULT_URL=https://pm-keyvault.vault.azure.net/ \
    AI_PROVIDER=watsonx \
    WATSONX_PROJECT_ID=your-project-id \
    WATSONX_URL=https://us-south.ml.cloud.ibm.com

# Enable Managed Identity (for Key Vault — no passwords in code)
az webapp identity assign \
  --name pm-automation-api \
  --resource-group pm-automation-rg
```

### Step 4 — Verify
```bash
curl https://pm-automation-api.azurewebsites.net/health
```

---

## API Endpoints Summary

| Method | Endpoint | Role Required | Description |
|--------|----------|---------------|-------------|
| POST | `/dev/token` | Dev only | Get dev auth token |
| GET | `/health` | None | Health check |
| POST | `/api/generate` | Technician+ | Generate PM document |
| GET | `/api/history` | All | List PM history |
| GET | `/api/history/dashboard` | All | Dashboard + AI analytics |
| POST | `/api/history/{id}/approve` | Supervisor+ | Approve a PM record |
| GET | `/api/library` | All | PM library summary |
| GET | `/api/library/{machine}/{hours}` | All | Get tasks for interval |
| POST | `/api/library/tasks` | Engineer/Manager | Add tasks to library |
| POST | `/api/library/hours` | Manager+ | Update machine hours |
| GET | `/api/machines` | All | List machines |
| POST | `/api/machines` | Engineer/Manager | Add machine |
| PATCH | `/api/machines/{id}` | Engineer/Manager | Update machine |
| POST | `/api/manual/upload` | Engineer/Manager | Upload PDF → RAG pipeline |
| GET | `/api/manual/uploads` | Engineer/Manager | Upload queue |
| GET | `/api/manual/uploads/{id}` | Engineer/Manager | Pipeline status |
| POST | `/api/manual/uploads/{id}/approve` | Engineer | Approve extracted tasks |
| GET | `/api/checklist/{record_id}` | All | Get checklist status |
| POST | `/api/checklist/{record_id}` | Technician+ | Submit filled checklist |
| GET | `/api/export/history/csv` | Manager | Export history CSV |
| GET | `/api/export/library/csv` | Manager | Export library CSV |
| GET | `/api/export/audit-logs/csv` | Manager | Export audit log CSV |
| GET | `/api/download/{machine}/{year}/{month}/{file}` | All | Download PM document |
| GET | `/docs` | Dev only | Swagger UI |

---

## Project Structure

```
pm_project/
  app/
    api/routes/     — FastAPI routes (generate, history, library, machines, manual, checklist, export)
    auth/           — Azure AD JWT + RBAC
    core/           — PDF generator, storage, history, analytics, Key Vault, App Insights
    db/             — SQLAlchemy models + CRUD
    rag/            — RAG pipeline (classifier, chunker, embedder, retriever, extractor)
    schemas/        — Pydantic validation
    utils/          — Error handler, audit logger, security
    main.py         — FastAPI app entry point
  data/
    pm_library.json — 5 machines, 146 tasks, 16 intervals
  frontend/
    index.html      — Login page
    dashboard.html  — Full dashboard (all 3 role views)
    static/         — CSS + JS
  scripts/
    seed_database.py    — Seed DB from JSON
    generate_all_pms.py — Generate all 16 PM PDFs
  tests/            — pytest test suite
  .github/workflows/ci-cd.yml — Bandit → Docker → Azure
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
```
