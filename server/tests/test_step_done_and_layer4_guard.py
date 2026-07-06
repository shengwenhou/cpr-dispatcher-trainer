"""STEP_DONE 意圖在各狀態的行為，與層 4 生成的「超前流程動作」驗證層。"""
from __future__ import annotations

from server.engine.fsm import EngineConfig
from server.engine.intents import State, layer4_text_violates
from server.engine.metrics import EventType
from .helpers import (
    AMBULANCE,
    LOCATION,
    NO_BREATH,
    NO_CONSCIOUS,
    POSITIONED,
    ids,
    intent,
    step_done_intent,
)


# ── STEP_DONE 在不同狀態 ────────────────────────────────────────
def test_step_done_in_s3_reasks_current_question(engine):
    """S3「然後呢」→ 重播當前問句（學員在問流程，答案就是當前問題），不走層 4。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    assert engine.state == State.S3
    a = ids(engine.on_utterance("然後呢", step_done_intent()))
    # 重播 S3 意識問句（canonical 或變體）
    assert any(x.startswith("s3_consciousness") for x in a)
    # 未觸發層 4
    # （step_done 專屬處理，不進 _handle_unknown）
    assert engine.state == State.S3


def test_step_done_in_s5_advances_substep(engine):
    """S5「再來呢」→ 播下一 sub-step（不是一次填完 POSITIONING_DONE）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北", intent(LOCATION))
    engine.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    engine.on_utterance("沒呼吸", intent(NO_BREATH))
    assert engine.state == State.S5 and engine._s5_step == 0
    a = ids(engine.on_utterance("再來呢", step_done_intent()))
    assert a == ["s5_position_handbase_c"]
    assert engine.state == State.S5


def test_step_done_in_s6_returns_encourage(engine):
    """S6「然後呢」→ 壓胸持續中，回一條 encourage insert（不觸發層 4）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北", intent(LOCATION))
    engine.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    engine.on_utterance("沒呼吸", intent(NO_BREATH))
    engine.on_utterance("都擺好了", intent(POSITIONED))
    assert engine.state == State.S6
    a = ids(engine.on_utterance("然後呢", step_done_intent()))
    assert len(a) == 1 and a[0].startswith("s6_encourage")


def test_step_done_not_counted_as_defense_layer(engine, metrics):
    """step_done 記為 layer=0（正常流程動作），非五層防禦觸發。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北", intent(LOCATION))
    engine.on_utterance("然後呢", step_done_intent())
    step_events = [ev for ev in metrics.events if ev.type == EventType.DEFENSE and ev.data.get("kind") == "step_done"]
    assert step_events and all(ev.data.get("layer") == 0 for ev in step_events)


# ── 層 4 驗證層：超前流程動作 ──────────────────────────────────
def test_layer4_validator_flags_compression_outside_s6():
    """S5 生成含「壓胸」→ 判違規（該動作只屬 S6）。"""
    assert layer4_text_violates("你先繼續壓胸就好", State.S5) == "壓胸"
    assert layer4_text_violates("我們一起壓下去", State.S5) == "壓下去"


def test_layer4_validator_allows_compression_in_s6():
    """S6 生成含「壓」屬合法（正在壓胸階段）。"""
    assert layer4_text_violates("保持節奏繼續壓", State.S6) is None


def test_layer4_validator_flags_never_allowed_actions():
    """AED／人工呼吸／電擊在任何狀態都不該由層 4 生成。"""
    assert layer4_text_violates("拿 AED 過來電擊", State.S6) is not None
    assert layer4_text_violates("先做人工呼吸", State.S6) is not None
    assert layer4_text_violates("把他翻過來", State.S5) is not None


def test_layer4_validator_allows_pure_comfort():
    """純安撫、無動作詞 → 通過。"""
    assert layer4_text_violates("別怕，我一直在線上陪你。", State.S5) is None
    assert layer4_text_violates("我知道你很緊張，我們慢慢來。", State.S3) is None


def test_layer4_rejects_procedure_leak_and_degrades_to_layer2(make_engine, metrics):
    """層 4 生成含超前動作（S5 說壓胸）→ 丟棄、降級層 2（不播該生成句）。"""
    def bad_gen(utterance, question, state_context=""):
        return "你先繼續壓胸不要停"  # S5 不該提壓胸

    cfg = EngineConfig(layer4_enabled=True)
    eng = make_engine(config=cfg, layer4_generator=bad_gen)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    eng.on_utterance("台北", intent(LOCATION))
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    eng.on_utterance("沒呼吸", intent(NO_BREATH))
    assert eng.state == State.S5
    # 在 S5 說一句無法歸類、有實質內容的話 → 觸發層 4 生成 → 被驗證層擋下 → 層 2
    a = ids(eng.on_utterance("我好怕我會弄錯", intent(confidence=0.1)))
    # 不應出現任何 <dynamic>（生成被丟棄）
    assert "<dynamic>" not in a
    # 應走層 2 clarify
    assert any(x.startswith("meta_clarify") for x in a)
    # metrics 有 rejected 記錄
    rejected = [ev for ev in metrics.events if ev.type == EventType.DEFENSE and ev.data.get("rejected")]
    assert rejected and rejected[0].data.get("keyword") == "壓胸"


def test_layer4_accepts_clean_generation(make_engine, metrics):
    """層 4 生成純安撫（無動作詞）→ 通過，先 filler 再播生成。"""
    def good_gen(utterance, question, state_context=""):
        return "別怕，我一直在線上陪你，我們一步一步來。"

    cfg = EngineConfig(layer4_enabled=True)
    eng = make_engine(config=cfg, layer4_generator=good_gen)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    a = ids(eng.on_utterance("我整個慌掉了怎麼辦", intent(confidence=0.1)))
    assert any(x.startswith("meta_filler") for x in a)
    assert "<dynamic>" in a
