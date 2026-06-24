from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.config import get_settings
from app.core.pm_generation import generate_pm_document, generate_pm_document_from_manual
from app.db import crud
from app.db.models import ChatMessage, ChatSession, Machine, Task
from app.dependencies import CurrentUserDep, DBDep
from app.rag.chunker import TextChunk
from app.rag.embedder import embed_chunks
from app.rag.retriever import retrieve_top_k

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api/chat", tags=["Chat"])

# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are an expert industrial maintenance AI assistant for Niagara Line 3.
You help maintenance engineers and technicians with:
- Answering questions about preventive maintenance procedures
- Generating step-by-step maintenance checklists
- Explaining safety requirements (LOTO, E-STOP, power isolation)
- Recommending PM intervals and schedules
- Interpreting maintenance manual content

The factory has 5 machines:
- CONTIFORM-C3-L3 (Krones bottle blowing)
- BOTTLECODER-L3 (coding/marking)
- DEHUMIDIFIER-L3 (desiccant dehumidifier)
- VARIOPAC-PRO-L3 (Krones packaging)
- SHRINK-TUNNEL-L3 (heat shrink tunnel)

When asked to generate a checklist, format it as a numbered markdown list with:
- Task number, area, action verb, full description
- Machine state required (RUNNING / STOPPED / POWERED OFF)
- Safety warnings marked with ⚠️

Always prioritize safety. If a task requires LOTO, clearly state it first.
Be concise and practical — these are working technicians on the factory floor."""

# Used when a manual has been uploaded — tells the AI to treat the PDF as ground truth
_SYSTEM_PROMPT_PDF_MODE = """You are an expert industrial maintenance AI assistant.

A machine manual has been uploaded. You have been given relevant excerpts from it below.

CRITICAL RULES — follow these exactly:
1. Answer ONLY from the [MANUAL EXCERPTS] provided. Do not invent procedures not present in the text.
2. If the answer is not in the excerpts, say: "This specific information was not found in the uploaded manual. Try asking with different wording, or check a different section."
3. For checklists: extract and format tasks EXACTLY as described in the manual. Preserve part numbers, measurements, tolerances, and torque values.
4. Cite the source page when available, e.g. "(p.45)"
5. Format checklists as a numbered markdown list with:
   - Task number
   - Area/component
   - Action verb (CHECK / CLEAN / REPLACE / LUBRICATE / INSPECT / VERIFY / LOCKOUT)
   - Full description with values/specs from the manual
   - Machine state: RUNNING | STOPPED | POWERED OFF
   - ⚠️ SAFETY for any LOTO / isolation / danger-zone task
6. Always place SAFETY / LOCKOUT tasks first in any checklist.
7. Be complete — do not summarise or skip steps. Technicians depend on this."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_checklist_request(message: str) -> bool:
    keywords = [
        "checklist", "checlist", "cheklist", "check list", "generate", "create", "make", "give me",
        "list of tasks", "maintenance tasks", "pm tasks", "steps", "procedure",
        "what to do", "how to", "maintenance for", "service steps",
    ]
    low = message.lower()
    return any(k in low for k in keywords)


# ── Conversational document generation ───────────────────────────────────────
# Lets a user type something like "generate the 240hr PM for the bottle coder
# as an Excel sheet" and get back a real, downloadable PDF/DOCX/XLSX — built by
# the same engine as POST /api/generate — instead of just chat text.

_MACHINE_ALIASES: dict[str, list[str]] = {
    "CONTIFORM-C3-L3": ["contiform", "blow moulder", "blow molder", "bottle blowing", "c3 san", "blower"],
    "BOTTLECODER-L3": ["bottle coder", "bottlecoder", "laser coder", "con l3", "coder"],
    "DEHUMIDIFIER-L3": ["dehumidifier", "dehumidify", "das-e8k", "eisbar", "eisbär", "dryer"],
    "VARIOPAC-PRO-L3": ["variopac", "packer", "shrink wrapper", "pro fs", "wrapper"],
    "SHRINK-TUNNEL-L3": ["shrink tunnel", "shrinking tunnel", "tunnel", "ts001"],
}

_FORMAT_KEYWORDS: dict[str, list[str]] = {
    "xlsx": ["excel", "xlsx", "spreadsheet", "xls "],
    "docx": ["word doc", "docx", " word "],
}

