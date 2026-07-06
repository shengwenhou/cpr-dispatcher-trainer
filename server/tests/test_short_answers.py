"""語境化極簡回應解讀：單字/極簡回答依當前問句解讀（好→step_done、不會→CONSCIOUS_NO…）。

對應維護者第三場試玩存證（logs/layer4/ 六筆全落層 4 的極簡回答）。驗收要求案例全覆蓋。
"""
from __future__ import annotations

from server.engine.fastpath import (
    KeywordFallbackClassifier,
    RegexFastPath,
    resolve_short_answer,
)
from server.engine.intents import Slot, SlotValue, State
from .helpers import AMBULANCE, LOCATION, ids, intent


# ── 純解讀函式（單元）──────────────────────────────────────────
def test_resolve_s5_affirmatives_are_step_done():
    for w in ["好", "好，", "好啦", "有", "對", "嗯", "是", "OK", "可以"]:
        r = resolve_short_answer(w, State.S5)
        assert r is not None and r.step_done is True, w


def test_resolve_s5_completion_form_is_step_done():
    for w in ["我早就扣起來了", "已經放好了", "手弄好了", "都做完了"]:
        r = resolve_short_answer(w, State.S5)
        assert r is not None and r.step_done is True, w


def test_resolve_s3_negation_is_conscious_no():
    for w in ["不會", "沒有", "沒反應", "叫不醒"]:
        r = resolve_short_answer(w, State.S3)
        assert r is not None and r.slots.get(Slot.CONSCIOUSNESS) == SlotValue.NO, w


def test_resolve_s3_affirmative_is_conscious_yes():
    for w in ["有", "會", "對", "是"]:
        r = resolve_short_answer(w, State.S3)
        assert r is not None and r.slots.get(Slot.CONSCIOUSNESS) == SlotValue.YES, w


def test_resolve_s4_has_breathing_is_unclear():
    for w in ["有", "對", "是"]:
        r = resolve_short_answer(w, State.S4)
        assert r is not None and r.slots.get(Slot.BREATHING) == SlotValue.UNCLEAR, w


def test_resolve_s4_negation_is_absent():
    for w in ["沒有", "沒了", "不會"]:
        r = resolve_short_answer(w, State.S4)
        assert r is not None and r.slots.get(Slot.BREATHING) == SlotValue.ABSENT, w


def test_resolve_misfire_prevention():
    """誤觸防範：非整詞/長句/含痛等不可命中。"""
    assert resolve_short_answer("好痛", State.S5) is None
    assert resolve_short_answer("好痛喔", State.S5) is None
    assert resolve_short_answer("有人嗎", State.S3) is None
    assert resolve_short_answer("有點喘", State.S4) is None          # 「喘」屬 agonal，不該當「有」
    assert resolve_short_answer("好像怪怪的", State.S4) is None
    # 「好」對意識 yes-no 問句語意不明 → 交 LLM（不硬判）
    assert resolve_short_answer("好", State.S3) is None
    # 「都擺好了」屬全局完成，讓路給 detect_all_positioned（不在此當 step_done）
    r = resolve_short_answer("都擺好了", State.S5)
    assert r is None or r.step_done is False


def test_resolve_all_positioned_not_shadowed_by_completion_form():
    """完成式 pattern 不可搶走全局完成句：都擺好了仍走 all_positioned。"""
    fp = RegexFastPath()
    r = fp.classify("我都擺好了手也放好了", State.S5)
    assert r.slots.get(Slot.POSITIONING_DONE) == SlotValue.YES
    assert r.step_done is False


# ── fastpath 短路 ───────────────────────────────────────────────
def test_fastpath_short_answer_shortcircuits():
    fp = RegexFastPath()
    assert fp.classify("不會", State.S3).slots.get(Slot.CONSCIOUSNESS) == SlotValue.NO
    assert fp.classify("有", State.S4).slots.get(Slot.BREATHING) == SlotValue.UNCLEAR
    assert fp.classify("好", State.S5).step_done is True


# ── keyword 後備同步 ────────────────────────────────────────────
def test_keyword_short_answer_synced():
    kw = KeywordFallbackClassifier()
    assert kw.classify("不會", State.S3).slots.get(Slot.CONSCIOUSNESS) == SlotValue.NO
    assert kw.classify("好", State.S5).step_done is True
    assert kw.classify("有", State.S4).slots.get(Slot.BREATHING) == SlotValue.UNCLEAR


