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

Extract ALL maintenance tasks from the provided text. For each task output:
- task_no: task number (start at 10, increment by 10)
- area: component/area category in CAPITALS (e.g. SAFETY, INSPECT, CLEAN, LUBRICATE, REPLACE)
- action: verb in CAPITALS (LOCKOUT, CHECK, CLEAN, REPLACE, LUBRICATE, VERIFY, TEST, CONFIRM)
- description: full task instruction in CAPITAL LETTERS, detailed enough for a trained technician
- machine_state: RUNNING (machine producing) | STOPPED (stopped, still powered) | POWERED_OFF (fully isolated)
- safety_flag: true if task involves LOTO, E-STOP, power isolation, or entering danger zone
- part_number: exact part number if task involves replacing a consumable, else null
- interval_hours: maintenance interval in hours (convert calendar time: 1 day=8hr, 1 week=120hr, 2 weeks=240hr, 1 month=500hr, 3 months=1500hr, 6 months=3000hr, 1 year=6000hr)

IMPORTANT:
- Every task description must be in CAPITAL LETTERS
- Start safety/LOTO tasks first
- Group tasks by machine state (RUNNING first, then STOPPED, then POWERED_OFF)
- Always end with a GMP return-to-service confirmation task
- Return ONLY valid JSON array of task objects, no explanation"""


async def extract_tasks_from_chunks(
    chunks: list[dict],
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]] = None,
) -> list[dict]:
    """Use LLM to extract structured PM tasks from retrieved manual chunks."""
    combined_text = "\n\n---\n\n".join(c["text"] for c in chunks[:10])

    if settings.ai_provider == "watsonx":
        return await _extract_watsonx(combined_text, manufacturer, model, interval_hints)
    return await _extract_openai(combined_text, manufacturer, model, interval_hints)


async def _extract_openai(
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
{text[:12000]}

Extract all maintenance tasks as a JSON array. Validate task_no increments by 10."""

    try:
        response = await client.chat.completions.create(
            model=settings.openai_model_generation,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or '{"tasks":[]}'
        data = json.loads(content)
        tasks = data if isinstance(data, list) else data.get("tasks", [])
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
        t["description"] = str(t.get("description", "")).upper()
        state = str(t.get("machine_state", "STOPPED")).upper()
        t["machine_state"] = state if state in valid_states else "STOPPED"
        t["safety_flag"] = bool(t.get("safety_flag", False))
        t["part_number"] = t.get("part_number") or None
        t["interval_hours"] = int(t.get("interval_hours", 0)) or None

        if t["description"] and t["interval_hours"]:
            valid.append(t)

    logger.info("Validated %d/%d extracted tasks", len(valid), len(raw_tasks))
    return valid
