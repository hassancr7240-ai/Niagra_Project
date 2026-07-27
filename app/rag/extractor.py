from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_TASK_SCHEMA = {
    "type": "object",
    "required": ["task_no", "area", "action", "description", "machine_state", "interval_hours"],
    "properties": {
        "task_no": {"type": "integer"},
        "area": {"type": "string"},
        "action": {"type": "string", "enum": [
            "LOCKOUT", "CHECK", "LISTEN", "CLEAN", "REPLACE", "LUBRICATE",
            "VERIFY", "TEST", "CONFIRM", "INSPECT",
        ]},
        "description": {"type": "string"},
        "machine_state": {"type": "string", "enum": ["RUNNING", "STOPPED", "POWERED_OFF"]},
        "safety_flag": {"type": "boolean"},
        "part_number": {"type": ["string", "null"]},
        "interval_hours": {"type": "integer"},
    },
}

_SYSTEM_PROMPT = """You are an expert industrial maintenance engineer extracting PM tasks from machine manuals.

Extract ONLY tasks that are explicitly stated in the manual text. Do NOT invent or hallucinate tasks.

For each task output:
- task_no: task number (start at 10, increment by 10)
- area: component/area category in CAPITALS (e.g. CONVEYOR, DRIVE SYSTEM, ELECTRICAL, FILTER, LUBRICATION, SAFETY, SENSORS)
- action: verb in CAPITALS (LOCKOUT, CHECK, CLEAN, REPLACE, LUBRICATE, VERIFY, TEST, CONFIRM, INSPECT)
- description: full task instruction in CAPITAL LETTERS, include specific details from the manual (part numbers, lubricant types, torque specs, measurements)
- machine_state: RUNNING = task done while machine is producing (visual checks, listening, monitoring temperature/vibration) | STOPPED = machine halted but still powered (adjustments, tightening, alignment, filter cleaning) | POWERED_OFF = full LOTO isolation required (internal access, electrical work, replacing parts inside guards)
- safety_flag: true if task involves LOTO, E-STOP, power isolation, or entering danger zone
- part_number: exact part number if mentioned in the text, else null
- interval_hours: the EXACT interval from the manual text. Look for phrases like "every X hours", "every X operating hours", "alle X Betriebsstunden". Convert calendar time: 1 day=8hr, 1 week=120hr, 2 weeks=240hr, 1 month=500hr, 3 months=1500hr, 6 months=3000hr, 1 year=6000hr, 7 years=42000hr

CRITICAL RULES:
- interval_hours: use the exact number from the manual (500, 1000, 2000, etc.). If you cannot find an interval, use 500 as the default. Do NOT omit or set to 0.
- Every task description must be in CAPITAL LETTERS
- Extract tasks that have a clear maintenance action (check, inspect, replace, clean, lubricate, verify, test). Do NOT extract warnings or general information.
- Return ONLY valid JSON array of task objects, no explanation"""


async def extract_tasks_from_chunks(
    chunks: list[dict],
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]] = None,
) -> list[dict]:
    """Use LLM to extract structured PM tasks from retrieved manual chunks."""
    # Send more chunks so AI sees enough maintenance content across all intervals
    # granite3.3:8b handles ~8192 tokens — 15 chunks x 500 chars fits comfortably
    combined_text = "\n\n---\n\n".join(c["text"][:500] for c in chunks[:15])

    if settings.ai_provider == "watsonx":
        return await _extract_watsonx(combined_text, manufacturer, model, interval_hints)
    return await _extract_ollama(combined_text, manufacturer, model, interval_hints)

async def _extract_ollama(  # Ollama (local IBM Granite) via OpenAI-compatible API

    text: str,
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]],
) -> list[dict]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)

    user_msg = f"""Machine: {manufacturer} {model or ''}
Known intervals (hours): {interval_hints or 'detect from text'}

Manual text:
{text[:3500]}

Return a JSON array of task objects only. Example: [{{"task_no":10,"area":"FILTER",...}}]"""

    try:
        response = await client.chat.completions.create(
            model=settings.openai_model_generation,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=2000,
            # NOTE: response_format json_object NOT used — granite3.3:2b crashes Ollama with it
        )
        content = response.choices[0].message.content or "[]"
        # Model may return a bare array or {"tasks":[...]} wrapper
        raw = _extract_json_array(content)
        if raw == "[]":
            data = json.loads(content) if content.strip().startswith("{") else []
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
        else:
            tasks = json.loads(raw)
        return _validate_tasks(tasks)
    except Exception as exc:
        logger.error("OpenAI task extraction failed: %s", exc)
        return []


