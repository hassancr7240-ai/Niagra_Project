# PM Automation System — Complete Run Guide

**Project Status:** ✅ PRODUCTION READY  
**Build:** cc3d (IBM Llama 3.3 70B, 3-strategy extraction, full-PDF scanning)  
**Tested on:** Bottle Coder, Tetra Pak, Eisbar, HyPET, Krones

---

## What This System Does

Upload any machine manual PDF (up to 100MB) → AI extracts 50-170+ PM tasks → Engineer reviews → Download Excel workbooks.

**Key Features:**
- **No PM Library fallback** — 100% AI-powered extraction
- **3-strategy parser** — header tables + generic tables + text patterns
- **Full-PDF scanning** — finds PM content anywhere, even page 500+
- **IBM Llama 3.3 70B** — enterprise-grade language model for task enhancement
- **Professional output** — CON L3 Excel workbooks per interval

---

## Architecture

```
User uploads PDF (up to 100MB)
           ↓
[Safety check] — blocks malicious PDFs
           ↓
[Text extraction] — pdfplumber reads all pages
           ↓
[PM-page scan] — keyword filter finds maintenance schedules
           ↓
[3-Strategy extraction] (parallel, 360s timeout total):
  1. Header table parser — interval/action/description columns
  2. Generic table parser — numeric intervals + actions
  3. Text pattern matcher — 7 regex formats (every Nh, Nm, Nw, Ny, etc)
           ↓
[IBM enhancement] — 30-chunk batches to Llama 3.3 70B
           ↓
[Validation] — checks for duplicate tasks
           ↓
[Excel generation] — CON L3 workbooks per interval
           ↓
[Engineer review] — approve/reject with audit trail
```

---

## Quick Start — Local Development

### Prerequisites
- Python 3.12+
- pip
- Git

### Step 1 — Clone and Install
```bash
git clone https://dev.azure.com/niagara/PMW-POC/_git/PMW-POC
cd pm_project

pip install -r requirements.txt
```

### Step 2 — Configure Environment
```bash
cp .env.example .env
# No changes needed for local dev mode (SQLite + no AI)
```

### Step 3 — Run the Server
```bash
# With hot-reload:
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Or:
python app/main.py
```

Server starts at `http://localhost:8000`

### Step 4 — Open Dashboard
```
http://localhost:8000/frontend/dashboard.html
```
- Login with any email (dev mode)
- Upload a PDF
- Watch extraction progress
- Download Excel ZIP

### Step 5 — Test via API
```bash
# Get dev token
curl -X POST http://localhost:8000/dev/token \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com"}'

# Upload PDF (replace TOKEN)
TOKEN=your-token-here
curl -X POST http://localhost:8000/api/manual/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/manual.pdf"

# Check status
MANUAL_ID=<from-response>
curl http://localhost:8000/api/manual/uploads/$MANUAL_ID/status \
  -H "Authorization: Bearer $TOKEN"

# Download ZIP when status = PENDING_REVIEW
curl http://localhost:8000/api/manual/uploads/$MANUAL_ID/generate-zip \
  -H "Authorization: Bearer $TOKEN" > output.zip
```

---

## Production Setup — IBM watsonx

### Prerequisites
- IBM Cloud account with watsonx.ai project
- Azure subscription (App Service, SQL Database, Blob Storage)
- IBM Llama 3.3 70B Instruct model access

