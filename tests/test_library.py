import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_library(client: AsyncClient):
    response = await client.get("/api/library")
    assert response.status_code == 200
    data = response.json()
    assert data["total_machines"] == 4
    assert data["total_tasks"] == 146
    assert data["total_intervals"] == 16


@pytest.mark.asyncio
async def test_get_interval_tasks_contiform_8hr(client: AsyncClient):
    response = await client.get("/api/library/CONTIFORM-C3-L3/8")
    assert response.status_code == 200
    data = response.json()
    assert data["task_count"] == 8
    assert data["machine_name"] == "Krones Contiform C3 SAN"
    assert data["interval_label"] == "8hr"
    # Verify task numbering: 10, 20, 30...
    task_nos = [t["task_no"] for t in data["tasks"]]
    assert task_nos == [10, 20, 30, 40, 50, 60, 70, 80]


@pytest.mark.asyncio
async def test_get_interval_tasks_bottle_coder_240hr(client: AsyncClient):
    response = await client.get("/api/library/BOTTLECODER-L3/240")
    assert response.status_code == 200
    data = response.json()
    assert data["task_count"] == 28


@pytest.mark.asyncio
async def test_task_has_required_fields(client: AsyncClient):
    response = await client.get("/api/library/CONTIFORM-C3-L3/120")
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    for task in tasks:
        assert "task_no" in task
        assert "area" in task
        assert "action" in task
        assert "description" in task
        assert "machine_state" in task
        assert task["machine_state"] in ("RUNNING", "STOPPED", "POWERED_OFF")
        assert "safety_flag" in task


@pytest.mark.asyncio
async def test_bottle_coder_240hr_has_part_numbers(client: AsyncClient):
    response = await client.get("/api/library/BOTTLECODER-L3/240")
    data = response.json()
    part_numbers = [t["part_number"] for t in data["tasks"] if t.get("part_number")]
    assert "L011362" in part_numbers
    assert "L011378" in part_numbers


@pytest.mark.asyncio
async def test_get_machines(client: AsyncClient):
    response = await client.get("/api/machines")
    assert response.status_code == 200
    machines = response.json()
    machine_ids = [m["machine_id"] for m in machines]
    assert "CONTIFORM-C3-L3" in machine_ids
    assert "BOTTLECODER-L3" in machine_ids
    assert "DEHUMIDIFIER-L3" in machine_ids
    assert "VARIOPAC-PRO-L3" in machine_ids


@pytest.mark.asyncio
async def test_120hr_contiform_has_three_machine_states(client: AsyncClient):
    """120hr PM must have RUNNING, STOPPED and POWERED_OFF tasks."""
    response = await client.get("/api/library/CONTIFORM-C3-L3/120")
    tasks = response.json()["tasks"]
    states = {t["machine_state"] for t in tasks}
    assert "RUNNING" in states
    assert "STOPPED" in states
    assert "POWERED_OFF" in states


@pytest.mark.asyncio
async def test_safety_tasks_present(client: AsyncClient):
    """Every interval should have at least one safety task."""
    response = await client.get("/api/library/BOTTLECODER-L3/240")
    tasks = response.json()["tasks"]
    safety_tasks = [t for t in tasks if t["safety_flag"]]
    assert len(safety_tasks) > 0
