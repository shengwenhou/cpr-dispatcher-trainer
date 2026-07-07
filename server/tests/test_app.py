"""FastAPI 課堂後端測試：health、hello envelope、建課→開場次（文字模式）的 WS 往返。

WS 骨架的 echo 行為已依定案改為真 handler，故原 echo 測試改為驗證真協定。
資料落地導向臨時目錄（CPR_DATA_ROOT），避免污染 repo。"""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("fastapi")

# 在 import app 前把資料根導向臨時目錄（app 於 import 時建立 SessionStore）。
os.environ.setdefault("CPR_DATA_ROOT", tempfile.mkdtemp(prefix="cpr_test_data_"))

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


def test_ws_hello_envelope():
    with client.websocket_connect("/ws/classroom") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert "payload" in hello
        assert hello["payload"]["locale"] == "zh-TW"


def test_ws_create_class_and_start_text_session():
    with client.websocket_connect("/ws/classroom") as ws:
        assert ws.receive_json()["type"] == "hello"

        ws.send_text('{"type":"create_class","payload":{"scenario":"adult","locale":"zh-TW"}}')
        created = ws.receive_json()
        assert created["type"] == "class_created"
        assert created["payload"]["class_id"]

        ws.send_text('{"type":"start_session","payload":{"student_alias":"測試員","mode":"text"}}')
        started = ws.receive_json()
        assert started["type"] == "session_started"
        session_id = started["session_id"]
        assert started["payload"]["state"] == "s0"

        # 開場動作即時串流：應收到 state_change 與 tts_play
        seen = set()
        for _ in range(40):
            msg = ws.receive_json()
            seen.add(msg["type"])
            if {"state_change", "tts_play"} <= seen:
                break
        assert "state_change" in seen
        assert "tts_play" in seen

        # 收尾：中止場次（停背景 tick_loop）
        ws.send_text(f'{{"type":"abort_session","session_id":"{session_id}","payload":{{}}}}')
