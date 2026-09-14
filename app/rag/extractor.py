from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Valid values ─────────────────────────────────────────────────────────────

_VALID_ACTIONS = {
    "LOCKOUT", "CHECK", "LISTEN", "CLEAN", "REPLACE",
    "LUBRICATE", "VERIFY", "TEST", "CONFIRM", "INSPECT",
    "ADJUST", "CALIBRATE", "TIGHTEN", "DRAIN", "REFILL",
    "MEASURE", "RECORD", "GREASE", "TORQUE", "RESET",
}
_VALID_STATES = {"RUNNING", "STOPPED", "POWERED_OFF"}
_VALID_INTERVALS = {
    8, 16, 24, 40, 80, 100, 120, 160, 240, 500, 750, 1000, 1500, 2000,
    2500, 3000, 4000, 5000, 6000, 8000, 10000, 12000, 15000, 18000,
    20000, 24000, 30000, 36000, 42000, 45000, 48000, 60000, 72000,
}

# ── Few-shot examples so any model understands the exact format ───────────────

_FEW_SHOT = """CORRECT OUTPUT EXAMPLES:
[
  {"task_no":10,"area":"LUBRICATION","action":"LUBRICATE","description":"LUBRICATE MAIN DRIVE CHAIN WITH FOOD-GRADE OIL ISO VG 68","machine_state":"STOPPED","safety_flag":false,"part_number":null,"interval_hours":500},
  {"task_no":20,"area":"FILTER","action":"REPLACE","description":"REPLACE HYDRAULIC OIL FILTER ELEMENT — PART NO. HF-2241","machine_state":"POWERED_OFF","safety_flag":true,"part_number":"HF-2241","interval_hours":2000},
  {"task_no":30,"area":"CONVEYOR","action":"CHECK","description":"CHECK CONVEYOR BELT TENSION AND ALIGNMENT — ADJUST IF DEVIATION EXCEEDS 5MM","machine_state":"STOPPED","safety_flag":false,"part_number":null,"interval_hours":1000},
  {"task_no":40,"area":"ELECTRICAL","action":"INSPECT","description":"INSPECT ALL CABLE CONNECTIONS AND TERMINAL BLOCKS FOR CORROSION OR LOOSENESS","machine_state":"POWERED_OFF","safety_flag":true,"part_number":null,"interval_hours":6000},
  {"task_no":50,"area":"SENSORS","action":"VERIFY","description":"VERIFY PROXIMITY SENSOR DETECTION RANGE IS WITHIN 2-4MM OF TARGET","machine_state":"RUNNING","safety_flag":false,"part_number":null,"interval_hours":1000},
  {"task_no":60,"area":"PNEUMATIC","action":"CHECK","description":"CHECK AIR PRESSURE AT MAIN MANIFOLD — MUST BE 6.0-6.5 BAR","machine_state":"RUNNING","safety_flag":false,"part_number":null,"interval_hours":500},
  {"task_no":70,"area":"GEARBOX","action":"REPLACE","description":"REPLACE GEARBOX OIL — USE MOBIL SHC 636 SYNTHETIC GEAR OIL","machine_state":"POWERED_OFF","safety_flag":true,"part_number":null,"interval_hours":6000}
]"""

