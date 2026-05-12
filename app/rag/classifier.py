from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import get_settings

settings = get_settings()


@dataclass
class ClassificationResult:
    manufacturer: str          # e.g. "KRONES", "EISBAR", "THIRD_PARTY"
    model: Optional[str]
    machine_type: str          # KRONES | THIRD_PARTY
    confidence: float          # 0.0 – 1.0
    method: str                # "keyword" | "ai"
    detected_chapters: list[int]


_KRONES_KEYWORDS = [
    "krones", "contiform", "variopac", "linajet", "modulfill",
    "pressant", "innoket", "linaglide", "lineaglide", "monotec",
]
_EISBAR_KEYWORDS = ["eisbär", "eisbar", "das-e", "das e", "trockentechnik", "dehumidifier das"]
_MAINTENANCE_KEYWORDS = [
    "maintenance", "wartung", "inspection", "service", "lubrication",
    "cleaning", "replace", "check", "filter change",
]


async def classify_manual(pdf_path: Path, sample_text: str) -> ClassificationResult:
    """
    Two-stage classification:
    1. Fast keyword scan (no API call)
    2. AI fallback (watsonx granite-13b-instruct-v2 or OpenAI) if confidence is low
    """
    result = _keyword_classify(sample_text)
    if result.confidence >= 0.8:
        return result

    try:
        if settings.ai_provider == "watsonx" and settings.watsonx_api_key:
            ai_result = await _ai_classify_watsonx(sample_text)
        else:
            ai_result = await _ai_classify_openai(sample_text)
        if ai_result.confidence > result.confidence:
            return ai_result
    except Exception:
        pass

    return result


def _keyword_classify(text: str) -> ClassificationResult:
    text_lower = text.lower()

    # Krones
    krones_matches = sum(1 for kw in _KRONES_KEYWORDS if kw in text_lower)
    if krones_matches >= 2:
        model = _detect_krones_model(text_lower)
        chapters = [12] if "contiform" in text_lower else [11, 12, 13] if "variopac" in text_lower else [12]
        return ClassificationResult(
            manufacturer="KRONES",
            model=model,
            machine_type="KRONES",
            confidence=min(0.95, 0.5 + krones_matches * 0.1),
            method="keyword",
            detected_chapters=chapters,
        )

    # eisbär
    eisbar_matches = sum(1 for kw in _EISBAR_KEYWORDS if kw in text_lower)
    if eisbar_matches >= 1:
        return ClassificationResult(
            manufacturer="EISBAR",
            model="DAS",
            machine_type="THIRD_PARTY",
            confidence=0.85,
            method="keyword",
            detected_chapters=[9],
        )

    # Try to detect chapter with maintenance content
    chapters = _detect_maintenance_chapters(text_lower)
    return ClassificationResult(
        manufacturer="THIRD_PARTY",
        model=None,
        machine_type="THIRD_PARTY",
        confidence=0.5,
        method="keyword",
        detected_chapters=chapters,
    )


def _detect_krones_model(text_lower: str) -> Optional[str]:
    models = {
        "contiform c3": "Contiform C3 SAN",
        "contiform": "Contiform",
        "variopac pro": "Variopac Pro FS",
        "variopac": "Variopac",
    }
    for kw, model in models.items():
        if kw in text_lower:
            return model
    return "Krones Machine"


def _detect_maintenance_chapters(text_lower: str) -> list[int]:
    chapters = []
    pattern = r"(?:chapter|section|kapitel)\s+(\d+)[\s\S]{0,100}(?:maintenance|service|inspection|wartung)"
    for m in re.finditer(pattern, text_lower):
        ch = int(m.group(1))
        if ch not in chapters:
            chapters.append(ch)
    return chapters[:3]


async def _ai_classify_openai(sample_text: str) -> ClassificationResult:
    from openai import AsyncOpenAI
    import json

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    truncated = sample_text[:4000]

    prompt = f"""You are a technical document classifier for industrial machinery manuals.

Analyse the following text excerpt from a machine operating manual and identify:
1. Manufacturer name (e.g. Krones, eisbär, ABB, etc.)
2. Machine model (if identifiable)
3. Chapter numbers that contain maintenance/service content

Return ONLY valid JSON in this exact format:
{{"manufacturer": "...", "model": "...", "maintenance_chapters": [11, 12], "machine_type": "KRONES|THIRD_PARTY"}}

Text:
{truncated}"""

    response = await client.chat.completions.create(
        model=settings.openai_model_classification,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=200,
    )

    content = response.choices[0].message.content or "{}"
    data = json.loads(content)

    mfr = data.get("manufacturer", "THIRD_PARTY").upper()
    mtype = "KRONES" if "KRONES" in mfr else "THIRD_PARTY"

    return ClassificationResult(
        manufacturer=mfr,
        model=data.get("model"),
        machine_type=mtype,
        confidence=0.85,
        method="ai",
        detected_chapters=data.get("maintenance_chapters", []),
    )


async def _ai_classify_watsonx(sample_text: str) -> ClassificationResult:
    """
    IBM watsonx.ai granite-13b-instruct-v2 classifier.
    Data Flow Diagram: Classification Model → detect machine type.
    """
    import httpx
    import json
    from app.rag.watsonx_auth import watsonx_headers

    truncated = sample_text[:3000]
    prompt = f"""You are a technical document classifier for industrial machinery manuals.

Analyse the following text excerpt and identify:
1. Manufacturer name (e.g. Krones, eisbär, ABB)
2. Machine model (if identifiable)
3. Chapter numbers containing maintenance/service content

Return ONLY valid JSON:
{{"manufacturer": "...", "model": "...", "maintenance_chapters": [12], "machine_type": "KRONES|THIRD_PARTY"}}

Text:
{truncated}

JSON:"""

    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    payload = {
        "model_id": settings.watsonx_model_classification,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {"max_new_tokens": 200, "temperature": 0},
    }

    try:
        headers = await watsonx_headers(settings.watsonx_api_key)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text_out = data["results"][0]["generated_text"]
            import re
            m = re.search(r"\{[\s\S]*?\}", text_out)
            if not m:
                raise ValueError("No JSON in watsonx classification response")
            parsed = json.loads(m.group(0))
            mfr = parsed.get("manufacturer", "THIRD_PARTY").upper()
            mtype = "KRONES" if "KRONES" in mfr else "THIRD_PARTY"
            return ClassificationResult(
                manufacturer=mfr,
                model=parsed.get("model"),
                machine_type=mtype,
                confidence=0.85,
                method="watsonx",
                detected_chapters=parsed.get("maintenance_chapters", []),
            )
    except Exception as exc:
        logger.error("watsonx classification failed: %s", exc)
        raise
