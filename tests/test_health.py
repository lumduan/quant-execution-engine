from __future__ import annotations

from fastapi.testclient import TestClient
from src.quant_execution_engine.api.main import app

client = TestClient(app)


def test_health_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "quant-execution-engine"
    assert "version" in body
