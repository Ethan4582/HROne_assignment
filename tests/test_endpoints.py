from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app.main import app, db, datetime_to_epoch_ms


@pytest.fixture(scope="module")
def client():
    # Run tests using TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def setup_db():
    # Clean and reseed before tests
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    # Seed 2 employees
    e1 = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
    }
    e2 = {
        "emp_code": "EMP0002",
        "name": "Vikram Shah",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13",
        "created_at": datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
    }
    e3 = {
        "emp_code": "EMP0005",
        "name": "Meera Nair",
        "email": "meera@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10",
        "created_at": datetime(2026, 3, 10, 4, 30, tzinfo=timezone.utc),
    }
    db.employees.insert_many([e1, e2, e3])


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee(client):
    new_emp = {
        "emp_code": "EMP0010",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-06-01"
    }
    res = client.post("/employees", json=new_emp)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0010"
    assert isinstance(data["created_at"], int)

    # Duplicate emp_code returns 409
    res_dup = client.post("/employees", json=new_emp)
    assert res_dup.status_code == 409

    # Identical shift start and end returns 422
    bad_shift = dict(new_emp, emp_code="EMP0011", email="test2@example.com", shift_start="09:00", shift_end="09:00")
    res_bad = client.post("/employees", json=bad_shift)
    assert res_bad.status_code == 422


def test_list_employees(client):
    res = client.get("/employees?department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0002"


def test_punch_in_and_out(client):
    # Non-existent employee returns 404
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res_404.status_code == 404

    # Valid punch-in (2026-07-06 09:28:00 IST -> 03:58:00 UTC)
    # 2026-07-06 03:58:00 UTC = 1783310280000 ms
    ms_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in, "status": "PRESENT"})
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["date"] == "2026-07-06"
    assert data_in["late_minutes"] == 0
    assert data_in["punch_in"] == ms_in
    assert data_in["punch_out"] is None

    # Duplicate punch-in for same date returns 409
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in})
    assert res_dup.status_code == 409

    # Punch-out (2026-07-06 18:35:00 IST -> 13:05:00 UTC = 9h7m = 9.12h, 5m OT < 30 -> 0 OT)
    ms_out = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["work_hours"] == 9.12
    assert data_out["half_day"] is False
    assert data_out["overtime_minutes"] == 0
    assert data_out["punch_out"] == ms_out

    # Second punch-out on same record returns 409
    res_out_again = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res_out_again.status_code == 409


def test_punch_in_overnight(client):
    # EMP0005 has shift 22:00 - 06:00
    # Punch in at 01:15 IST on 2026-07-07 (19:45 UTC on 2026-07-06)
    # Belongs to 2026-07-06 shift!
    ms_in = int(datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms_in})
    assert res_in.status_code == 201
    data = res_in.json()
    assert data["date"] == "2026-07-06"
    # Late by 3h15m = 195 minutes
    assert data["late_minutes"] == 195


def test_list_attendance(client):
    # Insert attendance record
    ms_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in})

    res = client.get("/attendance?emp_code=EMP0001")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert "id" not in data["items"][0]

    # date_from > date_to returns 422
    res_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_bad.status_code == 422


def test_regularize_attendance(client):
    ms_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in})

    # Setting LEAVE with punch_in/punch_out returns 422
    res_bad = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "LEAVE", "punch_in": ms_in, "reason": "Approved sick leave", "regularized_by": "HR001"}
    )
    assert res_bad.status_code == 422

    # Setting LEAVE properly clears punch times
    res_leave = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "LEAVE", "reason": "Approved medical leave", "regularized_by": "HR001"}
    )
    assert res_leave.status_code == 200
    data_leave = res_leave.json()
    assert data_leave["status"] == "LEAVE"
    assert data_leave["punch_in"] is None
    assert data_leave["punch_out"] is None
    assert len(data_leave["history"]) == 1
    assert data_leave["history"][0]["by"] == "HR001"

    # Sending identical request changes nothing -> 422
    res_no_change = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "LEAVE", "reason": "Duplicate update", "regularized_by": "HR001"}
    )
    assert res_no_change.status_code == 422


def test_analytics_endpoints(client):
    # Seed known records
    ms_in_1 = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ms_out_1 = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in_1})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out_1})

    # Test monthly analytics
    res_month = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_month.status_code == 200
    m_data = res_month.json()
    assert m_data["working_days"] == 23
    assert m_data["present_days"] == 1.0

    # Test department summary
    res_dept = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res_dept.status_code == 200
    d_data = res_dept.json()
    assert len(d_data["items"]) == 1
    assert d_data["items"][0]["department"] == "Engineering"
    assert d_data["items"][0]["headcount"] == 2  # EMP0001 and EMP0002

    # Test leaderboard late
    res_lead = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res_lead.status_code == 200
    assert "items" in res_lead.json()

    # Test department trend
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res_trend.status_code == 200
    t_data = res_trend.json()
    assert len(t_data["items"]) == 5  # Mon-Fri
    assert t_data["items"][0]["date"] == "2026-07-06"
    assert t_data["items"][0]["present_count"] == 1.0


def test_admin_explain(client):
    endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    for ep in endpoints:
        res = client.get(f"/admin/explain/{ep}")
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        assert "executionStats" in data["explain"] or "stages" in data["explain"]
