"""持久化：Class→Session→Events 落地、SessionRef 索引、結案 summary、斷線重連 snapshot 重建。"""
from __future__ import annotations

import json

from server.session_store import SessionStore


def _ev(t_mono, etype, trigger_text=None, **data):
    return json.dumps({"t_mono": t_mono, "type": etype, "trigger_text": trigger_text, "data": data},
                      ensure_ascii=False)


def test_class_session_event_roundtrip_and_snapshot(tmp_path):
    store = SessionStore(tmp_path)
    cm = store.create_class("adult", "zh-TW", label="早班A")
    assert cm.class_id and (tmp_path / cm.class_id / "class.json").exists()

    meta = store.create_session(cm.class_id, "學員01", "text")
    assert meta.status == "running"
    assert (tmp_path / cm.class_id / f"{meta.session_id}.jsonl").exists()

    # 逐事件 append（即時落地）
    store.append_event(cm.class_id, meta.session_id, _ev(0.0, "session_start"))
    store.append_event(cm.class_id, meta.session_id, _ev(1.0, "state_enter", state="s5"))
    store.append_event(cm.class_id, meta.session_id, _ev(1.2, "slot_fill", slot="breathing", value="absent"))
    store.append_event(cm.class_id, meta.session_id, _ev(2.0, "utterance_in", trigger_text="他沒有在呼吸"))

    store.finalize_session(cm.class_id, meta.session_id, "completed",
                           {"ohca_recognized_s": 1.0, "compression_start_s": 3.0, "ems_arrived_s": 8.0})

    # 以全新 store 重載（模擬重啟）：class 索引與 key_metrics 就位
    store2 = SessionStore(tmp_path)
    cm2 = store2.load_class(cm.class_id)
    assert len(cm2.sessions) == 1
    ref = cm2.sessions[0]
    assert ref["status"] == "completed"
    assert ref["key_metrics"]["compression_start_s"] == 3.0

    meta2 = store2.load_session(cm.class_id, meta.session_id)
    assert meta2.status == "completed" and meta2.ended_at is not None
    assert meta2.summary["ohca_recognized_s"] == 1.0

    # 斷線重連：由事件流重建快照
    snap = store2.snapshot(cm.class_id, meta.session_id)
    assert snap["state"] == "s5"
    assert snap["filled"]["breathing"] == "absent"
    assert snap["transcript_tail"] == ["他沒有在呼吸"]
    assert snap["summary"]["ems_arrived_s"] == 8.0


def test_alias_is_sanitized_against_path_traversal(tmp_path):
    store = SessionStore(tmp_path)
    cm = store.create_class("adult", "zh-TW")
    meta = store.create_session(cm.class_id, "../../etc/passwd", "text")
    # session_id 不得含路徑分隔符；檔案須落在 class 目錄內
    assert "/" not in meta.session_id and ".." not in meta.session_id
    meta_path = (tmp_path / cm.class_id / f"{meta.session_id}.meta.json").resolve()
    assert str(meta_path).startswith(str((tmp_path / cm.class_id).resolve()))


def test_unique_ids_avoid_collision(tmp_path):
    store = SessionStore(tmp_path)
    cm = store.create_class("adult", "zh-TW", label="同名")
    cm2 = store.create_class("adult", "zh-TW", label="同名")
    assert cm.class_id != cm2.class_id  # 同秒同名不覆蓋
    a = store.create_session(cm.class_id, "王小明", "text")
    b = store.create_session(cm.class_id, "王小明", "text")
    assert a.session_id != b.session_id