_INTERVAL_RE = re.compile(r"(\d{1,6})\s*-?\s*(?:hour|hr|hrs|hours)\b", re.IGNORECASE)


def _detect_machine(message: str, machines: list[Machine], hint: Optional[str] = None) -> Optional[str]:
    low = f" {message.lower()} "
    valid_ids = {m.machine_id for m in machines}
    if hint and hint in valid_ids:
        return hint
    for m in machines:
        if m.machine_id.lower() in low or m.name.lower() in low:
            return m.machine_id
    for machine_id, aliases in _MACHINE_ALIASES.items():
        if machine_id in valid_ids and any(a in low for a in aliases):
            return machine_id
    return None


def _detect_interval(message: str) -> Optional[int]:
    match = _INTERVAL_RE.search(message)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _detect_format(message: str) -> str:
    low = f" {message.lower()} "
    for fmt, keywords in _FORMAT_KEYWORDS.items():
        if any(k in low for k in keywords):
            return fmt
    return "pdf"


async def _try_generate_document(db, user, message: str, effective_machine: Optional[str]) -> Optional[dict]:
    """
    If this message is asking to produce/download an actual PM document and we
    can identify the machine + interval, generate it for real and return a
    result dict. Returns None if this doesn't look like a document request —
    callers should fall through to the normal AI chat flow in that case.
    """
    if not _is_checklist_request(message):
        return None

    machines = await crud.get_all_machines(db)
    if not machines:
        return None

    machine_id = _detect_machine(message, machines, hint=effective_machine)
    interval_hours = _detect_interval(message)
    if not machine_id or interval_hours is None:
        return None

    output_format = _detect_format(message)
    doc = await generate_pm_document(db, user, machine_id, interval_hours, output_format=output_format)
    if doc:
        return {"ok": True, **doc}

    # Machine recognised but no tasks stored for that interval — show what is available
    intervals = await crud.get_intervals_for_machine(db, machine_id)
    machine = next((m for m in machines if m.machine_id == machine_id), None)
    return {
        "ok": False,
        "machine_id": machine_id,
        "machine_name": machine.name if machine else machine_id,
        "available_intervals": [iv.label for iv in intervals],
    }


def _format_document_reply(result: dict) -> str:
    if result["ok"]:
        size_kb = result["file_size_bytes"] / 1024
        return (
            f"✅ **Generated the {result['interval_label']} PM checklist for {result['machine_name']}**\n\n"
            f"- {result['task_count']} tasks included, pulled straight from the PM Library\n"
            f"- Format: {result['output_format'].upper()} ({size_kb:.1f} KB)\n\n"
            f"[⬇ Download {result['file_name']}]({result['download_url']})"
        )
    avail = ", ".join(result["available_intervals"]) or "none generated yet — try uploading the manual first"
    example = result["available_intervals"][0] if result["available_intervals"] else "120hr"
    return (
        f"I couldn't find PM tasks for **{result['machine_name']}** at that interval in the library.\n\n"
        f"Available intervals for this machine: **{avail}**\n\n"
        f"Try: \"Generate the {example} PM checklist for {result['machine_name']}\" "
        f"(add \"as an Excel sheet\" or \"as a Word doc\" for other formats — PDF is the default)."
    )


def _format_manual_document_reply(result: dict) -> str:
    size_kb = result["file_size_bytes"] / 1024
    return (
        f"✅ **Generated a PM checklist from {result['source_manual']}**\n\n"
        f"- {result['task_count']} maintenance tasks extracted from this manual\n"
        f"- Format: {result['output_format'].upper()} ({size_kb:.1f} KB)\n\n"
        f"[⬇ Download {result['file_name']}]({result['download_url']})"
    )


_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_GENERIC_WORDS = frozenset({
    "the", "this", "that", "for", "and", "with", "from", "machine", "manual",
    "generate", "create", "make", "give", "checklist", "checlist", "cheklist",
    "check", "list", "pdf", "xlsx", "excel", "word", "download", "all",
    "hours", "hour", "operating", "complete", "maintenance", "preventive",
    "tasks", "steps", "procedure", "service",
})


