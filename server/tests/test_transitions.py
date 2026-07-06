"""狀態轉移全路徑測試：S0→S7 逐狀態推進、跳步、分支、確認/詢問句抑制。"""
from __future__ import annotations

from server.engine.intents import State
from .helpers import (
    AGONAL,
    AMBULANCE,
    COMPRESSING,
    FIRE,
    LOCATION,
    NO_BREATH,
    NO_CONSCIOUS,
    POSITIONED,
    ids,
    intent,
)


def test_start_plays_opening(engine):
    actions = engine.start()
    assert ids(actions) == ["s0_open_c"]
    assert engine.state == State.S0


def test_linear_full_path(engine):
    """逐句推進走完 S0→S7（每次只填一個 slot）。"""
    engine.start()

    # S0 → S1(確認) → S2(詢問地址)
    a = engine.on_utterance("我要救護車", intent(AMBULANCE))
    assert ids(a) == ["s1_need_ambulance_c", "s2_addr_ask_c"]
    assert engine.state == State.S2

    # 地址 → S2 確認 → S3 詢問意識
    a = engine.on_utterance("台北市信義路一號", intent(LOCATION))
    assert ids(a) == ["s2_addr_confirm_c", "s3_consciousness_c"]
    assert engine.state == State.S3

    # 意識(無) → S4 呼吸詢問（尚未判定呼吸，停在 S4）
    a = engine.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    assert "s4_breathing_c" in ids(a)
    assert engine.state == State.S4

    # 呼吸「明確無」→ S4 判定用 v01「他沒有在正常呼吸…」（非「這種喘」）→ S5 擺位
    a = engine.on_utterance("沒有呼吸", intent(NO_BREATH))
    got = ids(a)
    assert "s4_agonal_ruling_v01" in got
    assert "s4_agonal_ruling_c" not in got   # ABSENT 不用「這種喘」判定句
    assert "s5_position_kneel_c" in got
    assert engine.state == State.S5

    # 擺位完成 → S6 起始指令
    a = engine.on_utterance("好了", intent(POSITIONED))
    got = ids(a)
    assert "s6_start_c" in got
    assert engine.state == State.S6

    # 結束訊號 → S7
    a = engine.on_utterance("救護人員到了", intent(end_signal=True))
    assert "s7_handover_c" in ids(a)
    assert engine.state == State.S7
    assert engine.finished is True


def test_s2_confirm_suppressed_until_location(engine):
    """進 S2 時地址未給 → 只播詢問句，不播確認句；地址給了才播確認。"""
    engine.start()
    a = engine.on_utterance("救護車", intent(AMBULANCE))
    got = ids(a)
    assert "s2_addr_ask_c" in got
    assert "s2_addr_confirm_c" not in got  # 還沒拿到地址，抑制確認句


def test_s1_fire_branch_not_in_normal_flow(engine):
    """S1 的 fire_redirect 分支句不在正常流播出（只有明確消防車分支才觸發，屬 UI 階段細節）。"""
    engine.start()
    a = engine.on_utterance("救護車", intent(AMBULANCE))
    assert "s1_fire_redirect_c" not in ids(a)


def test_multislot_jump_ahead(engine):
    """★多 slot 跳步：在 S3 一句話含意識+呼吸 → 跳過 S4 詢問、播判定、直達 S5。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    assert engine.state == State.S3

    # 一句話：沒反應 + 明確沒呼吸
    a = engine.on_utterance("他叫不醒也沒有呼吸", intent(NO_CONSCIOUS, NO_BREATH))
    got = ids(a)
    # S4 呼吸詢問句與 probe 都應被抑制（已知答案、非模糊）；判定句用 v01（ABSENT）
    assert "s4_breathing_c" not in got
    assert "s4_agonal_probe_c" not in got     # 明確答案，不追問
    assert "s4_agonal_ruling_v01" in got
    assert "s4_agonal_ruling_c" not in got
    # 跳到 S5
    assert "s5_position_kneel_c" in got
    assert engine.state == State.S5


def test_full_jump_to_s6(engine):
    """一句話填齊意識+呼吸+已壓 → 一路跳到 S6。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("在家裡台北市", intent(LOCATION))
    a = engine.on_utterance(
        "他沒反應沒呼吸我已經在壓了",
        intent(NO_CONSCIOUS, NO_BREATH, COMPRESSING),
    )
    got = ids(a)
    assert "s5_position_kneel_c" in got
    assert "s6_start_c" in got
    assert engine.state == State.S6


def test_agonal_counts_as_no_breathing(engine):
    """瀕死喘息（AGONAL）視同無正常呼吸 → 觸發 OHCA 並推進。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市", intent(LOCATION))
    engine.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    a = engine.on_utterance("他很久才喘一大口", intent(AGONAL))
    assert engine.state == State.S5
    assert "s5_position_kneel_c" in ids(a)


def test_end_signal_from_any_state(make_engine):
    """任何狀態聽到結束訊號都直達 S7。"""
    eng = make_engine()
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    # 在 S2 就喊救護人員到了
    a = eng.on_utterance("救護人員到了", intent(end_signal=True))
    assert eng.state == State.S7
    assert eng.finished is True
    assert "s7_handover_c" in ids(a)


def test_low_confidence_does_not_advance(engine):
    """LLM 信心不足 → 不前進，播澄清（層 2）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    before = engine.state
    # 有 slot 但信心低於門檻
    a = engine.on_utterance("呃…好像是", intent(LOCATION, confidence=0.2))
    assert engine.state == before  # 未前進
    got = ids(a)
    assert any(x.startswith("meta_clarify") for x in got)


def test_fire_truck_redirect(engine):
    """說要消防車（WANTS_AMBULANCE=NO）→ 播 fire_redirect 引導回，停在 S1，不進 S2。"""
    engine.start()
    a = engine.on_utterance("我要消防車", intent(FIRE))
    got = ids(a)
    assert "s1_fire_redirect_c" in got       # 播引導回句
    assert "s1_need_ambulance_c" not in got  # 不播救護車確認
    assert engine.state == State.S1
    assert "s2_addr_ask_c" not in got        # 不前進到 S2

    # 之後改口要救護車 → 正常推進到 S2
    a2 = engine.on_utterance("喔好那要救護車", intent(AMBULANCE))
    assert engine.state == State.S2
    assert "s2_addr_ask_c" in ids(a2)