_SYSTEM_PROMPT = f"""You are an expert industrial maintenance engineer. You will be given multiple sections from a machine manual separated by "--- SECTION BREAK ---". Extract EVERY Preventive Maintenance (PM) task found ANYWHERE in ALL sections.

CRITICAL: This document may contain 50-200+ maintenance tasks. You MUST extract ALL of them. Do NOT stop early.

EXTRACTION RULES:
1. Scan ALL sections — every task in every section must appear in the output.
2. Do NOT invent tasks not in the text. Do NOT skip tasks that are in the text.
3. interval_hours: use EXACT number from text. Convert: 8hr=1 day, 120hr=1 week, 240hr=2 weeks, 500hr=1 month, 1500hr=3 months, 3000hr=6 months, 6000hr=1 year, 12000hr=2 years, 42000hr=7 years. Default 500 if not stated.
4. machine_state: RUNNING=task done while machine produces | STOPPED=machine halted but powered | POWERED_OFF=full LOTO/lockout required.
5. safety_flag: true if LOTO, lockout, tagout, de-energise, or entering danger zone required.
6. area: CAPS category — CONVEYOR, DRIVE, ELECTRICAL, FILTER, LUBRICATION, SAFETY, SENSORS, PNEUMATIC, HYDRAULIC, COOLING, SEALING, FRAME, GEARBOX, PUMP, VALVE, BEARING, BELT, CHAIN, CLUTCH, MOTOR, or GENERAL.
7. action: exactly one of LOCKOUT / CHECK / LISTEN / CLEAN / REPLACE / LUBRICATE / VERIFY / TEST / CONFIRM / INSPECT / ADJUST / CALIBRATE / TIGHTEN / DRAIN / REFILL / MEASURE / RECORD / GREASE / TORQUE / RESET.
8. description: FULL instruction in CAPITAL LETTERS — include measurements, part numbers, lubricant types, torque specs.
9. part_number: exact part number string if stated in text, otherwise null.

{_FEW_SHOT}

OUTPUT ONLY a valid JSON array — no explanation, no markdown, no code fences. Start with [ and end with ]. Include EVERY task from ALL sections above."""

# ── Extraction configuration ──────────────────────────────────────────────────

_CHARS_PER_CHUNK       = 1500  # chars per chunk
_BATCH_SIZE_OLLAMA     = 5     # 3B model: 5 × 1500 chars = 7500 chars fits in 8K context
_BATCH_SIZE_IBM        = 30    # 70B model: 128K context — 30 × 1500 = 45K chars, extracts 50-80 tasks/batch
_PER_CALL_TIMEOUT      = 55    # seconds per Ollama call
_IBM_CALL_TIMEOUT      = 180   # seconds per IBM call — 4096 output tokens needs up to 2.5 min
_IBM_FALLBACK_TIMEOUT  = 150   # seconds per IBM batch — 30 chunks × potential 80 tasks needs time
_NUM_PREDICT           = 1024  # Ollama: 1024 tokens
_EXTRACTION_BUDGET_S   = 1200  # 20-min budget — 6 batches × 30 chunks = all 180 chunks covered


# ── Public entry point ────────────────────────────────────────────────────────

async def extract_tasks_from_chunks(
    chunks: list[dict],
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]] = None,
) -> list[dict]:
    """Extract PM tasks from ALL chunks — complete document coverage.

    Uses IBM batch size (8 chunks/call) when watsonx is configured — the 70B model
    handles 8 × 1500 chars comfortably within its 128K context window, cutting call
    count by ~3x vs Ollama's 3-chunk batches.
    All batches are sequential (Ollama single-threaded on ACI CPU).
    Results are merged and deduplicated.
    Stops early and returns partial results if _EXTRACTION_BUDGET_S is exceeded.
    """
    import time

    if not chunks:
        return []

    use_ibm = bool(settings.watsonx_api_key and settings.watsonx_project_id)
    batch_size = _BATCH_SIZE_IBM if use_ibm else _BATCH_SIZE_OLLAMA
    logger.info("[extractor] Using %s batch size=%d for %d chunks",
                "IBM" if use_ibm else "Ollama", batch_size, len(chunks))

    all_tasks: list[dict] = []
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    deadline = time.monotonic() + _EXTRACTION_BUDGET_S

    for batch_idx in range(total_batches):
        if time.monotonic() > deadline:
            logger.warning(
                "[extractor] Budget %ds exceeded at batch %d/%d — returning %d partial tasks",
                _EXTRACTION_BUDGET_S, batch_idx + 1, total_batches, len(all_tasks),
            )
            break

        batch = chunks[batch_idx * batch_size: (batch_idx + 1) * batch_size]
        combined = "\n\n--- SECTION BREAK ---\n\n".join(
            c["text"][:_CHARS_PER_CHUNK] for c in batch
        )
        logger.info(
            "[extractor] Batch %d/%d — %d chunks, %d chars",
            batch_idx + 1, total_batches, len(batch), len(combined),
        )
        batch_tasks = await _extract_with_fallback(combined, manufacturer, model, interval_hints)
        logger.info(
            "[extractor] Batch %d → %d tasks (total so far: %d)",
            batch_idx + 1, len(batch_tasks), len(all_tasks) + len(batch_tasks),
        )
        all_tasks.extend(batch_tasks)

    final = _deduplicate(all_tasks)
    logger.info("[extractor] Final: %d unique tasks from %d chunks across %d batches (batch_size=%d)",
                len(final), len(chunks), total_batches, batch_size)
    return final