async def _resolve_manual_id(db, message: str, current_manual_id: Optional[str]) -> Optional[str]:
    """
    If the message names a different machine/manual than the one currently
    attached to the chat (e.g. "give me the checklist for the variopac" while
    a Dehumidifier manual is active), redirect to the manual that matches —
    instead of silently generating a checklist from whichever manual happens
    to be active.
    """
    low = message.lower()
    uploads = await crud.get_manual_uploads(db, limit=50)
    ready = [u for u in uploads if u.status in ("PENDING_REVIEW", "APPROVED")]
    if not ready:
        return current_manual_id

    best_match: Optional[str] = None
    best_score = 0
    current_score = 0
    for u in ready:
        tokens = set(_WORD_RE.findall(Path(u.original_filename).stem.lower()))
        if u.detected_manufacturer:
            tokens.update(_WORD_RE.findall(u.detected_manufacturer.lower()))
        if u.machine_id:
            tokens.update(_WORD_RE.findall(u.machine_id.lower()))
        tokens -= _GENERIC_WORDS
        score = sum(1 for t in tokens if t in low)
        if u.manual_id == current_manual_id:
            current_score = score
        if score > best_score:
            best_score = score
            best_match = u.manual_id

    if best_match and best_match != current_manual_id and best_score > current_score:
        return best_match
    return current_manual_id