async def _extract_watsonx(
    text: str,
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]],
) -> list[dict]:
    """
    IBM watsonx.ai granite-3b-code-instruct task extraction.
    Uses IAM token auth. Model is optimised for structured JSON output.
    """
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    if not settings.watsonx_api_key:
        logger.error("WATSONX_API_KEY not set")
        return []

    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    prompt = f"""{_SYSTEM_PROMPT}

Machine: {manufacturer} {model or ''}
Known intervals: {interval_hints or 'detect from text'}

Manual text:
{text[:6000]}

Return JSON array of tasks:"""

    payload = {
        "model_id": settings.watsonx_model_generation,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {
            "max_new_tokens": 3000,
            "temperature": 0,
            "repetition_penalty": 1.1,
        },
    }
    try:
        headers = await watsonx_headers(settings.watsonx_api_key)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text_out = data["results"][0]["generated_text"]
            tasks = json.loads(_extract_json_array(text_out))
            return _validate_tasks(tasks)
    except Exception as exc:
        logger.error("watsonx extraction failed: %s", exc)
        return []


def _extract_json_array(text: str) -> str:
    m = re.search(r"\[[\s\S]*\]", text)
    return m.group(0) if m else "[]"


_VALID_INTERVALS = {
    8, 24, 40, 100, 120, 240, 500, 1000, 1500, 2000, 3000, 4000, 5000,
    6000, 8000, 10000, 12000, 18000, 24000, 30000, 42000, 45000,
}


def _snap_interval(raw: int) -> int:
    """Snap a raw interval value to the nearest known PM interval."""
    if raw in _VALID_INTERVALS:
        return raw
    # Find closest known interval
    return min(_VALID_INTERVALS, key=lambda v: abs(v - raw))


def _validate_tasks(raw_tasks: list) -> list[dict]:
    """Validate extracted tasks against schema. Auto-fix common issues."""
    valid = []
    valid_actions = {"LOCKOUT", "CHECK", "LISTEN", "CLEAN", "REPLACE", "LUBRICATE",
                     "VERIFY", "TEST", "CONFIRM", "INSPECT"}
    valid_states = {"RUNNING", "STOPPED", "POWERED_OFF"}

    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            continue

        t["task_no"] = t.get("task_no") or (i + 1) * 10
        t["area"] = str(t.get("area", "GENERAL")).upper()[:64]
        action = str(t.get("action", "CHECK")).upper()
        t["action"] = action if action in valid_actions else "CHECK"
        desc = str(t.get("description", "")).strip().upper()
        # If description is missing, synthesise from area + action so the task is not lost
        if not desc:
            desc = f"{t['action']} {t['area']}"
        t["description"] = desc
        state = str(t.get("machine_state", "")).upper()
        if state not in valid_states:
            # Infer from description when AI didn't set a valid state
            desc_low = t["description"].lower()
            if any(k in desc_low for k in ("loto", "lockout", "tagout", "de-energi", "powered off")):
                state = "POWERED_OFF"
            elif any(k in desc_low for k in ("listen", "monitor", "visual", "observe", "temperature", "vibration", "noise", "while running", "during operation")):
                state = "RUNNING"
            else:
                state = "STOPPED"
        t["machine_state"] = state
        t["safety_flag"] = bool(t.get("safety_flag", False))
        t["part_number"] = t.get("part_number") or None

        raw_iv = int(t.get("interval_hours") or 0)
        if raw_iv == 0:
            # Model didn't fill interval — default to 500hr (monthly) so the task is not silently dropped
            t["interval_hours"] = 500
            logger.debug("Task #%d missing interval_hours — defaulted to 500hr", t["task_no"])
        elif raw_iv not in _VALID_INTERVALS:
            t["interval_hours"] = _snap_interval(raw_iv)
        else:
            t["interval_hours"] = raw_iv

        if t["description"]:
            valid.append(t)

    logger.info("Validated %d/%d extracted tasks", len(valid), len(raw_tasks))
    return valid
