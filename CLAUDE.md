# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Quick Commands

### Local Development

```bash
# Install dependencies
pip install -r pm_project/requirements.txt

# Run dev server (auto-reload)
cd pm_project
python -m uvicorn app.main:app --reload --port 8000

# Run tests
pytest pm_project/tests/ -v --asyncio-mode=auto

# Security scan
bandit -r pm_project/app/ --severity-level medium

# Format code
black pm_project/app/

# Type check
mypy pm_project/app/ --ignore-missing-imports
```

### Docker & Deployment

```bash
# Build container
docker build -t pm-automation:latest pm_project/

# Run locally
docker run -p 8000:8000 \
  -e APP_ENV=development \
  -e AI_PROVIDER=watsonx \
  -e WATSONX_API_KEY=<key> \
  -e WATSONX_PROJECT_ID=<id> \
  pm-automation:latest

# Push to Azure Container Registry (ACR)
docker tag pm-automation:latest <acr-url>/pm-automation:latest
docker push <acr-url>/pm-automation:latest

# Restart App Service
az webapp restart --name fn-dev-pmw --resource-group <your-rg>
```

---

## High-Level Architecture

### System Flow

```
PDF Upload (100MB max)
    ↓
[Safety Check] — pdfplumber reads text, scans for JavaScript
    ↓
[Text Extraction] — extract first 50 pages (~2-3 min)
    ↓
[PM-Page Keyword Scan] — full PDF scan finds maintenance content
                         (keywords: "maintenance schedule", "every 500h", etc.)
    ↓
[Smart Chunking] — split text into 500-word semantic chunks with 103-word overlap
                   (pages 1-120 sent to IBM, large PDFs use full scan)
    ↓
[Three Parallel Extraction Strategies] (360s total timeout):
  1. Header Table Detection    — interval/action/description columns
     └─ Sub-strategy 1b: Checkmark intervals (Daily/Weekly/500h as headers)
  2. Generic Table Parser      — any table with numeric intervals + actions
  3. Text Pattern Matching     — 7 regex formats (every Nh, Nm, Nw, Ny, sections, bullets)
    ↓
[Embedding] — IBM Slate-125M (max 512 tokens ≈ 1800 chars per chunk)
              Batch 25 chunks, 5 parallel requests
    ↓
[IBM Llama 3.3 70B Extraction] — 30-chunk batches (45K chars per batch)
                                  Validates, deduplicates, fills missing descriptions
    ↓
[Excel Generation] — CON L3 workbooks, per-interval sheets (Daily/500h/1000h/etc.)
    ↓
[Engineer Review] — dashboard shows tasks by interval, approve/reject with comments
    ↓
[Audit Trail] — all approvals/rejections logged with email + timestamp
```

### Key Technology Decisions

| Component | Choice | Why |
|-----------|--------|-----|
| **PDF Parsing** | pdfplumber (no OCR) | Preserves table structure, fast, no external dependencies |
| **AI Generation** | IBM Llama 3.3 70B (not GPT/Granite) | 128K context allows 30 chunks per batch, proven extraction quality |
| **AI Embeddings** | IBM Slate-125M (not OpenAI) | Optimized for retrieval scoring, 512-token limit fits Azure SQL storage |
| **Extraction** | 3-strategy local parser + AI | Local strategies extract 50-170 tasks in 1-2 min; AI enhances, deduplicates |
| **PM-Page Scan** | Full PDF keyword filter (not chunking cap) | Finds maintenance schedules on page 500+ (chunking only saw pages 1-120) |
| **Timeouts** | 360s extraction max (120s per strategy) | Allows complete extraction for complex PDFs; per-strategy checks prevent hangs |
| **Database** | SQLite (dev) / Azure SQL (prod) | Async SQLAlchemy engine, no blocking operations |
| **Blob Upload** | Async thread pool (not blocking) | 30MB uploads take 1 min; don't block event loop (was causing 2+ min hangs) |

---

## Critical Systems

### 1. PDF Extraction Pipeline (`app/rag/pipeline.py`)

**Three-strategy extraction runs in parallel:**

