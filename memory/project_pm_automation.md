---
name: PM Automation System Project
description: Complete production-level PM Automation System built at c:\Users\PC\Desktop\niagara_project\pm_project
type: project
---

Full production FastAPI PM Automation System built from two architecture diagrams and the PM_Complete_Story_Document.docx.

**Why:** Factory Line 3 maintenance was done manually (2-4hrs per checklist). System automates PDF/DOCX/XLSX generation of GMP-compliant PM checklists from a library of 146 tasks across 4 machines.

**How to apply:** When user asks about this project, know it is fully built and runnable. Run with: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

Key facts:
- 4 machines: Contiform C3 SAN (51 tasks), Bottle Coder CON L3 (33), Dehumidifier DAS-E8K.2 (17), Variopac Pro FS (45)
- 16 PM intervals total, 146 tasks, all in data/pm_library.json
- CON L3 240hr is the template (28 tasks, task numbering 10/20/30...)
- PDF uses ReportLab with colour-coded sections (green=RUNNING, amber=STOPPED, purple=POWERED_OFF, red=safety)
- API: POST /api/generate, GET /api/history, GET /api/library, POST /api/manual/upload
- Auth: Azure AD JWT (production) or dev API key (development, key="dev-secret-key-change-in-prod")
- DB: SQLite local dev, Azure SQL production — auto-seeded from pm_library.json on startup
- Storage: local (default), Azure Blob, FTP/SFTP
- RAG pipeline: PDF → classify → chunk (500w/103w overlap) → embed → Azure AI Search → top 10 → GPT-4o extract → engineer review → library
- CI/CD: GitHub Actions with Bandit security scan → Docker → Azure App Service
- All 16 PDFs pre-generated in output/pm-docs/