# ── 端到端（引擎）：驗收要求案例 ────────────────────────────────
def _to_s3(engine):
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    assert engine.state == State.S3


def test_e2e_s3_buhui_advances_to_s4(engine):
    """S3「不會」→ CONSCIOUS=NO → 正常進 S4（不落層 4，不打轉）。"""
    _to_s3(engine)
    a = ids(engine.on_utterance("不會", resolve_short_answer("不會", State.S3)))
    assert "s4_breathing_c" in a
    assert engine.state == State.S4


def test_e2e_s3_conscious_yes_stays_and_reasks(engine):
    """S3「有」→ CONSCIOUS=YES → 停 S3 重問，不誤進 S4。"""
    _to_s3(engine)
    a = ids(engine.on_utterance("有", resolve_short_answer("有", State.S3)))
    assert engine.state == State.S3
    assert "s4_breathing_c" not in a
    # 重問意識問句（canonical 或變體）
    assert any(x.startswith("s3_consciousness") for x in a)


def test_e2e_s4_has_triggers_probe(engine):
    """S4「有」→ BREATHING=UNCLEAR → 觸發既有 probe 條件句，停 S4。"""
    _to_s3(engine)
    engine.on_utterance("不會", resolve_short_answer("不會", State.S3))  # → S4
    a = ids(engine.on_utterance("有", resolve_short_answer("有", State.S4)))
    assert "s4_agonal_probe_c" in a
    assert engine.state == State.S4


def test_e2e_s4_meiyou_rules_absent(engine):
    """S4「沒有」→ BREATHING=ABSENT → 判定 v01 → S5。"""
    _to_s3(engine)
    engine.on_utterance("不會", resolve_short_answer("不會", State.S3))  # → S4
    a = ids(engine.on_utterance("沒有", resolve_short_answer("沒有", State.S4)))
    assert "s4_agonal_ruling_v01" in a
    assert engine.state == State.S5


def test_e2e_s5_hao_advances_substep(engine):
    """S5「好」→ step_done → 播下一 sub-step。"""
    _to_s3(engine)
    engine.on_utterance("不會", resolve_short_answer("不會", State.S3))
    engine.on_utterance("沒有", resolve_short_answer("沒有", State.S4))
    assert engine.state == State.S5 and engine._s5_step == 0
    a = ids(engine.on_utterance("好", resolve_short_answer("好", State.S5)))
    assert a == ["s5_position_handbase_c"]


def test_e2e_completion_form_advances_substep(engine):
    """S5「我早就扣起來了」→ step_done → 下一步。"""
    _to_s3(engine)
    engine.on_utterance("不會", resolve_short_answer("不會", State.S3))
    engine.on_utterance("沒有", resolve_short_answer("沒有", State.S4))
    a = ids(engine.on_utterance("我早就扣起來了", resolve_short_answer("我早就扣起來了", State.S5)))
    assert a == ["s5_position_handbase_c"]


def test_e2e_haotong_does_not_misfire(engine):
    """「好痛」不誤觸 step_done——S5 應走層防禦（此處無層 4 → clarify）。"""
    from server.engine.fsm import EngineConfig

    # 用無層 4 的引擎，確保「好痛」落到層 2 clarify（而非被當 step_done 推進）
    eng_cfg = EngineConfig(layer4_enabled=False)
    from server.engine.fsm import DialogueEngine
    import random
    from server.config import load_config
    from server.engine.metrics import MetricsRecorder
    from server.engine.script_store import ScriptStore

    cfg = load_config()
    sc = ScriptStore(cfg.script_path, rng=random.Random(1))
    m = MetricsRecorder(clock=lambda: 0.0)
    eng = DialogueEngine(sc, m, eng_cfg, rng=random.Random(1))
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    eng.on_utterance("台北", intent(LOCATION))
    eng.on_utterance("不會", resolve_short_answer("不會", State.S3))
    eng.on_utterance("沒有", resolve_short_answer("沒有", State.S4))
    assert eng.state == State.S5 and eng._s5_step == 0
    # 「好痛」：resolve 回 None → pipeline 交 LLM（此處無 LLM，走 keyword→unknown→clarify）
    r = resolve_short_answer("好痛", State.S5)
    assert r is None
    a = ids(eng.on_utterance("好痛", intent(confidence=0.0)))
    assert eng._s5_step == 0  # 未推進
    assert any(x.startswith("meta_clarify") for x in a)