- **Strategy 1: Header Tables** (lines 1035-1165)
  - Detects "Interval | Action | Description" column format
  - Sub-strategy 1b (lines 1118-1162): Checkmark-based intervals
    * Format: columns are "Daily", "Weekly", "500h", "2500h"
    * Cells contain checkmarks (✓, X, ●) indicating which intervals apply
    * Example: HyPET PM tables use this format
  - Extracts via column detection + regex for checkmarks

- **Strategy 2: Generic Tables** (lines 1169-1209)
  - Finds ANY table with numeric interval column (100, 500, 1000, etc.)
  - Looks for action/description in adjacent columns
  - Works on any manufacturer's format

- **Strategy 3: Text Patterns** (lines 1232-1330)
  - 7 regex patterns for different formats:
    1. "every 500 hours → task" (e.g., "every 500h: drain oil")
    2. "500h – task" shorthand (Krones/PTF style)
    3. "every 6 months" (converts to hours)
    4. "every 4 weeks" (converts to hours)
    5. "every 2 years" (converts to hours)
    6. Section headers: "[500 Hour Maintenance]" followed by bullets
    7. Numbered lists under "Every 500h:" header

**All strategies merge results → deduplicate → return combined list.**

**PM-Page Keyword Scan** (lines 1012-1032):
- Scans FULL PDF for PM-relevant pages using keyword regex (_PM_PAGE_KEYWORDS, line 993)
- Returns pages containing: "maintenance schedule", "preventive maintenance", "every X hours", "500h", "1000h", etc.
- Prepends these pages to extraction pool so IBM sees ALL maintenance content
- 60s budget prevents full-PDF scan from stalling

### 2. IBM Enhancement (`app/rag/extractor.py`)

**Batch Processing:**
- Groups 30 chunks per batch (45K chars = ~11,000 tokens)
- Calls `meta-llama/llama-3-3-70b-instruct` with structured JSON prompt
- Validates JSON response, handles malformed output gracefully
- Retries batch if JSON parsing fails (once)

**System Prompt** (lines 42-59):
- Instructs model to extract ALL tasks, no skipping
- Provides manufacturer + known intervals as context
- Requests structured JSON: `[{"interval_hours": 500, "area": "...", "action": "...", ...}, ...]`

**Timeout Handling:**
- Per-batch timeout: 60s (fails gracefully if IBM slow)
- Sequential batches: if time budget exhausted, stop early and return partial results

### 3. Blob Upload (`app/api/routes/manual.py`, lines 92-142)