# ── Primary + fallback dispatcher ────────────────────────────────────────────

async def _extract_with_fallback(
    text: str,
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]],
) -> list[dict]:
    """IBM watsonx only — no Ollama fallback. Returns [] and logs clearly if IBM is unavailable."""
    if settings.watsonx_api_key and settings.watsonx_project_id:
        try:
            result = await asyncio.wait_for(
                _extract_watsonx(text, manufacturer, model, interval_hints),
                timeout=_IBM_FALLBACK_TIMEOUT,
            )
            if result:
                return result
            logger.error("[extractor] IBM watsonx returned 0 tasks — key may be disabled or WML plan expired")
            return []
        except asyncio.TimeoutError:
            logger.error("[extractor] IBM watsonx timed out after %ds — WML service not responding", _IBM_FALLBACK_TIMEOUT)
            return []
        except Exception as exc:
            logger.error("[extractor] IBM watsonx FAILED: %s", exc)
            return []
    logger.error("[extractor] IBM credentials not configured — set WATSONX_API_KEY and WATSONX_PROJECT_ID")
    return []


# ── IBM watsonx.ai ────────────────────────────────────────────────────────────

async def _extract_watsonx(
    text: str,
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]],
) -> list[dict]:
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Machine: {manufacturer} {model or ''}\n"
        f"Known intervals (hours): {interval_hints or 'detect from text'}\n\n"
        f"Manual text:\n{text}\n\n"
        "JSON array:"
    )
    payload = {
        "model_id": settings.watsonx_model_generation,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {"max_new_tokens": 4096, "temperature": 0, "repetition_penalty": 1.1},
    }
    try:
        headers = await watsonx_headers(settings.watsonx_api_key)
        async with httpx.AsyncClient(timeout=_IBM_CALL_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text_out = resp.json()["results"][0]["generated_text"]
            logger.debug("[extractor] IBM raw output (%.200s)", text_out.replace('\n', '↵'))
            parsed = _extract_json_array(text_out)
            validated = _validate_tasks(json.loads(parsed))
            logger.info("[extractor] watsonx → %d tasks from %d chars", len(validated), len(text))
            return validated
    except Exception as exc:
        logger.error("[extractor] watsonx failed: %s", exc)
        return []


# ── Ollama local LLM ──────────────────────────────────────────────────────────

async def _extract_ollama(
    text: str,
    manufacturer: str,
    model: Optional[str],
    interval_hints: Optional[list[int]],
) -> list[dict]:
    if not settings.ollama_url:
        logger.error("[extractor] Ollama URL not configured")
        return []

    import httpx

    url = f"{settings.ollama_url}/api/generate"
    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Machine: {manufacturer} {model or ''}\n"
        f"Known intervals (hours): {interval_hints or 'detect from text'}\n\n"
        f"Manual text:\n{text}\n\n"
        "OUTPUT ONLY A JSON ARRAY. No explanation. No markdown. No code fences. Start with [ and end with ]."
    )
    def _sync_call() -> str:
        """Synchronous httpx call — runs in a thread so asyncio.wait_for cancels it reliably."""
        import httpx as _httpx
        with _httpx.Client(timeout=_httpx.Timeout(_PER_CALL_TIMEOUT, connect=5.0)) as c:
            r = c.post(url, json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "num_predict": _NUM_PREDICT},
            })
            r.raise_for_status()
            return r.json().get("response", "")

    try:
        text_out = await asyncio.wait_for(
            asyncio.to_thread(_sync_call),
            timeout=_PER_CALL_TIMEOUT,
        )
        # Strip markdown code fences small models sometimes add
        text_out = re.sub(r"```(?:json)?\s*|\s*```", "", text_out).strip()
        try:
            tasks = json.loads(_extract_json_array(text_out))
        except json.JSONDecodeError as je:
            logger.error("[extractor] Ollama JSON parse failed (%s) — raw: %.300s", je, text_out)
            return []
        validated = _validate_tasks(tasks)
        logger.info("[extractor] Ollama → %d tasks from %d chars", len(validated), len(text))
        return validated
    except asyncio.TimeoutError:
        logger.warning("[extractor] Ollama timed out after %ds — skipping batch", _PER_CALL_TIMEOUT)
        return []
    except Exception as exc:
        logger.error("[extractor] Ollama error: %s", exc)
        return []


