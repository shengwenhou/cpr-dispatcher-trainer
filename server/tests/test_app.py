"""FastAPI 骨架測試：health endpoint 與 WebSocket 骨架。"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from server.app import app  # noqa: E402


client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["locale"] == "zh-TW"
    assert data["scenario"] == "adult"
    assert data["script_loaded"] is True


def test_ws_skeleton_echo():
    with client.websocket_connect("/ws/session") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        ws.send_text('{"type":"text","text":"救護車"}')
        echo = ws.receive_json()
        assert echo["type"] == "echo"
