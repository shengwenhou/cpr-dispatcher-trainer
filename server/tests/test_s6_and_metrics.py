"""S6 特殊模式（插播計時器、counting 起壓時間戳）與 metrics（時間戳、觸發原句）測試。"""
from __future__ import annotations

from server.engine.intents import State
from server.engine.metrics import EventType
from .helpers import (
    AMBULANCE,
    LOCATION,
    NO_BREATH,
    NO_CONSCIOUS,
    POSITIONED,
    ids,
    intent,
)


def _drive_to_s6(engine, clock=None):
    """推進到 S6。clock 提供時以其控制 metrics 時間戳（否則沿用引擎既有時鐘）。"""
    def at(t):
        if clock is not None:
            clock.set(t)

    engine.start()
    at(0.5)
    engine.on_utterance("救護車", intent(AMBULANCE))
    at(1.0)
    engine.on_utterance("台北市信義路", intent(LOCATION))
    at(1.5)
    engine.on_utterance("叫不醒沒反應", intent(NO_CONSCIOUS))
    at(2.0)
    engine.on_utterance("沒有呼吸", intent(NO_BREATH))
    at(2.5)
    engine.on_utterance("我準備好了", intent(POSITIONED))
    assert engine.state == State.S6


def test_ohca_timestamp_on_s5_entry(engine, metrics, clock):
    """★辨識 OHCA 時間＝進入 S5 時刻。"""
    engine.start()
    clock.set(0.5)
    engine.on_utterance("救護車", intent(AMBULANCE))
    clock.set(1.0)
    engine.on_utterance("台北市", intent(LOCATION))
    clock.set(1.5)
    engine.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    clock.set(3.3)
    engine.on_utterance("沒呼吸", intent(NO_BREATH))  # 此刻進 S5
    ohca = [ev for ev in metrics.events if ev.type == EventType.OHCA_RECOGNIZED]
    assert len(ohca) == 1
    assert ohca[0].t_mono == 3.3


def test_compression_start_timestamp_first_counting(engine, metrics, clock):
    """★開始按壓時間＝S6 首次偵測數數；只記一次。"""
    _drive_to_s6(engine, clock)
    # 首次數數
    clock.set(5.0)
    engine.on_utterance("一下兩下三下", intent())
    comp = [ev for ev in metrics.events if ev.type == EventType.COMPRESSION_START]
    assert len(comp) == 1
    assert comp[0].t_mono == 5.0
    assert comp[0].trigger_text == "一下兩下三下"  # 附觸發原句
    # 後續再數數不再記第二次
    clock.set(6.0)
    engine.on_utterance("四下五下六下", intent())
    comp2 = [ev for ev in metrics.events if ev.type == EventType.COMPRESSION_START]
    assert len(comp2) == 1


def test_s6_counting_does_not_advance_or_interrupt(engine):
    """S6 內數數不推進狀態、不打斷（回傳空動作）。"""
    _drive_to_s6(engine)
    a = engine.on_utterance("一下兩下三下", intent(), now=5.0)
    assert ids(a) == []
    assert engine.state == State.S6


def test_s6_insert_timer_fires_and_rotates(make_engine):
    """S6 插播計時器：到期播 insert；連兩次不重複。用短間隔設定測。"""
    from server.engine.fsm import EngineConfig

    cfg = EngineConfig(s6_insert_min_s=5.0, s6_insert_max_s=5.0)  # 固定 5s 便於斷言
    eng = make_engine(config=cfg, rng_seed=42)
    _drive_to_s6(eng)
    enter_t = eng._state_enter_s
    # 未到 5s：無插播
    assert ids(eng.tick(enter_t + 3.0)) == []
    # 到 5s：第一次插播
    a1 = ids(eng.tick(enter_t + 5.0))
    assert len(a1) == 1 and a1[0].startswith("s6_encourage")
    # 再 5s：第二次插播，且與第一次不同 id
    a2 = ids(eng.tick(enter_t + 10.0))
    assert len(a2) == 1 and a2[0].startswith("s6_encourage")
    assert a1[0] != a2[0]  # 同輪不重複


def test_s6_end_signal_to_s7(engine, metrics, clock):
    """S6 聽到結束訊號 → S7，記 EMS_ARRIVED。"""
    _drive_to_s6(engine, clock)
    clock.set(5.0)
    engine.on_utterance("一下兩下三下", intent())
    clock.set(30.0)
    a = engine.on_utterance("救護人員到了", intent())
    assert engine.state == State.S7
    assert "s7_handover_c" in ids(a)
    ems = [ev for ev in metrics.events if ev.type == EventType.EMS_ARRIVED]
    assert len(ems) == 1 and ems[0].t_mono == 30.0


def test_metrics_events_carry_trigger_text(engine, metrics):
    """每筆關鍵事件附觸發原句（可回溯）。"""
    engine.start()
    engine.on_utterance("我要救護車", intent(AMBULANCE), now=0.5)
    # UTTERANCE_IN 與 SLOT_FILL 應帶原句
    utt = [ev for ev in metrics.events if ev.type == EventType.UTTERANCE_IN]
    assert utt[0].trigger_text == "我要救護車"
    fills = [ev for ev in metrics.events if ev.type == EventType.SLOT_FILL]
    assert all(f.trigger_text == "我要救護車" for f in fills)


def test_metrics_monotonic_timestamps(engine, metrics, clock):
    """時間戳單調遞增（虛擬時鐘推進）。"""
    engine.start()
    clock.set(1.0)
    engine.on_utterance("救護車", intent(AMBULANCE))
    clock.set(2.0)
    engine.on_utterance("台北市", intent(LOCATION))
    ts = [ev.t_mono for ev in metrics.events]
    assert ts == sorted(ts)  # 單調不減


def test_metrics_summary_shape(engine, metrics, clock):
    """summary 產出 SPEC 第六節關鍵指標欄位，可序列化。"""
    _drive_to_s6(engine, clock)
    clock.set(5.0)
    engine.on_utterance("一下兩下三下", intent())
    clock.set(20.0)
    engine.on_utterance("救護人員到了", intent())
    s = metrics.summary()
    assert s["ohca_recognized_s"] is not None
    assert s["compression_start_s"] == 5.0
    assert s["ems_arrived_s"] == 20.0
    assert set(["s5", "s6"]).issubset(set(s["state_dwell_s"].keys()))
    # JSONL 可序列化
    jl = metrics.to_jsonl()
    assert jl.count("\n") == len(metrics.events) - 1


def test_metrics_jsonl_roundtrip(engine, metrics, tmp_path):
    """事件流可寫成 JSONL 檔並逐行解析。"""
    import json

    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE), now=0.5)
    p = tmp_path / "events.jsonl"
    metrics.dump_jsonl(p)
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == len(metrics.events)
    for l in lines:
        d = json.loads(l)  # 每行合法 JSON
        assert "type" in d and "t_mono" in d