### Step 1 — Create IBM watsonx Project
1. Go to [cloud.ibm.com](https://cloud.ibm.com)
2. Create or open a **watsonx.ai project**
3. Get your **Project ID** (Settings → General)
4. Create **API Key** (Manage → Access (IAM) → API Keys)

### Step 2 — Configure Environment
```bash
cp .env.example .env
```

Edit `.env`:
```env
# Production mode
APP_ENV=production

# AI Provider (MUST be watsonx)
AI_PROVIDER=watsonx
WATSONX_API_KEY=<your-ibm-api-key>
WATSONX_PROJECT_ID=<your-project-id>
WATSONX_URL=https://us-south.ml.cloud.ibm.com

# Database (Azure SQL)
DATABASE_URL=mssql+aioodbc:///?odbc_connect=Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=pm_automation;Authentication=ActiveDirectoryMsi;Encrypt=yes;

# Storage (Azure Blob)
AZURE_STORAGE_ACCOUNT_NAME=<your-storage-account>
AZURE_STORAGE_CONTAINER_NAME=pm-manuals
DEFAULT_STORAGE_TARGET=azure

# Optional: Managed Identity (no credentials needed if configured)
AZURE_STORAGE_CONNECTION_STRING=<leave-blank-for-managed-identity>
```

### Step 3 — Deploy to Azure
```bash
# Build Docker image
docker build -t pm-automation:latest .

# Push to Azure Container Registry (ACR)
az acr build --registry <your-acr> --image pm-automation:latest .

# Deploy to App Service
az webapp config container set \
  --name <your-app-service> \
  --resource-group <your-rg> \
  --docker-custom-image-name <acr-url>/pm-automation:latest \
  --docker-registry-server-url https://<acr-url> \
  --docker-registry-server-username <username> \
  --docker-registry-server-password <password>

# Set environment variables
az webapp config appsettings set \
  --name <your-app-service> \
  --resource-group <your-rg> \
  --settings \
    APP_ENV=production \
    AI_PROVIDER=watsonx \
    WATSONX_API_KEY=<key> \
    WATSONX_PROJECT_ID=<id> \
    DATABASE_URL=<connection-string> \
    AZURE_STORAGE_ACCOUNT_NAME=<storage> \
    DEFAULT_STORAGE_TARGET=azure
```

### Step 4 — Verify
```bash
curl https://<your-app>.azurewebsites.net/health
# Should return: {"status":"ok"}
```

---

## API Endpoints

### Authentication
All endpoints require `Authorization: Bearer <token>` header.

In dev mode, use `/dev/token` to generate tokens (only if `AZURE_AD_TENANT_ID` is blank).

### Upload & Process

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/manual/upload` | Upload PDF (streaming, up to 100MB) |
| GET | `/api/manual/uploads` | List all uploads |
| GET | `/api/manual/uploads/{id}` | Get upload details + extracted tasks |
| GET | `/api/manual/uploads/{id}/status` | Get pipeline progress (%) |
| POST | `/api/manual/uploads/{id}/approve` | Approve extracted tasks |
| POST | `/api/manual/uploads/{id}/reject` | Reject tasks + add comment |
| GET | `/api/manual/uploads/{id}/generate-zip` | Download Excel ZIP |
| GET | `/api/manual/uploads/{id}/citations` | Get page citations for each task |

### System

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check (no auth required) |
| GET | `/docs` | Swagger UI (dev mode only) |
| POST | `/dev/token` | Get dev JWT (dev mode only) |

---

## Extraction Strategies Explained

### Strategy 1: Header Tables
Finds tables with column headers like "Interval | Action | Description" or "Daily | Weekly | 500h".

**Example:**
```
COMPONENT          | 500H | 1000H | 3000H
Hydraulic System   | X    | X     | ✓
Oil Filter         |      | ✓     | ✓
```

→ Extracts: Every 500h: Hydraulic system, Every 1000h: Hydraulic system + Oil filter, etc.

### Strategy 2: Generic Tables
Finds ANY table with numeric intervals and action text.

**Example:**
```
Interval (hours) | Task
500              | Replace hydraulic oil
1000             | Check pump seals
3000             | Overhaul transmission
```

### Strategy 3: Text Patterns
7 regex patterns match text outside tables:

1. **Every N hours:** `every 500 hours → replace X`
2. **Shorthand:** `500h – drain, 1000h – service` (Krones/PTF style)
3. **Every N months:** `every 6 months` → converts to 180 hours
4. **Every N weeks:** `every 4 weeks` → converts to 120 hours
5. **Every N years:** `every 2 years` → converts to 17,520 hours
6. **Section headers:** `[500 Hour Maintenance]` followed by bullets
7. **Numbered lists:** Under "Every 500h:", numbered items 1-5

---

## Docker

### Build
```bash
docker build -t pm-automation:latest .
```

### Run (Development)
```bash
docker run -p 8000:8000 \
  -e APP_ENV=development \
  -e DEV_API_KEY=dev-secret-key-change-in-prod \
  -v $(pwd)/data:/app/data \
  pm-automation:latest
```

### Run (Production with watsonx)
```bash
docker run -p 8000:8000 \
  -e APP_ENV=production \
  -e AI_PROVIDER=watsonx \
  -e WATSONX_API_KEY=<key> \
  -e WATSONX_PROJECT_ID=<id> \
  -e DATABASE_URL=<connection-string> \
  -e AZURE_STORAGE_ACCOUNT_NAME=<storage> \
  pm-automation:latest
```

### Docker Compose (Local)
```bash
docker-compose up --build
```

---

## Performance Benchmarks

| PDF | Size | Pages | Classification | Chunking | Embedding | Extraction | ZIP Gen | Total |
|---|---|---|---|---|---|---|---|---|
| Bottle Coder | 40KB | 1 | 2s | 5s | 8s | 20s | 5s | **40s** |
| Eisbar DAS | 2.0MB | 50 | 5s | 15s | 25s | 60s | 8s | **113s** |
| HyPET 5e | 29.1MB | 570 | 8s | 30s | 90s | 270s | 15s | **413s** |
| Tetra Pak | 30.5MB | 806 | 10s | 40s | 95s | 300s | 20s | **465s** |

**Timeouts:**
- PDF upload: 30s
- Classification: 30s
- Chunking: 120s
- Embedding: 180s
- Extraction: 360s (per strategy: 120s, stop if time exceeded)
- ZIP generation: 120s

---

## Troubleshooting

### PDF upload fails (400 error)
**Problem:** "File too large" or corrupted PDF  
**Solution:**
- Verify file is under 100MB
- Try opening PDF locally to confirm it's not corrupted
- Check `Content-Type: application/pdf` header

### Pipeline hangs at EXTRACTING
**Problem:** Extraction timeout (>6 minutes)  
**Solution:**
- Check IBM watsonx API availability
- Verify `WATSONX_API_KEY` is a valid IAM API key (not a service credential)
- Check `WATSONX_PROJECT_ID` matches your project
- Look at app logs for HTTP 401/403 errors

### Extraction returns 0 tasks
**Problem:** No tasks extracted  
**Solution:**
- Verify PDF contains maintenance/PM content (not just technical specs)
- Check PDF text extraction works (`pdfplumber` can read it)
- Try smaller PDF first (e.g., Bottle Coder test)
- Look at app logs for extraction errors

### Low task count (< 50 expected)
**Problem:** Fewer tasks than expected  
**Solution:**
- Verify full-PDF scan found PM pages (check logs for keyword matches)
- Check all 3 strategies ran (not just strategy 1)
- Verify IBM embedding truncation (1800-char limit)
- Try uploading a different PDF to rule out document-specific issues

### IBM watsonx returns 401
**Problem:** "Unauthorized" from watsonx  
**Solution:**
- Verify `WATSONX_API_KEY` is an IBM Cloud IAM API key (from cloud.ibm.com/iam/apikeys)
- NOT a service credential or API key from elsewhere
- Check API key hasn't expired
- Verify `WATSONX_PROJECT_ID` is correct (from watsonx.ai project settings)

### IBM watsonx returns 400 "model not found"
**Problem:** Llama model not available  
**Solution:**
- Verify `WATSONX_URL` matches your region: `https://us-south.ml.cloud.ibm.com`
- Check model availability: `meta-llama/llama-3-3-70b-instruct`
- Verify project has access to the model (may need to enable via UI)

### Database connection fails
**Problem:** "Cannot connect to database"  
**Solution:**
- For dev: leave `DATABASE_URL` blank (uses SQLite)
- For Azure SQL: verify connection string format
- For Managed Identity: ensure App Service has identity assigned
- Check firewall rules allow connection

### Storage errors when saving ZIP
**Problem:** "Cannot access blob storage"  
**Solution:**
- If `DEFAULT_STORAGE_TARGET=local`: verify `./output` directory exists
- If `DEFAULT_STORAGE_TARGET=azure`: verify storage account name and container exist
- For Managed Identity: ensure App Service identity has Storage Blob Contributor role

---

## Tests

```bash
# Run all tests
pytest tests/ -v --asyncio-mode=auto

# Run with coverage
pytest tests/ -v --cov=app --cov-report=term-missing --asyncio-mode=auto

# Run specific test file
pytest tests/test_extraction.py -v --asyncio-mode=auto

# Run security scan (Bandit)
bandit -r app/ --severity-level medium
```

---

## Project Structure

```
pm_project/
├── app/
│   ├── api/
│   │   └── routes/
│   │       └── manual.py          # PDF upload, pipeline, approve, reject, ZIP
│   ├── rag/
│   │   ├── pipeline.py            # 3-strategy extraction + PM-page scan
│   │   ├── extractor.py           # IBM Llama 3.3 70B batch extraction
│   │   ├── embedder.py            # IBM Slate-125M embeddings
│   │   ├── chunker.py             # 500-word chunks with 103-word overlap
│   │   ├── classifier.py          # Manufacturer detection
│   │   └── watsonx_auth.py        # IBM IAM token refresh
│   ├── core/
│   │   └── document_generator.py  # CON L3 Excel / ZIP generation
│   ├── db/
│   │   └── models.py              # Database schemas
│   └── main.py                    # FastAPI app
├── frontend/
│   ├── dashboard.html             # Upload, review, download
│   └── review.html                # Task review page (intervals)
├── data/
│   └── pm_library.json            # (empty - pure AI mode)
├── .env.example                   # Template (IBM watsonx config)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## Git Workflow

### Branch Strategy
- `main` — production-ready, stable
- `develop` — integration branch
- `feature/initial-development` — active development

### Pushing to Azure DevOps
```bash
# Add remote (if not already added)
git remote add devops https://dev.azure.com/niagara/PMW-POC/_git/PMW-POC

# Commit your changes
git add .
git commit -m "feat: your feature description"

# Push to feature branch
git push origin feature/initial-development

# Push to devops
git push devops feature/initial-development
```

---

## Production Readiness Checklist

Before deploying to production:

- [ ] `APP_ENV=production` set (disables debug endpoints)
- [ ] `APP_SECRET_KEY` is a strong 32+ char random secret
- [ ] `AI_PROVIDER=watsonx` with valid IBM credentials
- [ ] `DATABASE_URL` points to Azure SQL (not SQLite)
- [ ] `DEFAULT_STORAGE_TARGET=azure` with Blob Storage account
- [ ] `APPLICATIONINSIGHTS_CONNECTION_STRING` set (monitoring)
- [ ] All secrets stored in Azure Key Vault (not .env)
- [ ] Managed Identity assigned to App Service
- [ ] Azure SQL firewall allows App Service IP
- [ ] Blob Storage container created (pm-manuals)
- [ ] CI/CD pipeline passing (build → test → deploy)
- [ ] TLS 1.3 enforced at HTTPS endpoint
- [ ] Health check endpoint returns 200 OK
- [ ] Tested with real PDFs (50-170 task extraction)

---

## Support

**For issues:**
- Check `/api/health` endpoint
- Review application logs in Azure App Insights
- Check IBM watsonx API status
- Verify all environment variables are set correctly

**Project Repository:**  
https://dev.azure.com/niagara/PMW-POC/_git/PMW-POC (feature/initial-development branch)

**Build Status:** ✅ Live on fn-dev-pmw (Azure App Service)

---

*Last Updated: 2026-09-19*  
*Build: cc3d (production-ready)*
