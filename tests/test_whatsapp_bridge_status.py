"""Bounded, redacted WhatsApp bridge status contracts."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import dashboard as dashboard_route

APP_JS = Path(__file__).resolve().parents[1] / "api" / "static" / "js" / "app.js"


def test_bridge_status_reports_archive_counts_without_private_payloads() -> None:
    app = FastAPI()
    app.include_router(dashboard_route.router, prefix="/api")
    dashboard_route._bridge_last_seen.clear()

    client = TestClient(app)
    response = client.post(
        "/api/bridge/heartbeat",
        json={
            "id": "whatsapp-web",
            "groups_watched": 6,
            "archive_scan": {
                "enabled": True,
                "active": True,
                "running": False,
                "phase": "idle",
                "mode": "hydrated_cache_only",
                "last_started_at": "2026-09-01T08:00:00Z",
                "last_finished_at": "2026-09-01T08:00:01Z",
                "last_group_count": 6,
                "last_message_count": 42,
                "last_error_code": "HISTORICAL_PAGINATION_UNAVAILABLE",
                "last_pagination_available": False,
                "body": "must not be returned",
                "url": "https://private.example/job/1",
            },
        },
    )
    assert response.status_code == 200

    status = client.get("/api/bridge/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["connected"] is True
    assert payload["groups_watched"] == 6
    assert payload["archive_scan"]["mode"] == "hydrated_cache_only"
    assert payload["archive_scan"]["last_group_count"] == 6
    assert payload["archive_scan"]["last_message_count"] == 42
    assert payload["archive_scan"]["last_error_code"] == "HISTORICAL_PAGINATION_UNAVAILABLE"
    assert payload["archive_scan"]["last_pagination_available"] is False
    assert "body" not in payload["archive_scan"]
    assert "url" not in payload["archive_scan"]


def test_dashboard_labels_archive_status_as_cache_only() -> None:
    javascript = APP_JS.read_text(encoding="utf-8")
    assert "data.groups_watched" in javascript
    assert "hydrated_cache_only" in javascript
    assert "last_pagination_available" in javascript
