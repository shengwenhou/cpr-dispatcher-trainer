"""S5 擺位逐步引導：口頭確認推進／沉默 auto-advance／全局完成跳步／FAQ 重播當前步／metrics。

對應維護者反饋：學員說「跪好了（再來呢）」系統要往下播下一步，不可卡在第一步重複叫跪好。
S5 四步：kneel→handbase→stack→arms，一次播一步；四步完成 chain 進 S6。
"""
from __future__ import annotations

from server.engine.fsm import DialogueEngine, EngineConfig, S5_STEP_IDS
from server.engine.intents import Slot, SlotValue, State
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


def _to_s5(engine):
    """推進到 S5，只播出第一步（kneel）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    engine.on_utterance("叫不醒沒反應", intent(NO_CONSCIOUS))
    a = engine.on_utterance("沒有呼吸", intent(NO_BREATH))
    assert engine.state == State.S5
    return a


def test_s5_enters_playing_only_first_step(engine):
    """進 S5 只播第一步 kneel，不一次連播四步。"""
    a = _to_s5(engine)
    got = ids(a)
    assert "s5_position_kneel_c" in got
    assert "s5_position_handbase_c" not in got  # 不連播
    assert "s5_position_arms_c" not in got
    assert engine._s5_step == 0


def test_s5_verbal_confirm_advances_one_step_each(engine):
    """口頭確認（step_done）→ 每次播下一步；四步走完 chain 進 S6。"""
    _to_s5(engine)
    a1 = ids(engine.on_utterance("好了", step_done_intent()))
    assert a1 == ["s5_position_handbase_c"]
    assert engine.state == State.S5 and engine._s5_step == 1

    a2 = ids(engine.on_utterance("再來呢", step_done_intent()))
    assert a2 == ["s5_position_stack_c"]

    a3 = ids(engine.on_utterance("好了", step_done_intent()))
    assert a3 == ["s5_position_arms_c"]

    # 第四步後再確認 → 完成，進 S6 起始三句
    a4 = ids(engine.on_utterance("下一步", step_done_intent()))
    assert "s6_start_c" in a4
    assert engine.state == State.S6


def test_s5_silence_autoadvance(make_engine):
    """沉默 auto-advance：每步逾 autoadvance 秒自動播下一步（不問「你還在嗎」）。"""
    cfg = EngineConfig(s5_autoadvance_s=4.0)
    eng = make_engine(config=cfg)
    # 用假時鐘：直接以 now 參數推進
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE), now=0.5)
    eng.on_utterance("台北", intent(LOCATION), now=1.0)
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS), now=1.5)
    eng.on_utterance("沒呼吸", intent(NO_BREATH), now=2.0)
    assert eng.state == State.S5 and eng._s5_step == 0
    enter_t = eng._state_enter_s

    # 未到 4s：不推進
    assert ids(eng.tick(enter_t + 3.0)) == []
    assert eng._s5_step == 0
    # 到 4s：自動播第二步
    a = ids(eng.tick(enter_t + 4.0))
    assert a == ["s5_position_handbase_c"]
    assert eng._s5_step == 1
    # 不會觸發沉默 timeout（S5 排除）
    assert not any(x.startswith("meta_timeout") for x in ids(eng.tick(enter_t + 4.0)))


def test_s5_silence_autoadvance_chains_to_s6_on_big_tick(make_engine):
    """單次大幅 tick（模擬 /wait 20）連鎖補上剩餘步驟並走完進 S6。"""
    cfg = EngineConfig(s5_autoadvance_s=4.0)
    eng = make_engine(config=cfg)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE), now=0.5)
    eng.on_utterance("台北", intent(LOCATION), now=1.0)
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS), now=1.5)
    eng.on_utterance("沒呼吸", intent(NO_BREATH), now=2.0)
    enter_t = eng._state_enter_s
    a = ids(eng.tick(enter_t + 30.0))  # 一次跳很久
    # 應補上 handbase/stack/arms 並進 S6
    assert "s5_position_handbase_c" in a
    assert "s5_position_arms_c" in a
    assert "s6_start_c" in a
    assert eng.state == State.S6


def test_s5_global_complete_skips_remaining(engine, metrics):
    """全局完成句（POSITIONING_DONE slot）→ 跳過剩餘步驟直接進 S6，記 skipped。"""
    _to_s5(engine)
    # 才播到第一步，就回報「都擺好了」
    a = ids(engine.on_utterance("我都擺好了手也放好了", intent(POSITIONED)))
    assert "s5_position_handbase_c" not in a  # 不逐步播剩餘
    assert "s6_start_c" in a
    assert engine.state == State.S6
    # metrics 有 skipped 記錄
    subs = [ev for ev in metrics.events if ev.type == EventType.S5_SUBSTEP]
    assert any(ev.data.get("advance") == "skipped" for ev in subs)


def test_s5_faq_reasks_current_substep_not_first(engine):
    """FAQ 答完後重播「當前 sub-step」，不回第一步。"""
    _to_s5(engine)
    engine.on_utterance("好了", step_done_intent())   # → handbase (step 1)
    engine.on_utterance("好了", step_done_intent())   # → stack (step 2)
    assert engine._s5_step == 2
    a = ids(engine.on_utterance("要壓多深", intent(faq_id="faq_depth_ans")))
    assert "faq_depth_ans" in a
    # 重播的是當前步 stack（或其變體），不是第一步 kneel
    assert "s5_position_kneel_c" not in a
    assert any(x.startswith("s5_position_stack") for x in a)
    assert engine.state == State.S5


def test_s5_clarify_reasks_current_substep(make_engine):
    """unknown→clarify 後重播當前 sub-step（不回第一步）。層 4 停用以走層 2。"""
    cfg = EngineConfig(layer4_enabled=False)
    eng = make_engine(config=cfg)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    eng.on_utterance("台北", intent(LOCATION))
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    eng.on_utterance("沒呼吸", intent(NO_BREATH))
    eng.on_utterance("好了", step_done_intent())  # → handbase (step 1)
    # 兩次 unknown → 第二次 takeover 會重問；先看第一次 clarify 不改 step
    a1 = eng.on_utterance("嗯嗯嗯", intent(confidence=0.0))
    assert any(x.startswith("meta_clarify") for x in ids(a1))
    assert eng._s5_step == 1  # step 不變
    assert eng.state == State.S5


def test_s5_substep_metrics_record_advance_reason(engine, metrics):
    """每個 sub-step 播放記入 metrics，附推進方式（enter/confirmed/skipped）。"""
    _to_s5(engine)
    engine.on_utterance("好了", step_done_intent())
    subs = [ev for ev in metrics.events if ev.type == EventType.S5_SUBSTEP]
    reasons = [ev.data.get("advance") for ev in subs]
    assert "enter" in reasons        # 進 S5 第一步
    assert "confirmed" in reasons     # 口頭確認推進
    steps = [ev.data.get("step") for ev in subs]
    assert "s5_position_kneel_c" in steps
    assert "s5_position_handbase_c" in steps


def test_s5_interaction_resets_autoadvance_window(make_engine):
    """S5 中學員互動（如 FAQ）→ 重置 auto-advance 計時器，不會剛答完就被計時器搶推。"""
    cfg = EngineConfig(s5_autoadvance_s=4.0, layer4_enabled=False)
    eng = make_engine(config=cfg)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE), now=0.5)
    eng.on_utterance("台北", intent(LOCATION), now=1.0)
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS), now=1.5)
    eng.on_utterance("沒呼吸", intent(NO_BREATH), now=2.0)
    enter_t = eng._state_enter_s  # 2.0；auto 原定 6.0
    # 在 5.5s（原 auto 6.0 前）插一個 FAQ → 重置 auto 到 5.5+4=9.5
    eng.on_utterance("要壓多深", intent(faq_id="faq_depth_ans"), now=5.5)
    assert eng._s5_step == 0  # FAQ 不推進 sub-step
    # 到原本的 6.0：因已重置，不應 auto-advance
    assert ids(eng.tick(6.0)) == []
    assert eng._s5_step == 0
    # 到 9.5：才 auto-advance
    a = ids(eng.tick(9.5))
    assert a == ["s5_position_handbase_c"]


def test_s5_counting_midway_completes_and_starts_compression(make_engine, metrics):
    """學員在 S5 途中就開始數數 → 視為就位且起壓 → 完成擺位跳 S6、打起壓戳。"""
    eng = make_engine()
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE), now=0.5)
    eng.on_utterance("台北", intent(LOCATION), now=1.0)
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS), now=1.5)
    eng.on_utterance("沒呼吸", intent(NO_BREATH), now=2.0)
    assert eng.state == State.S5
    # 才播到 kneel，學員就開始數數（fastpath 在 S5 也偵測 counting）
    from server.engine.fastpath import RegexFastPath
    from server.engine.intents import IntentResult
    r = RegexFastPath().classify("一下兩下三下", State.S5)  # 帶 COMPRESSIONS_STARTED
    a = ids(eng.on_utterance("一下兩下三下", r, now=6.0))
    assert "s6_start_c" in a
    assert eng.state == State.S6
    comp = [ev for ev in metrics.events if ev.type == EventType.COMPRESSION_START]
    assert len(comp) == 1


def test_s5_step_ids_match_script(script):
    """S5_STEP_IDS 與台詞庫 canonical 一致（防漂移）。"""
    canon = [l.id for l in script.canonical("s5")]
    assert S5_STEP_IDS == canon
