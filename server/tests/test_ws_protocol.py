"""WS 協定：envelope 編解碼 round-trip 與 error/degraded 的 message_key 規則。"""
from __future__ import annotations

import json

from server import ws_protocol as wsp


def test_envelope_roundtrip_with_session_id():
    env = wsp.make(wsp.MsgType.TRANSCRIPT, payload={"kind": "final", "text": "救護車"}, session_id="s1")
    back = wsp.Envelope.from_json(env.to_json())
    assert back.type == "transcript"
    assert back.session_id == "s1"
    assert back.payload == {"kind": "final", "text": "救護車"}


def test_envelope_omits_none_session_id():
    env = wsp.make(wsp.MsgType.CREATE_CLASS, payload={"scenario": "adult"})
    d = env.to_dict()
    assert "session_id" not in d  # None 時省略（session_id 可選）
    back = wsp.Envelope.from_dict(d)
    assert back.session_id is None
    assert back.type == "create_class"


def test_envelope_tolerates_missing_payload():
    back = wsp.Envelope.from_dict({"type": "end_class"})
    assert back.payload == {}
    assert back.type == "end_class"


def test_from_dict_rejects_missing_type():
    for bad in ({}, {"payload": {}}, {"type": "x", "payload": []}):
        try:
            wsp.Envelope.from_dict(bad)
            assert False, f"應拋 ValueError：{bad}"
        except ValueError:
            pass


def test_error_and_degraded_use_message_key_only():
    err = wsp.error("stt_start_failed", session_id="s2", detail="boom")
    assert err.type == "error"
    assert err.payload["message_key"] == "stt_start_failed"
    assert err.payload["detail"] == "boom"
    deg = wsp.degraded("stt_helper_error")
    assert deg.type == "degraded"
    assert deg.payload["message_key"] == "stt_helper_error"
    # 不得夾帶會被前端直接顯示的中文原文（只允許 message_key + 除錯 detail）
    for k in err.payload:
        assert k in ("message_key", "detail")


def test_make_accepts_str_and_enum():
    a = wsp.make("metric", payload={"x": 1})
    b = wsp.make(wsp.MsgType.METRIC, payload={"x": 1})
    assert a.type == b.type == "metric"
    assert json.loads(a.to_json())["type"] == "metric"
