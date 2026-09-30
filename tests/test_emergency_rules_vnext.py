from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.hub.db import HubRepository
from services.hub.emergency_rules import evaluate_rule_expression, validate_rule_expression
from tests.test_hub_vnext import _client


def test_rule_validator_and_evaluator_support_fields_and_active_alarm_lists():
    valid, error = validate_rule_expression("('Low oil' in active_alarms) and RPM < 300", {"RPM", "active_alarms"})
    assert valid and error is None
    assert evaluate_rule_expression("('Low oil' in active_alarms) and RPM < 300", {"RPM": 120, "active_alarms": ["Low oil"]}) == (True, None)
    assert not validate_rule_expression("__import__('os')", {"active_alarms"})[0]
    assert not validate_rule_expression("Unknown > 1", {"RPM"})[0]
    assert validate_rule_expression(
        "'BUS Low Volt' in active_alarms",
        {"active_alarms"},
        list_fields={"active_alarms"},
        error_labels={"BUS Low Volt"},
    )[0]
    assert not validate_rule_expression(
        "'Wrong label' in Status",
        {"Status"},
        list_fields={"Status"},
        error_labels={"BUS Low Volt"},
    )[0]


def test_emergency_rule_transitions_persist_duration_and_keep_snapshot(tmp_path):
    repo = HubRepository(tmp_path / "hub.db")
    rule = repo.save_emergency_rule(name="Низкое давление", expression="Pressure < 10")
    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    assert repo.evaluate_emergency_rules("vm-1", start, {"Pressure": 5})[0]["state"] == "active"
    assert repo.evaluate_emergency_rules("vm-1", start + timedelta(seconds=2), {"Pressure": 4}) == []
    stop = repo.evaluate_emergency_rules("vm-1", start + timedelta(seconds=5), {"Pressure": 12})
    assert stop[0]["state"] == "inactive"
    event = repo.list_emergency_events(["vm-1"])[0]
    assert event["started_at"] == start.isoformat()
    assert event["ended_at"] == (start + timedelta(seconds=5)).isoformat()
    assert event["expression"] == rule["expression"]
    assert repo.update_emergency_rule(rule["id"], name="Давление", expression="Pressure < 12")["name"] == "Давление"
    assert repo.evaluate_emergency_rules("vm-1", start + timedelta(seconds=6), {"Pressure": 5})[0]["state"] == "active"
    assert repo.delete_emergency_rule(rule["id"])
    closed = repo.close_deleted_emergency_rule(rule["id"], start + timedelta(seconds=7))
    assert closed[0]["state"] == "inactive"
    assert not closed[0]["has_active"]
    assert repo.list_emergency_events(["vm-1"])[0]["ended_at"] == (start + timedelta(seconds=7)).isoformat()
    assert repo.list_emergency_rules() == []


def test_emergency_rule_api_requires_admin_and_csrf(tmp_path):
    with _client(tmp_path) as client:
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
        token = client.cookies.get("bb_csrf")
        created = client.post(
            "/api/v1/emergency-rules",
            json={"name": "Алерт прибора", "expression": "'Alarm' in active_alarms"},
            headers={"X-CSRF-Token": token},
        )
        assert created.status_code == 200
        assert created.json()["name"] == "Алерт прибора"
        invalid = client.post(
            "/api/v1/emergency-rules",
            json={"name": "Плохое правило", "expression": "__import__('os')"},
            headers={"X-CSRF-Token": token},
        )
        assert invalid.status_code == 422
        assert client.get("/alarms").status_code == 200