async def _retrieve_context(
    query: str,
    machine_id: Optional[str] = None,
    manual_id: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Embed query and retrieve top-k most relevant chunks from the vector store."""
    try:
        dummy_chunk = [TextChunk(chunk_id="q", text=query, page_start=0, page_end=0, char_start=0, char_end=len(query), source_file="")]
        embeddings = await embed_chunks(dummy_chunk)
        if not embeddings:
            return ""
        q_vector = embeddings[0]["embedding"]
        chunks = await retrieve_top_k(q_vector, manual_id=manual_id, top_k=top_k)
        if not chunks:
            return ""
        parts = []
        for i, c in enumerate(chunks, 1):
            page_ref = f" (p.{c.get('page_start', 0)})" if c.get('page_start') else ""
            score = c.get('score', 0)
            if score > 0:
                parts.append(f"[Excerpt {i}{page_ref} | relevance={score:.2f}]\n{c['text']}")
            else:
                parts.append(f"[Excerpt {i}{page_ref}]\n{c['text']}")
        return "\n\n---\n\n".join(parts)
    except Exception as exc:
        logger.warning("RAG retrieval failed: %s", exc)
        return ""


async def _get_extracted_tasks_note(db, manual_id: str) -> str:
    """If the pipeline has finished extracting tasks, add a summary note for the AI."""
    try:
        from app.db.crud import get_manual_upload
        import json as _json
        upload = await get_manual_upload(db, manual_id)
        if not upload:
            return ""
        if upload.status in ("PENDING_REVIEW", "APPROVED"):
            tasks = _json.loads(upload.extracted_tasks or "[]")
            if tasks:
                return (
                    f"[PIPELINE COMPLETE] This manual has been fully processed. "
                    f"{len(tasks)} maintenance tasks were automatically extracted from it. "
                    f"File: {upload.original_filename}"
                )
        elif upload.status in ("CLASSIFYING", "CHUNKING", "EMBEDDING", "EXTRACTING"):
            return f"[PIPELINE RUNNING — status: {upload.status}] Indexing in progress. Some answers may be incomplete."
        elif upload.status == "FAILED":
            return f"[PIPELINE FAILED] Could not process this manual: {upload.error_message}"
        return ""
    except Exception:
        return ""


async def _get_library_context(db, machine_id: Optional[str] = None) -> str:
    """Pull relevant tasks from PM Library for grounding."""
    try:
        stmt = select(Task).where(Task.is_active == True)
        if machine_id:
            stmt = stmt.where(Task.machine_id == machine_id)
        stmt = stmt.limit(30)
        result = await db.execute(stmt)
        tasks = result.scalars().all()
        if not tasks:
            return ""
        lines = ["Existing PM Library tasks (sample):"]
        for t in tasks:
            safety = " ⚠️ SAFETY" if t.safety_flag else ""
            lines.append(
                f"  [{t.machine_id} | {t.interval_hours}hr | {t.action}] {t.description}{safety}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Library context failed: %s", exc)
        return ""


async def _call_ai_chat(messages: list[dict]) -> str:
    """Call OpenAI or watsonx for a chat response based on AI_PROVIDER env var."""
    if settings.ai_provider == "watsonx":
        return await _chat_watsonx(messages)
    return await _chat_openai(messages)


async def _chat_openai(messages: list[dict]) -> str:
    import httpx
    if not settings.openai_api_key:
        return (
            "**AI not configured**\n\n"
            "No AI API key is set. To enable the chat assistant:\n\n"
            "1. Open your `.env` file\n"
            "2. Set `OPENAI_API_KEY=sk-...` (for OpenAI) **or** set `AI_PROVIDER=watsonx` and fill `WATSONX_API_KEY`\n"
            "3. Restart the server\n\n"
            "Until then, I can still answer questions about your **PM Library** tasks (no AI key needed for library lookups).\n\n"
            "**Your PM Library has tasks for:** CONTIFORM-C3-L3, BOTTLECODER-L3, DEHUMIDIFIER-L3, VARIOPAC-PRO-L3, SHRINK-TUNNEL-L3"
        )
    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_model_generation,
                    "messages": messages,
                    "max_tokens": 2500,
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.error("OpenAI chat error: %s", exc)
        return f"AI error: {exc}"


async def _chat_watsonx(messages: list[dict]) -> str:
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    if not settings.watsonx_api_key:
        return (
            "**watsonx not configured**\n\n"
            "Set these in your `.env` file and restart:\n\n"
            "```\nAI_PROVIDER=watsonx\nWATSONX_API_KEY=your-ibm-api-key\nWATSONX_PROJECT_ID=your-project-id\n```\n\n"
            "Get your API key from: cloud.ibm.com → Manage → Access (IAM) → API keys"
        )

    prompt_parts = []
    for m in messages:
        if m["role"] == "system":
            prompt_parts.append(f"[INST] <<SYS>>\n{m['content']}\n<</SYS>>")
        elif m["role"] == "user":
            prompt_parts.append(f"[INST] {m['content']} [/INST]")
        elif m["role"] == "assistant":
            prompt_parts.append(m["content"])
    prompt = "\n".join(prompt_parts)

    # Use dedicated chat model (ibm/granite-3-8b-instruct)
    chat_model = getattr(settings, "watsonx_model_chat", settings.watsonx_model_analytics)
    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    headers = await watsonx_headers(settings.watsonx_api_key)
    payload = {
        "model_id": chat_model,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {
            "max_new_tokens": 1500,
            "temperature": 0.3,
            "repetition_penalty": 1.1,
            "stop_sequences": ["Human:", "User:", "\n\n\n"],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["results"][0]["generated_text"].strip()
    except Exception as exc:
        logger.error("watsonx chat error (model=%s): %s", chat_model, exc)
        return f"AI error: {exc}"


# ── Session management ────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(
    user: CurrentUserDep,
    db: DBDep,
    machine_id: Optional[str] = Body(None),
    title: str = Body("New Chat"),
):
    session = ChatSession(
        session_id=str(uuid.uuid4()),
        user_id=user.user_id,
        machine_id=machine_id,
        title=title,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {
        "session_id": session.session_id,
        "title": session.title,
        "machine_id": session.machine_id,
        "created_at": session.created_at,
    }


@router.get("/sessions")
async def list_sessions(user: CurrentUserDep, db: DBDep):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.user_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(20)
    )
    sessions = result.scalars().all()
    return [
        {
            "session_id": s.session_id,
            "title": s.title,
            "machine_id": s.machine_id,
            "message_count": s.message_count,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in sessions
    ]


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, user: CurrentUserDep, db: DBDep):
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    messages = result.scalars().all()
    return [
        {
            "message_id": m.message_id,
            "role": m.role,
            "content": m.content,
            "has_checklist": m.has_checklist,
            "created_at": m.created_at,
        }
        for m in messages
    ]


# ── Main chat endpoint ────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str,
    user: CurrentUserDep,
    db: DBDep,
    message: str = Body(...),
    machine_id: Optional[str] = Body(None),
    manual_id: Optional[str] = Body(None),
):
    # Load session
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == user.user_id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        from fastapi import HTTPException
        raise HTTPException(404, "Session not found")

    effective_machine = machine_id or session.machine_id

    # Save user message
    user_msg = ChatMessage(
        message_id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=message,
    )
    db.add(user_msg)

    # Load recent history (last 10 turns)
    hist_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(10)
    )
    history = list(reversed(hist_result.scalars().all()))

    download_url: Optional[str] = None
    rag_used = False
    is_checklist = _is_checklist_request(message)

    if manual_id:
        # ── PDF mode: answer from the uploaded manual ─────────────────────────
        has_document = False

        # If the user is asking for a checklist/document, generate a real
        # PDF/XLSX/DOCX from the AI-extracted tasks for THIS manual (same
        # GMP template as the PM Library export).
        if is_checklist:
            # If the message names a different machine than the one currently
            # attached (e.g. "checklist for the variopac" while the Dehumidifier
            # manual is active), redirect to the matching uploaded manual.
            manual_id = await _resolve_manual_id(db, message, manual_id)
            output_format = _detect_format(message)
            doc = await generate_pm_document_from_manual(db, user, manual_id, output_format=output_format)
            if doc:
                reply = _format_manual_document_reply(doc)
                is_checklist = False
                has_document = True
                download_url = doc["download_url"]

        if not has_document:
            rag_context = await _retrieve_context(message, effective_machine, manual_id=manual_id)
            rag_used = bool(rag_context)

            if not rag_context:
                # No indexed content for this manual yet — give a fast, accurate
                # status update instead of letting a small local model improvise
                # an answer it can't back up (it tends to claim "I can't read PDFs").
                upload = await crud.get_manual_upload(db, manual_id)
                upload_status = upload.status if upload else "UNKNOWN"
                filename = upload.original_filename if upload else "your manual"

                if upload_status == "FAILED":
                    reply = (
                        f"⚠️ Processing failed for **{filename}**: "
                        f"{upload.error_message or 'unknown error'}.\n\n"
                        "Please try re-uploading the manual."
                    )
                elif upload_status not in ("PENDING_REVIEW", "APPROVED"):
                    reply = (
                        f"⏳ **\"{filename}\" is still processing** (status: {upload_status}).\n\n"
                        "This usually takes 1–3 minutes. Please wait a moment and ask again — "
                        "I'll be able to search the document as soon as indexing finishes."
                    )
                else:
                    # Indexed, but nothing matched this query — fall back to the AI
                    # with general knowledge, being explicit about the limitation.
                    extracted_note = await _get_extracted_tasks_note(db, manual_id)
                    system_content = _SYSTEM_PROMPT_PDF_MODE
                    if effective_machine:
                        system_content += f"\n\nMachine context: {effective_machine}"
                    if extracted_note:
                        system_content += f"\n\n{extracted_note}"
                    system_content += (
                        "\n\n[NOTE: No matching excerpts were found in the uploaded manual for this "
                        "question. Say so explicitly, then answer from general maintenance knowledge "
                        "if you can.]"
                    )
                    ai_messages = [{"role": "system", "content": system_content}]
                    for h in history[-6:]:
                        ai_messages.append({"role": h.role, "content": h.content})
                    ai_messages.append({"role": "user", "content": message})
                    reply = await _call_ai_chat(ai_messages)
            else:
                # Also check if the manual has already had tasks extracted (pipeline complete)
                extracted_note = await _get_extracted_tasks_note(db, manual_id)

                system_content = _SYSTEM_PROMPT_PDF_MODE
                if effective_machine:
                    system_content += f"\n\nMachine context: {effective_machine}"
                if extracted_note:
                    system_content += f"\n\n{extracted_note}"
                system_content += f"\n\n[MANUAL EXCERPTS — answer from these]\n\n{rag_context}"

                ai_messages = [{"role": "system", "content": system_content}]
                for h in history[-6:]:  # fewer history turns to save context space for PDF content
                    ai_messages.append({"role": h.role, "content": h.content})
                ai_messages.append({"role": "user", "content": message})
                reply = await _call_ai_chat(ai_messages)

    else:
        # ── PM Library mode: document generation or AI + library context ─────
        # Try deterministic document generation first (no AI needed)
        doc_result = await _try_generate_document(db, user, message, effective_machine)

        if doc_result is not None:
            reply = _format_document_reply(doc_result)
            is_checklist = False
            has_document = bool(doc_result.get("ok"))
            download_url = doc_result.get("download_url")
        else:
            has_document = False
            rag_context = await _retrieve_context(message, effective_machine, manual_id=None)
            lib_context = await _get_library_context(db, effective_machine)
            rag_used = bool(rag_context)

            context_block = ""
            if rag_context:
                context_block += f"\n\n[MANUAL EXCERPTS]\n{rag_context}"
            if lib_context:
                context_block += f"\n\n[PM LIBRARY REFERENCE]\n{lib_context}"

            system_content = _SYSTEM_PROMPT
            if effective_machine:
                system_content += f"\n\nCurrent context machine: {effective_machine}"
            if is_checklist:
                system_content += "\n\nThe user wants a checklist. Format your response as a numbered markdown checklist."
            if context_block:
                system_content += f"\n\nContext:{context_block}"

            ai_messages = [{"role": "system", "content": system_content}]
            for h in history:
                ai_messages.append({"role": h.role, "content": h.content})
            ai_messages.append({"role": "user", "content": message})
            reply = await _call_ai_chat(ai_messages)

    # Save assistant message
    assistant_msg = ChatMessage(
        message_id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=reply,
        has_checklist=is_checklist,
    )
    db.add(assistant_msg)

    # Update session
    session.message_count = (session.message_count or 0) + 2
    session.updated_at = datetime.utcnow()
    if session.message_count == 2:
        session.title = message[:60] + ("..." if len(message) > 60 else "")

    await db.commit()

    return {
        "message_id": assistant_msg.message_id,
        "role": "assistant",
        "content": reply,
        "has_checklist": is_checklist,
        "has_document": has_document,
        "download_url": download_url,
        "rag_used": rag_used,
        "created_at": assistant_msg.created_at,
    }


# ── Quick chat (no session required) ─────────────────────────────────────────

@router.post("/quick")
async def quick_chat(
    user: CurrentUserDep,
    db: DBDep,
    message: str = Body(...),
    machine_id: Optional[str] = Body(None),
    manual_id: Optional[str] = Body(None),
):
    """Single-turn chat without session persistence — for quick questions."""
    doc_result = await _try_generate_document(db, user, message, machine_id)
    if doc_result is not None:
        return {
            "role": "assistant",
            "content": _format_document_reply(doc_result),
            "has_checklist": False,
            "has_document": bool(doc_result.get("ok")),
            "download_url": doc_result.get("download_url"),
            "rag_used": False,
        }

    rag_context = await _retrieve_context(message, machine_id, manual_id=manual_id)
    lib_context = await _get_library_context(db, machine_id)

    context_block = ""
    if rag_context:
        context_block += f"\n\n## Relevant Manual Excerpts\n{rag_context}"
    if lib_context:
        context_block += f"\n\n## PM Library Reference\n{lib_context}"

    is_checklist = _is_checklist_request(message)
    system_content = _SYSTEM_PROMPT
    if machine_id:
        system_content += f"\n\nContext machine: {machine_id}"
    if is_checklist:
        system_content += "\n\nFormat your response as a numbered markdown checklist."
    if context_block:
        system_content += f"\n\nRetrieved context:{context_block}"

    ai_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": message},
    ]
    reply = await _call_ai_chat(ai_messages)

    return {
        "role": "assistant",
        "content": reply,
        "has_checklist": is_checklist,
        "has_document": False,
        "download_url": None,
        "rag_used": bool(rag_context),
    }


@router.get("/status")
async def chat_status(user: CurrentUserDep):
    """Returns whether AI is configured so the UI can show a warning."""
    ai_ready = False
    if settings.ai_provider == "watsonx":
        ai_ready = bool(settings.watsonx_api_key and settings.watsonx_project_id)
    else:
        ai_ready = bool(settings.openai_api_key)
    model_info = {}
    if settings.ai_provider == "watsonx":
        model_info = {
            "chat_model": getattr(settings, "watsonx_model_chat", settings.watsonx_model_analytics),
            "extraction_model": settings.watsonx_model_generation,
            "classification_model": settings.watsonx_model_classification,
            "embedding_model": settings.watsonx_embedding_model,
            "watsonx_url": settings.watsonx_url,
        }
    else:
        model_info = {
            "chat_model": settings.openai_model_generation,
            "classification_model": settings.openai_model_classification,
            "embedding_model": settings.openai_embedding_model,
            "endpoint": settings.openai_base_url or "https://api.openai.com/v1",
            "local_mode": bool(settings.openai_base_url),
        }
    return {
        "ai_provider": settings.ai_provider,
        "ai_ready": ai_ready,
        "rag_ready": bool(settings.azure_search_endpoint),
        "models": model_info,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: CurrentUserDep, db: DBDep):
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.user_id == user.user_id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        from fastapi import HTTPException
        raise HTTPException(404, "Session not found")
    await db.execute(
        ChatMessage.__table__.delete().where(ChatMessage.session_id == session_id)
    )
    await db.delete(session)
    await db.commit()
    return {"deleted": True}
