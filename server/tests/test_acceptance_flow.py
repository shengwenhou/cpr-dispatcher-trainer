"""端到端驗收：文字模式完整跑一場 S0→S7，並驗證三個防禦案例。透過 TextModeDriver 走 runtime。"""
from __future__ import annotations

import random

from server.config import load_config
from server.engine.fsm import DialogueEngine, EngineConfig
from server.engine.intents import State
from server.engine.metrics import EventType, MetricsRecorder
from server.engine.script_store import ScriptStore
from server.providers.tts import TextTTS
from server.runtime import IntentPipeline, TextModeDriver
from .conftest import FakeClock


def _build_text_driver(seed=7, layer4_generator=None, llm=None):
    """組一組文字模式 driver：決定性時鐘與 rng。llm=None → 走 fastpath＋keyword 降級。"""
    cfg = load_config()
    rng = random.Random(seed)
    clock = FakeClock()
    script = ScriptStore(cfg.script_path, rng=rng)
    metrics = MetricsRecorder(clock=clock)
    engine = DialogueEngine(
        script=script,
        metrics=metrics,
        config=EngineConfig(s6_insert_min_s=15.0, s6_insert_max_s=20.0),
        rng=rng,
        layer4_generator=layer4_generator,
    )
    pipeline = IntentPipeline(llm=llm)
    tts = TextTTS(text_lookup=script.text_of)
    driver = TextModeDriver(engine=engine, pipeline=pipeline, tts=tts, text_of=script.text_of)
    return driver, engine, metrics, clock, script


def test_full_conversation_text_mode():
    """完整一場：開場→要救護車→給地址→叫不醒→沒呼吸→擺位→數數→插播→救護人員到了→S7。"""
    driver, engine, metrics, clock, script = _build_text_driver(seed=7)
    driver.start()
    assert engine.state == State.S0

    def feed(text, dt=0.5):
        clock.advance(dt)
        return driver.feed(text, now=clock.now())

    feed("我要救護車")
    assert engine.state == State.S2
    feed("台北市大安區忠孝東路四段一號")
    assert engine.state == State.S3
    feed("他叫不醒，沒有反應")
    assert engine.state == State.S4
    feed("他沒有在呼吸")
    assert engine.state == State.S5
    feed("我跪好了，手也放好了")
    assert engine.state == State.S6

    # 數數（起壓時間戳）
    feed("一下兩下三下四下")
    comp = [ev for ev in metrics.events if ev.type == EventType.COMPRESSION_START]
    assert len(comp) == 1

    # 插播：推進到插播計時器到期（seed 固定，插播區間 15–20s，走 40s 保證觸發）
    clock.advance(40.0)
    insert_actions = driver.tick(clock.now())
    assert any(a.line_id and a.line_id.startswith("s6_encourage") for a in insert_actions)

    # 結束訊號
    feed("救護人員到了")
    assert engine.state == State.S7
    assert engine.finished is True

    # 關鍵指標齊備
    s = metrics.summary()
    assert s["ohca_recognized_s"] is not None
    assert s["compression_start_s"] is not None
    assert s["ems_arrived_s"] is not None
    # 起壓時間 < EMS 抵達時間
    assert s["compression_start_s"] < s["ems_arrived_s"]


class _FaqLLM:
    """假 LLM：把含「人工呼吸」的句子分類為對應 FAQ（FAQ 命中屬 LLM 能力，非降級路徑）。"""

    def available(self):
        return True

    def classify_intent(self, text, state, context=None):
        from server.engine.intents import IntentResult

        if "人工呼吸" in text or "口對口" in text:
            return IntentResult(source="llm", confidence=0.9, faq_id="faq_rescue_breath_ans")
        return IntentResult(source="llm", confidence=0.0)


def test_defense_case_faq_insertion():
    """防禦案例 1：FAQ 插問「要不要做人工呼吸」→ 答句 + 回到當前問句，不改狀態。

    FAQ 意圖命中需 LLM（keyword 後備不涵蓋），故注入假 LLM 模擬。"""
    driver, engine, metrics, clock, script = _build_text_driver(llm=_FaqLLM())
    driver.start()

    def feed(text, dt=0.5):
        clock.advance(dt)
        return driver.feed(text, now=clock.now())

    feed("救護車")
    feed("台北市信義路")
    assert engine.state == State.S3
    actions = feed("要不要做人工呼吸")
    line_ids = [a.line_id for a in actions if a.line_id]
    assert "faq_rescue_breath_ans" in line_ids
    assert any(x.startswith("s3_consciousness") for x in line_ids)
    assert engine.state == State.S3  # FAQ 不改變狀態


def test_defense_case_two_unknowns_takeover():
    """防禦案例 2：連續兩次 unknown → takeover。"""
    driver, engine, metrics, clock, script = _build_text_driver()
    driver.start()

    def feed(text, dt=0.5):
        clock.advance(dt)
        return driver.feed(text, now=clock.now())

    feed("救護車")
    feed("台北市信義路")  # S3
    feed("今天天氣真好")  # unknown #1 → clarify
    actions = feed("你昨天有沒有吃飯")  # unknown #2 → takeover
    line_ids = [a.line_id for a in actions if a.line_id]
    assert any(x.startswith("meta_takeover") for x in line_ids)


def test_defense_case_silence_timeout():
    """防禦案例 3：沉默 timeout（指令模擬）→ 分級 l1/l2。在 turn-taking 狀態（S2）測。"""
    driver, engine, metrics, clock, script = _build_text_driver()
    driver.start()
    clock.advance(0.5)
    driver.feed("救護車", now=clock.now())  # 到 S2，等地址
    # 沉默 6s → l1
    clock.advance(6.0)
    a1 = driver.tick(clock.now())
    assert any(a.line_id and a.line_id.startswith("meta_timeout_l1") for a in a1)
    # 再到 11s → l2
    clock.advance(5.0)
    a2 = driver.tick(clock.now())
    assert any(a.line_id and a.line_id.startswith("meta_timeout_l2") for a in a2)
    timeouts = [ev for ev in metrics.events if ev.type == EventType.TIMEOUT]
    assert {ev.data["level"] for ev in timeouts} == {1, 2}
