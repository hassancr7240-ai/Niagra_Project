# PM Automation System

Automated Preventive Maintenance checklist generation from machine manuals using RAG (Retrieval-Augmented Generation). Upload a machine manual PDF → AI extracts all PM tasks grouped by service interval → Engineer reviews, approves, and downloads CON L3 format Excel checklists.

---

## What It Does

- **Upload any machine manual PDF** — pipeline classifies, chunks, and extracts PM tasks automatically
- **Interval-based review** — tasks grouped by PM interval (e.g. 500hr, 1000hr, 3000hr) with clickable interval tabs
- **Engineer approval flow** — review tasks with page citations, approve or reject with mandatory comment
- **CON L3 Excel output** — one XLSX per PM interval, ZIP download, matching Niagara Asset Activity template
- **Audit trail** — every approval and rejection saved with reviewer email and timestamp
- **Supports any manufacturer** — Krones, Eisbar, Tetra Pak, SIG, Sidel, Bosch, or any new machine

---

## Approval Workflow

```
Upload PDF
  → Pipeline: Classify → Chunk → 8-Pass Retrieve → AI Extract → Validate → Citations
  → Dashboard shows interval buttons: [500hr (8 tasks)] [1000hr (5 tasks)] [3000hr (4 tasks)]
  → Click interval → Review Page opens
      ├─ Task table: Area · Action · Description · Page # · Section · State · Safety · Verified
      ├─ Citations drawer: page references from PDF with JSON viewer
      ├─ ✓ Approve → tasks added to PM Library → Download ZIP / Excel
      └─ ✕ Reject → mandatory comment required → saved to audit log
```

---

## Machines Supported

| Manufacturer | Machine Examples | PM Intervals |
|---|---|---|
| Krones | Contiform, Variopac Pro, Shrink Tunnel | 100hr, 120hr, 500hr, 1000hr, 1500hr, 3000hr, 4000hr, 30000hr |
| Tetra Pak | Aseptic Tank, TEM | 3000hr, 6000hr, 12000hr, 18000hr |
| Eisbar | Dehumidifier DAS-E8K.2 | 500hr, 42000hr, 45000hr |
| SIG | Combibloc, Combiflex | 500hr, 1000hr, 2000hr, 5000hr, 10000hr |
| Sidel / Bosch / Sacmi | Various | 500hr, 1000hr, 2000hr, 4000hr, 8000hr |
| Any other | Auto-detected | 8hr, 120hr, 500hr, 1000hr, 3000hr, 6000hr (generic) |

---

## Technology Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI (Python 3.12) |
| Database | SQLite (dev) / Azure SQL (production) |
| Vector Store | SQLite in-memory / Azure AI Search (production) |
| AI — Extraction | IBM Granite 3.3 2B via Ollama (dev) / IBM watsonx.ai (production) |
| AI — Embeddings | granite-embedding:latest via Ollama (dev) / watsonx embeddings (production) |
| File Storage | Local filesystem (dev) / Azure Blob Storage (production) |
| Frontend | Vanilla JS + HTML (no framework) |
| Containerisation | Docker |
| Deployment | Azure App Service |

---

## RAG Pipeline (Detail)

```
PDF Upload
  → Safety scan (blocks JavaScript-embedded PDFs)
  → Archive to Azure Blob Storage (or local)
  → Text extraction: pdfplumber (structure-aware, not OCR)
  → Manufacturer classification: AI + keyword fallback (timeout 30s)
  → Structure-aware chunking: table_row / checkbox / section / paragraph
  → 8-Pass content_type retrieval (local dev — no embedding needed):
      Pass 1: toc_schedule     Pass 2: warning/safety
      Pass 3: loto/lockout     Pass 4: interval tables
      Pass 5: ppe/tools        Pass 6: startup/shutdown
      Pass 7: parts_list       Pass 8: general procedure coverage
  → AI task extraction: granite3.3:2b — structured JSON array
      • interval_hours=0 defaults to 500hr (not dropped)
      • missing description synthesised from area + action
  → Table-based fallback if AI returns 0 tasks
  → Citations saved: page_start, page_end, section, content_type, excerpt
  → Internal validation: tasks with citations → VERIFIED, others → UNVERIFIED
  → Status → PENDING_REVIEW
```

---

## Quick Start (Local Development)

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) installed and running

### 1. Pull AI models

```bash
ollama pull granite3.3:2b
ollama pull granite-embedding:latest
ollama serve
```

### 2. Install dependencies

```bash
cd pm_project
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Default .env is already set for local Ollama — no changes needed
```

Key `.env` settings for local dev:
```
AI_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
OPENAI_MODEL_GENERATION=granite3.3:2b
OPENAI_EMBEDDING_MODEL=granite-embedding:latest
DATABASE_URL=            # blank = SQLite
DEFAULT_STORAGE_TARGET=local
```

### 4. Start the server

```bash
python -m uvicorn app.main:app --reload --port 8000
```

### 5. Open the dashboard

```
http://localhost:8000/frontend/dashboard.html
```

Login via `/dev/token` with any email (dev mode only).

---

## How to Use

### Upload a manual and generate checklists

1. **Dashboard → Upload Manual** — select a machine PDF and click Upload
2. **Wait for pipeline** (1–5 min depending on PDF size):
   `UPLOADED → CLASSIFYING → CHUNKING → EMBEDDING → EXTRACTING → PENDING REVIEW`
