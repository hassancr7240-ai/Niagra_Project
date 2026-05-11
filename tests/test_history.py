import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_dashboard(client: AsyncClient):
    response = await client.get("/api/history/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert "stats" in data
    assert "overdue" in data
    assert "schedule" in data
    assert "recent_pms" in data
    assert data["stats"]["machines_covered"] == 4
    assert data["stats"]["total_tasks"] == 146


@pytest.mark.asyncio
async def test_get_history_empty(client: AsyncClient):
    response = await client.get("/api/history")
    assert response.status_code == 200
    data = response.json()
    assert "records" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_get_history_filter_by_machine(client: AsyncClient):
    response = await client.get("/api/history?machine_id=CONTIFORM-C3-L3")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_history_after_generate(client: AsyncClient):
    """Generate a PM then verify it appears in history."""
    gen = await client.post(
        "/api/generate",
        json={
            "machine_id": "CONTIFORM-C3-L3",
            "interval_hours": 8,
            "work_order": "HIST-TEST-001",
            "technician_name": "History Test",
            "output_format": "pdf",
            "storage_target": "local",
        },
    )
    assert gen.status_code == 201
    record_id = gen.json()["record_id"]

    hist = await client.get("/api/history")
    assert hist.status_code == 200
    record_ids = [r["record_id"] for r in hist.json()["records"]]
    assert record_id in record_ids


@pytest.mark.asyncio
async def test_get_pm_record_by_id(client: AsyncClient):
    gen = await client.post(
        "/api/generate",
        json={
            "machine_id": "BOTTLECODER-L3",
            "interval_hours": 8,
            "work_order": "HIST-TEST-002",
            "technician_name": "Test Tech",
            "output_format": "pdf",
            "storage_target": "local",
        },
    )
    record_id = gen.json()["record_id"]
    response = await client.get(f"/api/history/{record_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["record_id"] == record_id
    assert data["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_approve_pm_record(client: AsyncClient):
    gen = await client.post(
        "/api/generate",
        json={
            "machine_id": "VARIOPAC-PRO-L3",
            "interval_hours": 8,
            "work_order": "APPROVE-TEST-001",
            "technician_name": "Test Tech",
            "output_format": "pdf",
            "storage_target": "local",
        },
    )
    record_id = gen.json()["record_id"]

    approve = await client.post(
        f"/api/history/{record_id}/approve",
        json={"notes": "Verified by supervisor"},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_get_nonexistent_record(client: AsyncClient):
    response = await client.get("/api/history/nonexistent-id-12345")
    assert response.status_code == 404