**Critical Fix (commit f1bbd6d):**
- File upload wrapped in `asyncio.to_thread()` to avoid blocking event loop
- 30MB file upload: ~60s (happens in background thread, doesn't block 202 response)
- Blob re-key operation (copy + delete) also wrapped in thread

**Before fix:** Upload took 168+ seconds before returning 202 Accepted  
**After fix:** Returns 202 in milliseconds, upload happens async

### 4. Embedding (`app/rag/embedder.py`)

**Truncation:**
- IBM Slate-125M max input: 512 tokens (~1900 chars)
- Code truncates to 1800 chars to avoid HTTP 400 errors (lines 94-96)

**Batch Processing:**
- Groups 25 chunks per request
- 5 parallel requests (high concurrency, doesn't saturate IBM API)
- Handles timeouts: `asyncio.wait_for()` with per-call timeout

### 5. Extraction Timeouts (`app/rag/pipeline.py`, lines 844-895)

**Timeout Architecture:**
- Total extraction deadline: 360s (6 min hard cap)
- Per-strategy check: `if time.monotonic() < deadline: run_strategy()`
- Strategy skips if time budget exceeded (returns partial results)
- Logging shows time remaining per strategy for debugging

**Important:** Timeout checks prevent STARTING new strategies but don't STOP hung strategies. If pdfplumber hangs inside a strategy, timeout won't cancel it. Strategies must fail gracefully (within 120s window).

---

## Common Issues & Fixes

### Issue: Extraction takes 50+ minutes (hung)
**Root cause:** pdfplumber table parsing can hang on malformed PDFs.  
**Fix:** Reduce timeouts aggressively OR add per-strategy signal handlers (not implemented).  
**Current:** 360s total timeout + per-strategy checks. If still too slow, skip strategies if time < 60s remaining.  
**Testing:** Eisbar (50pp) should complete in 5-6 min; Tetra Pak (800pp) in 12-15 min.

### Issue: Upload takes 2+ minutes before returning
**Root cause:** Synchronous blob upload blocking event loop (commit f1bbd6d fixed this).  
**Fix:** Wrap in `asyncio.to_thread()` so upload happens in background thread.  
**Verification:** POST returns 202 in milliseconds now.

### Issue: Embedding returns HTTP 400 "model overloaded"
**Root cause:** IBM Slate-125M input exceeds 512 tokens (~1900 chars).  
**Fix:** Truncate to 1800 chars (line 95 in embedder.py).  
**Testing:** Should reduce 400 errors from 20+ → 0.

### Issue: Classification/extraction missing manufacturer
**Root cause:** IBM API timeout (slow network, API rate limiting).  
**Fix:** Keyword fallback (lines 744-745 in manual.py): if classification times out, use regex on text.  
**Manufacturers detected:** Krones, Tetra Pak, Eisbar, Husky HyPET, SIG, Sidel, Bosch (see `_keyword_classify` in classifier.py).

### Issue: Tasks extracted but no page citations
**Root cause:** Extraction didn't return page numbers for tasks (rare).  
**Status:** Marked UNVERIFIED in dashboard (engineer can still approve).  
**Fix:** IBM prompt includes "include page_start and page_end fields" (line 178 in extractor.py).

---

## Database Schema & Key Models

### Tables (SQLAlchemy, `app/db/models.py`)

- **ManualUpload** — one per PDF upload
  - Fields: `manual_id`, `status` (UPLOADED/CLASSIFYING/CHUNKING/EMBEDDING/EXTRACTING/PENDING_REVIEW), `extracted_tasks` (JSON), `detected_manufacturer`, `file_size_bytes`, `blob_url`
  - Status flow: Upload → Classification → Chunking → Embedding → Extraction → Review → Approval

- **Citation** — one per extracted task
  - Fields: `manual_id`, `task_id`, `page_start`, `page_end`, `section`, `content_type`, `excerpt`
  - Used for engineer review + audit trail

- **AuditLog** — every action logged
  - Fields: `action` (MANUAL_UPLOADED, TASKS_APPROVED, TASKS_REJECTED), `user_email`, `timestamp`, `details` (JSON)
  - Immutable: appended, never deleted

### Async Database Access

```python
from app.db.database import AsyncSessionLocal

async with AsyncSessionLocal() as db:
    result = await crud.get_manual_upload(db, manual_id)
    await crud.update_manual_upload(db, manual_id, {"status": "APPROVED"})
    await db.commit()  # Must be explicit
```

**Critical:** Always `await db.commit()` BEFORE `background_tasks.add_task()` to release DB lock. Otherwise, background extraction holds the lock and blocks other requests.

---

## Configuration & Secrets

### `.env` Variables

**Production (IBM watsonx):**
```env
APP_ENV=production
AI_PROVIDER=watsonx
WATSONX_API_KEY=<iam-api-key>
WATSONX_PROJECT_ID=<project-uuid>
WATSONX_URL=https://us-south.ml.cloud.ibm.com
DATABASE_URL=mssql+aioodbc:///?odbc_connect=...
AZURE_STORAGE_ACCOUNT_NAME=niagarapmstorage
DEFAULT_STORAGE_TARGET=azure
```

**Development (local SQLite):**
```env
APP_ENV=development
AI_PROVIDER=watsonx
WATSONX_API_KEY=<your-key>
WATSONX_PROJECT_ID=<your-id>
DATABASE_URL=  # blank = SQLite
DEFAULT_STORAGE_TARGET=local
```

### Secrets in Production

- **Azure Key Vault** loads secrets at startup (lifespan in main.py, line 37)
- App Service Managed Identity authenticates (no passwords in code)
- Config reads: `get_settings()` pulls from `.env` or Key Vault

---

## Testing

### Unit Tests (`pm_project/tests/`)

```bash
# All tests
pytest pm_project/tests/ -v --asyncio-mode=auto

# Single test file
pytest pm_project/tests/test_auth.py -v --asyncio-mode=auto

# With coverage
pytest pm_project/tests/ --cov=app --cov-report=term-missing --asyncio-mode=auto

# Specific test function
pytest pm_project/tests/test_generate.py::test_generate_pdf -v
```

### Integration Testing (Manual)

1. **Local:** `python -m uvicorn app.main:app --reload --port 8000`
2. **Upload PDF:** Dashboard → Upload Manual → select test PDF
3. **Monitor progress:** Status endpoint shows CLASSIFYING → EXTRACTING → PENDING_REVIEW
4. **Check extraction:** `GET /api/manual/uploads/{id}` shows task JSON
5. **Verify times:** Small PDF (50pp) should complete in 5-7 min; large (800pp) in 12-15 min

---

## Key Metrics & Benchmarks

| Step | Expected Time | Timeout | Notes |
|------|---|---|---|
| Upload (streaming) | 1-2 min (async, doesn't block response) | 30s endpoint timeout | File upload happens in background thread |
| Classification | 20-50s | 30s (falls back to keyword) | IBM API latency varies |
| Chunking | 60-80s | 120s (per batch) | pdfplumber parses 50 pages max |
| Embedding | 40-90s | 180s total | IBM Slate-125M, 25 chunks/req, 5 parallel |
| Extraction | 110-240s | 360s total, 120s per strategy | 3 strategies, full PDF scan included |
| **Total** | **5-15 min** | — | Small PDF 5-7 min, large PDF 12-15 min |

---

## Production Deployment Checklist

- [ ] `APP_ENV=production` (disables debug endpoints)
- [ ] `APP_SECRET_KEY` is strong 32+ char random secret
- [ ] `AI_PROVIDER=watsonx` with valid IBM credentials
- [ ] `WATSONX_API_KEY` is IAM API key (NOT service credential)
- [ ] `WATSONX_PROJECT_ID` matches your watsonx project UUID
- [ ] `DATABASE_URL` points to Azure SQL (not SQLite)
- [ ] `DEFAULT_STORAGE_TARGET=azure` with Blob Storage account
- [ ] All secrets in Azure Key Vault (not `.env`)
- [ ] Managed Identity assigned to App Service
- [ ] Azure SQL firewall allows App Service IP
- [ ] Container image built and pushed to ACR
- [ ] Health check endpoint returns 200 OK
- [ ] Test with real PDF: should extract 50-170 tasks in < 15 min

---

## Memory Notes & Known Workarounds

- **Blob upload blocking (cc3d):** Was taking 168+ seconds before returning response. Fixed by wrapping in `asyncio.to_thread()`.
- **Extraction hangs (cc3c):** Timeout checks added, but pdfplumber can still hang inside strategy. Reduced timeout to 360s and added per-strategy checks to skip if time budget exceeded.
- **Embedding HTTP 400:** IBM Slate-125M max 512 tokens. Truncate chunks to 1800 chars to prevent overload.
- **Full-PDF scan:** Keyword scan finds maintenance content on any page, not limited to first 50 pages (earlier versions only chunked 50-80 pages, missed page 400+ maintenance schedules on large PDFs).

---

## Quick Navigation

- **PDF Upload & Pipeline:** `pm_project/app/api/routes/manual.py` (lines 43-174)
- **Extraction Strategies:** `pm_project/app/rag/pipeline.py` (lines 825-895)
- **IBM Llama Integration:** `pm_project/app/rag/extractor.py` (lines 164-200)
- **Embedding & Truncation:** `pm_project/app/rag/embedder.py` (lines 94-96)
- **Approval Workflow:** `pm_project/app/api/routes/manual.py` (lines 270-371)
- **Excel Generation:** `pm_project/app/core/document_generator.py` (lines 60-150)
- **Database Models:** `pm_project/app/db/models.py`
- **Config/Secrets:** `pm_project/app/config.py` (lines 1-100)

---

**Last Updated:** 2026-09-22  
**Build:** cc3d (production-ready, timeout fixes, async blob upload)  
**Status:** Ready for unseen PDF testing