# ── Deduplication ─────────────────────────────────────────────────────────────

def _deduplicate(tasks: list[dict]) -> list[dict]:
    """Remove duplicate tasks across batches.
    Two tasks are duplicates if their description prefix (80 chars) and interval match.
    Renumbers task_no sequentially after deduplication.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for t in tasks:
        key = (t.get("description", "")[:80].upper(), int(t.get("interval_hours", 0)))
        if key not in seen:
            seen.add(key)
            unique.append(t)
    for i, t in enumerate(unique):
        t["task_no"] = (i + 1) * 10
    logger.info("[extractor] Deduplication: %d → %d unique tasks", len(tasks), len(unique))
    return unique


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json_array(text: str) -> str:
    """Pull the first complete [...] JSON array out of a model response.
    Uses bracket counting so greedy regex doesn't capture multiple arrays.
    Falls back to recovering complete objects if the array is truncated.
    """
    # Strip markdown code fences (IBM sometimes wraps output in ```json ... ```)
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()

    start = text.find("[")
    if start == -1:
        objects = re.findall(r'\{[^{}]*\}', text)
        if objects:
            logger.warning("[extractor] No array found — recovered %d objects", len(objects))
            return "[" + ",".join(objects) + "]"
        return "[]"

    # Walk forward with bracket + string tracking to find the first COMPLETE array
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    # Unclosed array — recover completed objects
    fragment = text[start:]
    objects = re.findall(r'\{[^{}]*\}', fragment)
    if objects:
        logger.warning("[extractor] JSON truncated — recovered %d objects from partial output", len(objects))
        return "[" + ",".join(objects) + "]"
    return "[]"


def _snap_interval(raw: int) -> int:
    return min(_VALID_INTERVALS, key=lambda v: abs(v - raw))


def _validate_tasks(raw_tasks: list) -> list[dict]:
    """Validate and auto-fix extracted tasks. Drops tasks with empty descriptions."""
    valid: list[dict] = []
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            continue

        t["task_no"] = int(t.get("task_no") or (i + 1) * 10)
        t["area"]    = str(t.get("area", "GENERAL")).upper()[:64]

        action = str(t.get("action", "CHECK")).upper()
        t["action"] = action if action in _VALID_ACTIONS else "CHECK"

        desc = str(t.get("description", "")).strip().upper()
        t["description"] = desc or f"{t['action']} {t['area']}"

        state = str(t.get("machine_state", "")).upper()
        if state not in _VALID_STATES:
            desc_low = t["description"].lower()
            if any(k in desc_low for k in ("loto", "lockout", "tagout", "de-energi", "powered off")):
                state = "POWERED_OFF"
            elif any(k in desc_low for k in ("listen", "monitor", "visual", "observe",
                                              "temperature", "vibration", "noise",
                                              "while running", "during operation")):
                state = "RUNNING"
            else:
                state = "STOPPED"
        t["machine_state"] = state
        t["safety_flag"]   = bool(t.get("safety_flag", False))
        t["part_number"]   = t.get("part_number") or None

        raw_iv = int(t.get("interval_hours") or 0)
        if raw_iv <= 0:
            t["interval_hours"] = 500
        elif raw_iv not in _VALID_INTERVALS:
            t["interval_hours"] = _snap_interval(raw_iv)
        else:
            t["interval_hours"] = raw_iv

        if t["description"]:
            valid.append(t)

    logger.info("[extractor] Validated %d/%d tasks", len(valid), len(raw_tasks))
    return valid
