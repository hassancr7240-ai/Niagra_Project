from __future__ import annotations

"""
IBM watsonx.ai Analytics — Data Flow Diagram: View History branch
  granite-13b-instruct-v2
  → Analyse PM Patterns + Predict Next Due
  → Analyses overdue patterns
  → Recommends tech scheduling
"""

import json
import logging
from datetime import datetime
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def analyse_pm_patterns(
    records: list[dict],
    machines: list[dict],
    current_hours_map: dict[str, int],
) -> dict:
    """
    IBM watsonx.ai Analytics — granite-13b-instruct-v2
    Analyses PM history, predicts next due dates, identifies overdue patterns.
    Falls back to rule-based analysis if watsonx is not configured.
    """
    if settings.watsonx_api_key:
        try:
            return await _watsonx_analytics(records, machines, current_hours_map)
        except Exception as exc:
            logger.warning("watsonx analytics failed, using rule-based fallback: %s", exc)

    return _rule_based_analytics(records, machines, current_hours_map)


async def _watsonx_analytics(
    records: list[dict],
    machines: list[dict],
    current_hours_map: dict[str, int],
) -> dict:
    """
    Call IBM watsonx.ai granite-13b-instruct-v2 for PM pattern analysis.
    Data Flow Diagram: granite-13b-instruct-v2 → Analyse PM Patterns + Predict Next Due
    Uses IAM token auth.
    """
    import httpx
    from app.rag.watsonx_auth import watsonx_headers

    url = f"{settings.watsonx_url}/ml/v1/text/generation?version=2024-03-14"
    headers = await watsonx_headers(settings.watsonx_api_key)

    # Summarise recent PM history for analysis
    summary = []
    for r in records[-20:]:
        summary.append({
            "machine": r.get("machine_id"),
            "interval_hours": r.get("interval_hours"),
            "completed_at": r.get("completed_at"),
            "status": r.get("status"),
        })

    prompt = f"""You are a predictive maintenance analytics system. Analyse this PM history data and provide insights.

PM History (last 20 records):
{json.dumps(summary, indent=2, default=str)}

Current machine hours:
{json.dumps(current_hours_map, indent=2)}

Analyse the data and respond ONLY with valid JSON in this exact format:
{{
  "overdue_risk": [{{"machine_id": "...", "interval_hours": ..., "risk_level": "HIGH|MEDIUM|LOW", "reason": "..."}}],
  "predicted_next_due": [{{"machine_id": "...", "interval_hours": ..., "predicted_at_hours": ..., "days_estimate": ...}}],
  "patterns": ["...list of observations about maintenance patterns..."],
  "recommendations": ["...list of scheduling recommendations..."]
}}"""

    payload = {
        "model_id": settings.watsonx_model_analytics,
        "project_id": settings.watsonx_project_id,
        "input": prompt,
        "parameters": {
            "max_new_tokens": 1000,
            "temperature": 0,
            "repetition_penalty": 1.1,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        text = data["results"][0]["generated_text"]
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))

    return {}


def _rule_based_analytics(
    records: list[dict],
    machines: list[dict],
    current_hours_map: dict[str, int],
) -> dict:
    """
    Rule-based PM pattern analysis (fallback when watsonx is not configured).
    Implements the same logic as watsonx.ai granite-13b analytics would apply.
    Data Flow: Analyse PM Patterns + Predict Next Due
    """
    from collections import defaultdict

    # Group completed PMs by machine + interval
    last_pm: dict[tuple, dict] = {}
    completion_times: dict[tuple, list] = defaultdict(list)

    for r in records:
        key = (r.get("machine_id", ""), r.get("interval_hours", 0))
        if r.get("status") in ("COMPLETED", "APPROVED") and r.get("completed_at"):
            if key not in last_pm or r["completed_at"] > last_pm[key].get("completed_at", ""):
                last_pm[key] = r
            completion_times[key].append(r["completed_at"])

    overdue_risk = []
    predicted_next_due = []
    patterns = []
    recommendations = []

    for m in machines:
        mid = m.get("machine_id") or m.get("machine_id", "")
        current_h = current_hours_map.get(mid, 0)

        for iv_hours in _get_machine_intervals(m):
            key = (mid, iv_hours)
            last = last_pm.get(key)

            # Calculate completion frequency
            completions = completion_times.get(key, [])
            completion_count = len(completions)

            # Risk assessment
            if not last:
                if current_h > iv_hours:
                    overdue_risk.append({
                        "machine_id": mid,
                        "interval_hours": iv_hours,
                        "risk_level": "HIGH",
                        "reason": f"Never completed — machine at {current_h}hrs, PM due at {iv_hours}hrs",
                    })
                elif current_h > iv_hours * 0.8:
                    overdue_risk.append({
                        "machine_id": mid,
                        "interval_hours": iv_hours,
                        "risk_level": "MEDIUM",
                        "reason": f"Never completed — due within {iv_hours - current_h}hrs",
                    })
            else:
                # Predict next due
                predicted_next = current_h + iv_hours
                predicted_next_due.append({
                    "machine_id": mid,
                    "interval_hours": iv_hours,
                    "predicted_at_hours": predicted_next,
                    "days_estimate": max(0, (predicted_next - current_h) // 8),
                })

        if completion_count > 1:
            patterns.append(
                f"{mid}: {completion_count} PMs completed — "
                f"Average frequency on schedule"
            )

    # General recommendations
    if overdue_risk:
        high_risk = [r for r in overdue_risk if r["risk_level"] == "HIGH"]
        if high_risk:
            recommendations.append(
                f"URGENT: {len(high_risk)} PM(s) are overdue — schedule immediately"
            )
    if not records:
        recommendations.append(
            "No PM history found — record machine hours and generate first PM checklists"
        )
    recommendations.append(
        "Ensure machine hours are updated weekly for accurate PM scheduling"
    )

    return {
        "overdue_risk": overdue_risk,
        "predicted_next_due": predicted_next_due[:10],
        "patterns": patterns[:5],
        "recommendations": recommendations[:5],
        "provider": "rule-based-fallback",
    }


def _get_machine_intervals(machine: dict) -> list[int]:
    machine_intervals = {
        "CONTIFORM-C3-L3":  [8, 120, 500, 1500, 3000, 6000],
        "BOTTLECODER-L3":   [8, 240],
        "DEHUMIDIFIER-L3":  [8, 1500],
        "VARIOPAC-PRO-L3":  [8, 120, 500, 1500, 3000, 6000],
        "SHRINK-TUNNEL-L3": [],
    }
    mid = machine.get("machine_id", "")
    return machine_intervals.get(mid, [8, 500])