3. **Step 3 shows interval buttons** — e.g. `[500hr / Monthly (8 tasks)]` `[1000hr (5 tasks)]`
4. **Click an interval** → opens the Review page for that interval
5. **Review page**:
   - Task table: Area, Action, Description, Page citation, State, Safety flags
   - Citations drawer: source pages from the PDF with text excerpts and JSON
   - Click **✓ Approve** → tasks added to PM Library, download ZIP / Excel
   - Click **✕ Reject** → enter mandatory rejection comment → saved to audit log
6. **Download** ZIP (all intervals) or individual Excel files

### Review page features

| Feature | Description |
|---|---|
| Interval tabs | Switch between 500hr, 1000hr, 3000hr etc. — each shows its own task table |
| Page badge | `📄 14–47` — click to open the citations drawer for that source page |
| VERIFY column | ✅ VERIFIED = AI found a page citation · ⚠ UNV = no page citation found |
| Safety column | 🔒 = task involves LOTO / power isolation |
| Citations drawer | Page refs, section names, content type (LOTO/PPE/Warning/Procedure/Parts), raw JSON |
| Approve | Machine pre-filled from upload — no re-selection needed at approval time |
| Reject | Mandatory comment — saved to GMP audit log with reviewer email |

---

## Project Structure

```
pm_project/
├── app/
│   ├── api/routes/
│   │   ├── manual.py          # Upload, pipeline trigger, approve, reject, citations, ZIP
│   │   ├── generate.py        # PM Library document generation
│   │   ├── machines.py        # Machine CRUD
│   │   └── library.py         # PM Library queries
│   ├── core/
│   │   ├── document_generator.py   # CON L3 Excel / ZIP generation
│   │   └── pm_generation.py        # Per-interval XLSX
│   ├── rag/
│   │   ├── pipeline.py        # 8-pass retrieval, table fallback, citation save, validation
│   │   ├── extractor.py       # AI task extraction (granite3.3:2b / watsonx)
│   │   ├── embedder.py        # Vector embedding
│   │   ├── retriever.py       # Hybrid BM25 + semantic search
│   │   ├── chunker.py         # Structure-aware PDF chunking
│   │   ├── classifier.py      # Manufacturer classification
│   │   └── watsonx_auth.py    # IBM IAM token refresh
│   ├── db/                    # SQLAlchemy models + CRUD
│   └── config.py              # All settings (env-driven)
├── frontend/
│   ├── dashboard.html         # Main dashboard (upload, history, library, machines)
│   ├── review.html            # Review & approval page (interval tabs, citations, approve/reject)
│   └── static/js/app.js       # Dashboard JS
├── data/
│   └── pm_library.json        # Seed data for PM Library
├── Dockerfile
├── docker-compose.yml
├── deploy.ps1                 # One-click Azure deployment (Windows)
├── .env.example               # Local dev config template
├── .env.production            # Production config template
└── requirements.txt
```

---

## Key API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/manual/upload` | Upload PDF — triggers RAG pipeline |
| GET | `/api/manual/uploads` | List all uploads |
| GET | `/api/manual/uploads/{id}` | Full detail: status, tasks, manufacturer |
| GET | `/api/manual/uploads/{id}/status` | Lightweight status poll (progress %) |
| GET | `/api/manual/uploads/{id}/citations` | Page citations saved during extraction |
| POST | `/api/manual/uploads/{id}/approve` | Approve tasks → add to PM Library |
| POST | `/api/manual/uploads/{id}/reject` | Reject with mandatory comment |
| GET | `/api/manual/uploads/{id}/generate-zip` | Download ZIP of all CON L3 Excel files |
| POST | `/api/manual/uploads/{id}/generate-xlsx` | Generate per-interval XLSX files |
| GET | `/api/library` | PM Library (machines + intervals + tasks) |
| POST | `/api/generate` | Generate PM document from library |
| GET | `/health` | Health check |
| GET | `/docs` | Swagger UI (dev mode only) |

---

## Environment Variables

| Variable | Dev | Production |
|---|---|---|
| `AI_PROVIDER` | `openai` (Ollama) | `watsonx` |
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | — |
| `OPENAI_MODEL_GENERATION` | `granite3.3:2b` | — |
| `WATSONX_API_KEY` | — | IBM watsonx.ai API key |
| `WATSONX_PROJECT_ID` | — | IBM watsonx.ai Project ID |
| `WATSONX_URL` | — | `https://us-south.ml.cloud.ibm.com` |
| `DATABASE_URL` | *(blank = SQLite)* | Azure SQL connection string |
| `DEFAULT_STORAGE_TARGET` | `local` | `azure` |
| `AZURE_STORAGE_CONNECTION_STRING` | — | Azure Blob Storage connection |
| `DEV_API_KEY` | `dev-secret-key-...` | *(disable in prod)* |

---

## Deployment to Azure

All Azure infrastructure is pre-wired. To deploy:

1. Install [Azure CLI](https://aka.ms/installazurecliwindows)
2. Open `deploy.ps1` and fill in your subscription and app name
3. Run in PowerShell:

```powershell
cd pm_project
.\deploy.ps1
```

The script creates: Resource Group → Container Registry → App Service Plan → Web App → sets all environment variables automatically.

> **Note:** Azure Document Intelligence and Azure AI Search require `Microsoft.CognitiveServices` and `Microsoft.Search` resource providers to be enabled on the subscription (pending CEO approval for production).

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Production-ready, stable |
| `develop` | Integration branch |
| `feature/initial-development` | Active development |
