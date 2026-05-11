import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_generate_pdf_success(client: AsyncClient):
    """Core test: generate a PM PDF for CON L3 Bottle Coder 240hr."""
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "BOTTLECODER-L3",
            "interval_hours": 240,
            "work_order": "TEST-WO-001",
            "technician_name": "Test Technician",
            "output_format": "pdf",
            "storage_target": "local",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["task_count"] == 28
    assert data["machine_id"] == "BOTTLECODER-L3"
    assert data["interval_hours"] == 240
    assert data["file_hash"]
    assert data["download_url"]


@pytest.mark.asyncio
async def test_generate_contiform_8hr(client: AsyncClient):
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "CONTIFORM-C3-L3",
            "interval_hours": 8,
            "work_order": "TEST-WO-002",
            "technician_name": "Ahmed Khan",
            "output_format": "pdf",
            "storage_target": "local",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["task_count"] == 8
    assert data["machine_name"] == "Krones Contiform C3 SAN"


@pytest.mark.asyncio
async def test_generate_variopac_120hr(client: AsyncClient):
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "VARIOPAC-PRO-L3",
            "interval_hours": 120,
            "work_order": "TEST-WO-003",
            "technician_name": "Test Tech",
            "output_format": "pdf",
        },
    )
    assert response.status_code == 201
    assert response.json()["task_count"] == 12


@pytest.mark.asyncio
async def test_generate_unknown_machine(client: AsyncClient):
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "MACHINE-DOES-NOT-EXIST",
            "interval_hours": 120,
            "work_order": "WO-X",
            "technician_name": "Test",
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generate_no_tasks(client: AsyncClient):
    """Returns 404 when no tasks exist for that interval."""
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "CONTIFORM-C3-L3",
            "interval_hours": 9999,
            "work_order": "WO-X",
            "technician_name": "Test",
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generate_docx_format(client: AsyncClient):
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "DEHUMIDIFIER-L3",
            "interval_hours": 8,
            "work_order": "WO-DH-001",
            "technician_name": "Test",
            "output_format": "docx",
            "storage_target": "local",
        },
    )
    assert response.status_code == 201
    assert response.json()["file_name"].endswith(".docx")


@pytest.mark.asyncio
async def test_generate_xlsx_format(client: AsyncClient):
    response = await client.post(
        "/api/generate",
        json={
            "machine_id": "DEHUMIDIFIER-L3",
            "interval_hours": 1500,
            "work_order": "WO-DH-002",
            "technician_name": "Test",
            "output_format": "xlsx",
            "storage_target": "local",
        },
    )
    assert response.status_code == 201
    assert response.json()["file_name"].endswith(".xlsx")


@pytest.mark.asyncio
async def test_generate_requires_auth():
    from httpx import ASGITransport, AsyncClient as AC
    from app.main import app
    transport = ASGITransport(app=app)
    async with AC(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/generate",
            json={"machine_id": "CONTIFORM-C3-L3", "interval_hours": 8,
                  "work_order": "WO-X", "technician_name": "Test"},
        )
    assert response.status_code == 401
