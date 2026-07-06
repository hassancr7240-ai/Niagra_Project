# PM Automation System — Niagara Line 3

Automated Preventive Maintenance checklist generation from machine manuals using RAG (Retrieval-Augmented Generation). Upload a machine manual PDF, the system extracts all PM tasks, groups them by service interval, and generates separate Excel files per interval — matching the Niagara Asset Activity template exactly.

---

## What It Does

- **Upload any machine manual PDF** → pipeline extracts PM tasks automatically
- **Generates separate Excel files per PM interval** (e.g. 500hr, 3000hr, 6000hr)
- **Chat with the manual** — ask questions, request checklists in natural language
- **RAG pipeline** — semantic search over PDF chunks using vector embeddings
- **Supports any manufacturer** — Krones, Eisbar, Tetra Pak, or any new machine

---

## Machines Supported

| Machine | Manufacturer | PM Intervals |
|---|---|---|
| Shrink Tunnel | Krones | 100hr, 500hr, 1000hr, 4000hr, 30000hr |
| Variopac Pro | Krones | 120hr, 500hr, 1500hr, 3000hr |
| Dehumidifier | Eisbar DAS-E8K.2 | 500hr, 42000hr, 45000hr |
| Aseptic Tank | Tetra Pak | 3000hr, 6000hr, 12000hr, 18000hr |
| Residual Hazards | Krones | 120hr safety compliance |

---

## Technology Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI (Python 3.12) |
| Database | SQLite (dev) / Azure SQL (production) |
| Vector Store | SQLite cosine search / Azure AI Search |
| AI — Chat & Extraction | IBM Granite 3.3 via Ollama (dev) / Azure OpenAI GPT-4o (production) |
| AI — Embeddings | granite-embedding:latest via Ollama (dev) / text-embedding-3-large (production) |
| File Storage | Local (dev) / Azure Blob Storage (production) |
| Frontend | Vanilla JS + HTML dashboard |
| Containerisation | Docker |
| Deployment | Azure App Service |

---

## RAG Pipeline

```
PDF Upload
    → Text extraction (pdfplumber)
    → Manufacturer classification (AI)
    → Chunking: 500 words / 103-word overlap
    → Embedding: granite-embedding (384-dim vectors)
    → Stored in vector store (SQLite or Azure AI Search)
    → Semantic retrieval: top-10 chunks by cosine similarity
    → AI task extraction: granite3.3:2b / GPT-4o
    → Table fallback: 3-strategy parser for any table format
    → 98 tasks saved → grouped by interval → Excel files generated
```

---

## Quick Start (Local Development)

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) with granite models

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
# .env is already set for local Ollama — no changes needed for dev
```

### 4. Start the server

```bash
python -m uvicorn app.main:app --reload --port 8000
```

### 5. Open the dashboard

```
http://localhost:8000/frontend/dashboard.html
```

Default dev login — use the `/dev/token` endpoint with any email.

---

## How to Use

### Upload a manual and generate checklists

1. Go to **Upload Manual** tab → select a machine PDF
2. Wait 1–3 minutes for the pipeline (status: CLASSIFYING → CHUNKING → EMBEDDING → EXTRACTING → Ready)
3. Open **Chat** → click the 📎 paperclip → select the uploaded manual
4. Type: `generate checklist`
5. Download links appear for each PM interval — one Excel file per frequency

### Chat with the manual

While a manual is attached in chat, ask anything:
- *"What are the daily checks?"*
- *"What oil does this machine use?"*
- *"List all safety procedures"*
- *"What does the manual say about belt tension?"*

### Generate from PM Library

Go to **Generate PM** tab → select machine + interval → choose format (PDF/Excel/Word) → download.

---

## Project Structure

```
pm_project/
├── app/
│   ├── api/routes/          # FastAPI endpoints
│   │   ├── chat.py          # Chat + checklist generation
│   │   ├── manual.py        # PDF upload + RAG pipeline
│   │   ├── generate.py      # PM Library document generation
│   │   └── ...
│   ├── core/
│   │   ├── document_generator.py   # Excel/PDF/Word output
│   │   └── pm_generation.py        # Per-interval XLSX generation
│   ├── rag/
│   │   ├── pipeline.py      # Full RAG pipeline + table fallback
│   │   ├── extractor.py     # AI task extraction prompts
│   │   ├── embedder.py      # Vector embedding
│   │   ├── retriever.py     # Cosine similarity search
│   │   └── chunker.py       # PDF text chunking
│   ├── db/                  # SQLAlchemy models + CRUD
│   └── config.py            # All settings (env-driven)
├── frontend/                # Dashboard HTML/JS/CSS
├── data/
│   └── pm_library.json      # Seed data for PM Library
├── Dockerfile               # Production container
├── docker-compose.yml       # Local Docker dev
├── deploy.ps1               # One-click Azure deployment (Windows)
├── .env.production          # Production environment template
└── requirements.txt
```

---

## Deployment to Azure

All Azure infrastructure is pre-wired. To deploy:

1. Install [Azure CLI](https://aka.ms/installazurecliwindows)
2. Open `deploy.ps1` and fill in your API key and app name
3. Run in PowerShell:

```powershell
cd pm_project
.\deploy.ps1
```

The script creates: Resource Group → Container Registry → App Service Plan → Web App → sets all environment variables automatically.

See `.env.production` for all configuration options including Azure OpenAI, Azure Blob Storage, and Azure SQL.

---

## Key API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/manual/upload` | Upload a PDF manual |
| GET | `/api/manual/uploads/{id}/status` | Check pipeline progress |
| POST | `/api/manual/uploads/{id}/generate-xlsx` | Generate per-interval Excel files |
| POST | `/api/chat/sessions/{id}/message` | Chat with the AI assistant |
| POST | `/api/generate` | Generate PM document from library |
| GET | `/api/download/{machine}/{year}/{month}/{file}` | Download generated file |
| GET | `/health` | Health check |
| GET | `/docs` | Swagger UI (dev mode only) |

---

## Environment Variables

See `.env.production` for full documentation. Key variables:

| Variable | Description |
|---|---|
| `AI_PROVIDER` | `openai` or `watsonx` |
| `OPENAI_API_KEY` | OpenAI or Azure OpenAI key |
| `OPENAI_BASE_URL` | Leave blank for OpenAI; set for Azure OpenAI or Ollama |
| `OPENAI_MODEL_GENERATION` | Model for task extraction + chat (e.g. `gpt-4o`) |
| `OPENAI_EMBEDDING_MODEL` | Embedding model (e.g. `text-embedding-3-large`) |
| `DEFAULT_STORAGE_TARGET` | `local`, `azure`, or `ftp` |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure Blob Storage connection |
| `DATABASE_URL` | Leave blank for SQLite; set for Azure SQL |

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Production-ready, stable |
| `develop` | Integration branch |
| `feature/initial-development` | Active development |
