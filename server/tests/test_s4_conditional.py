"""S4 條件 utterance 選擇：依呼吸回報值選 probe / ruling_c / ruling_v01（臨床對話正確性）。

對應台詞庫審定備註（content/zh-TW/adult_script_draft.md S4 節）：
- 進入 S4 只問 s4_breathing_c（有無正常呼吸）；不無條件連播 probe。
- BREATHING=UNCLEAR（描述模糊）→ 播 s4_agonal_probe_c 追問，停 S4。
- BREATHING=AGONAL（瀕死喘息）→ 判定用 s4_agonal_ruling_c「這種喘不算正常呼吸…」。
- BREATHING=ABSENT（明確無呼吸）→ 判定用 s4_agonal_ruling_v01「他沒有在正常呼吸…」。
"""
from __future__ import annotations

from server.engine.intents import State
from .helpers import (
    AGONAL,
    AMBULANCE,
    LOCATION,
    NO_BREATH,
    NO_CONSCIOUS,
    UNCLEAR_BREATH,
    ids,
    intent,
)


def _to_s4(engine):
    """推進到 S4（意識已判定，等呼吸回報）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路一段", intent(LOCATION))
    engine.on_utterance("他叫不醒沒反應", intent(NO_CONSCIOUS))
    assert engine.state == State.S4


def test_a_enter_s4_asks_breathing_only_no_probe(engine):
    """（a）只回報意識 → 進 S4 只問 s4_breathing_c，不無條件播 probe。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路一段", intent(LOCATION))
    a = engine.on_utterance("他叫不醒，都沒有反應", intent(NO_CONSCIOUS))
    got = ids(a)
    assert "s4_breathing_c" in got
    assert "s4_agonal_probe_c" not in got   # 尚未回報呼吸，不追問
    assert "s4_agonal_ruling_c" not in got
    assert "s4_agonal_ruling_v01" not in got
    assert engine.state == State.S4


def test_b_unclear_breathing_triggers_probe_and_stays(engine):
    """（b-1）回報「有喘但模糊」→ 播 probe 追問，停在 S4（不推進）。"""
    _to_s4(engine)
    a = engine.on_utterance("好像有一點呼吸，我不太確定", intent(UNCLEAR_BREATH))
    got = ids(a)
    assert "s4_agonal_probe_c" in got        # 追問釐清
    assert "s4_agonal_ruling_c" not in got   # 尚未判定
    assert "s4_agonal_ruling_v01" not in got
    assert "s5_position_kneel_c" not in got  # 不推進
    assert engine.state == State.S4


def test_b_unclear_then_agonal_rules_with_c(engine):
    """（b-2）probe 後學員澄清為瀕死喘息 → ruling 用 c，進 S5。"""
    _to_s4(engine)
    engine.on_utterance("好像有點喘，不太確定", intent(UNCLEAR_BREATH))  # → probe，停 S4
    assert engine.state == State.S4
    a = engine.on_utterance("對，他很久才用力喘一大口", intent(AGONAL))
    got = ids(a)
    assert "s4_agonal_ruling_c" in got        # 瀕死喘息用「這種喘」
    assert "s4_agonal_ruling_v01" not in got
    assert "s5_position_kneel_c" in got
    assert engine.state == State.S5


def test_agonal_direct_rules_with_c(engine):
    """（b-alt）一次就明確回報瀕死喘息 → 直接 ruling c，不必先 probe。"""
    _to_s4(engine)
    a = engine.on_utterance("他很久才喘一下，像打呼那樣", intent(AGONAL))
    got = ids(a)
    assert "s4_agonal_ruling_c" in got
    assert "s4_agonal_probe_c" not in got     # 已明確，不需追問
    assert engine.state == State.S5


def test_c_absent_rules_with_v01(engine):
    """（c）明確沒呼吸 → ruling 用 v01（非「這種喘」），進 S5。"""
    _to_s4(engine)
    a = engine.on_utterance("胸口都沒有起伏，也沒有在呼吸", intent(NO_BREATH))
    got = ids(a)
    assert "s4_agonal_ruling_v01" in got
    assert "s4_agonal_ruling_c" not in got    # 沒有喘，不用「這種喘」
    assert "s4_agonal_probe_c" not in got
    assert "s5_position_kneel_c" in got
    assert engine.state == State.S5


def test_d_multislot_jump_absent_uses_v01_no_probe(engine):
    """（d）多 slot 跳步（意識+明確無呼吸）→ 播 v01 判定、不播 probe，再進 S5。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    assert engine.state == State.S3
    a = engine.on_utterance("他叫不醒也沒有呼吸", intent(NO_CONSCIOUS, NO_BREATH))
    got = ids(a)
    assert "s4_breathing_c" not in got        # 詢問句抑制
    assert "s4_agonal_probe_c" not in got     # 不追問
    assert "s4_agonal_ruling_v01" in got      # 明確無呼吸 → v01
    assert "s4_agonal_ruling_c" not in got
    assert "s5_position_kneel_c" in got
    assert engine.state == State.S5


def test_d_multislot_jump_agonal_uses_c(engine):
    """（d-alt）多 slot 跳步但呼吸為瀕死喘息 → 判定用 c、不播 probe。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市信義路", intent(LOCATION))
    a = engine.on_utterance("他叫不醒，一直很久才喘一大口", intent(NO_CONSCIOUS, AGONAL))
    got = ids(a)
    assert "s4_agonal_ruling_c" in got
    assert "s4_agonal_probe_c" not in got
    assert "s4_agonal_ruling_v01" not in got
    assert engine.state == State.S5


def test_ohca_timestamp_fires_regardless_of_ruling_choice(engine, metrics):
    """不論走 v01 或 c，進入 S5 都應打 OHCA 時間戳。"""
    from server.engine.metrics import EventType

    _to_s4(engine)
    engine.on_utterance("完全沒呼吸沒起伏", intent(NO_BREATH))
    ohca = [ev for ev in metrics.events if ev.type == EventType.OHCA_RECOGNIZED]
    assert len(ohca) == 1
