from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ChatMessage, ChatSession, Machine, Task
from app.dependencies import CurrentUserDep, DBDep
from app.rag.embedder import embed_chunks
from app.rag.retriever import retrieve_top_k

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api/chat", tags=["Chat"])

# ── System prompt ─────────────────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_checklist_request(message: str) -> bool:
    keywords = [
        "checklist", "check list", "generate", "create", "make", "give me",
        "list of tasks", "maintenance tasks", "pm tasks", "steps", "procedure",
        "what to do", "how to", "maintenance for", "service steps",
    ]
    low = message.lower()
    return any(k in low for k in keywords)


async def _retrieve_context(query: str, machine_id: Optional[str] = None) -> str:
    """Embed query and retrieve top-k chunks from Azure AI Search."""
    try:
        dummy_chunk = [{"chunk_id": "q", "text": query, "page_start": 0, "page_end": 0, "source_file": ""}]
        embeddings = await embed_chunks(dummy_chunk)
        if not embeddings:
            return ""
        q_vector = embeddings[0]["embedding"]
        chunks = await retrieve_top_k(q_vector, manual_id=None, top_k=6)
        if not chunks:
            return ""
        parts = []
        for i, c in enumerate(chunks, 1):
            parts.append(f"[Source {i} — {c.get('source_file','manual')} p.{c.get('page_start',0)}]\n{c['text']}")
        return "\n\n---\n\n".join(parts)
    except Exception as exc:
        logger.warning("RAG retrieval failed: %s", exc)
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
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_model_generation,
                    "messages": messages,
                    "max_tokens": 1500,
                    "temperature": 0.3,
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

    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    headers = await watsonx_headers(settings.watsonx_api_key)
    payload = {
        "model_id": settings.watsonx_model_generation,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {
            "max_new_tokens": 1500,
            "temperature": 0.3,
            "repetition_penalty": 1.1,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["results"][0]["generated_text"].strip()
    except Exception as exc:
        logger.error("watsonx chat error: %s", exc)
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

    # Build context
    rag_context = await _retrieve_context(message, effective_machine)
    lib_context = await _get_library_context(db, effective_machine)

    context_block = ""
    if rag_context:
        context_block += f"\n\n## Relevant Manual Excerpts\n{rag_context}"
    if lib_context:
        context_block += f"\n\n## PM Library Reference\n{lib_context}"

    # Build message list for AI
    is_checklist = _is_checklist_request(message)
    system_content = _SYSTEM_PROMPT
    if effective_machine:
        system_content += f"\n\nCurrent context machine: {effective_machine}"
    if is_checklist:
        system_content += "\n\nThe user wants a checklist. Format your response as a numbered markdown checklist."
    if context_block:
        system_content += f"\n\nRetrieved context from manuals and library:{context_block}"

    ai_messages = [{"role": "system", "content": system_content}]
    for h in history:
        ai_messages.append({"role": h.role, "content": h.content})
    ai_messages.append({"role": "user", "content": message})

    # Call AI
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
        "rag_used": bool(rag_context),
        "created_at": assistant_msg.created_at,
    }


# ── Quick chat (no session required) ─────────────────────────────────────────

@router.post("/quick")
async def quick_chat(
    user: CurrentUserDep,
    db: DBDep,
    message: str = Body(...),
    machine_id: Optional[str] = Body(None),
):
    """Single-turn chat without session persistence — for quick questions."""
    rag_context = await _retrieve_context(message, machine_id)
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
    return {
        "ai_provider": settings.ai_provider,
        "ai_ready": ai_ready,
        "rag_ready": bool(settings.azure_search_endpoint),
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
