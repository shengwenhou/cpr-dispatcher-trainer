"""台詞庫載入／變體輪替不重複，與 RegexFastPath／keyword 分類器測試。"""
from __future__ import annotations

import random

from server.engine.fastpath import (
    KeywordFallbackClassifier,
    RegexFastPath,
    detect_arrival,
    detect_counting,
)
from server.engine.intents import Slot, SlotValue, State
from server.engine.script_store import ScriptStore


# ── 台詞庫 ────────────────────────────────────────────────────
def test_script_loads_expected_ids(script):
    assert script.has("s0_open_c")
    assert script.text_of("s0_open_c").startswith("你好")
    assert script.faq_answer("faq_rescue_breath_ans") is not None


def test_canonical_order_preserved(script):
    ids = [l.id for l in script.canonical("s6")]
    # s6 起始三句依序
    assert ids[:3] == ["s6_start_c", "s6_start_rate_c", "s6_start_depth_c"]


def test_variant_rotation_no_repeat_within_round(script_path):
    """變體輪替：同一輪（池大小 N 次）內不重複。"""
    store = ScriptStore(script_path, rng=random.Random(0))
    # s3_consciousness_c 有 3 個變體 + canonical 本身 = 池大小 4
    qid = "s3_consciousness_c"
    n = 4
    first_round = [store.rotate_variant(qid).id for _ in range(n)]
    assert len(set(first_round)) == n  # 一輪內全不同
    # 第二輪重新洗牌，仍是同一組 id、仍不重複
    second_round = [store.rotate_variant(qid).id for _ in range(n)]
    assert len(set(second_round)) == n
    assert set(first_round) == set(second_round)


def test_insert_rotation_no_repeat_within_round(script_path):
    """S6 插播池輪替：一輪內不重複。"""
    store = ScriptStore(script_path, rng=random.Random(0))
    pool_size = len(store._insert_pool)
    assert pool_size >= 10  # s6 有 12 個 insert
    round1 = [store.rotate_insert().id for _ in range(pool_size)]
    assert len(set(round1)) == pool_size


def test_meta_rotation(script_path):
    store = ScriptStore(script_path, rng=random.Random(0))
    c = store.rotate_meta("clarify")
    assert c is not None and c.id.startswith("meta_clarify")
    assert store.rotate_meta("nonexistent") is None


def test_branch_line_lookup(script):
    line = script.branch_line("s1", "fire_truck")
    assert line is not None and line.id == "s1_fire_redirect_c"


# ── RegexFastPath：counting ──────────────────────────────────
def test_detect_counting_digits_and_xia():
    assert detect_counting("一下兩下三下")            # 中文數字+下
    assert detect_counting("1 2 3 4 5")               # 阿拉伯數字
    assert detect_counting("三十")                     # 純數字
    assert detect_counting("下巴")                     # 諧音「下」也算（寬鬆，S6 內可接受）
    assert not detect_counting("他沒有反應")           # 無數字無下


def test_detect_counting_strict_avoids_address():
    """strict 模式（S6 外）：門牌號等單一數字不誤判為數數。"""
    # 「五段七號」含數字，strict 下需雙 token 或「數字+下」；純地址不應算數數
    assert not detect_counting("信義路", strict=True)
    assert detect_counting("一下兩下", strict=True)     # 明確數數仍算


def test_fastpath_counting_in_s6_fills_slot():
    fp = RegexFastPath()
    res = fp.classify("一下兩下三下", State.S6)
    assert res.counting is True
    assert res.slots.get(Slot.COMPRESSIONS_STARTED) == SlotValue.YES


def test_fastpath_counting_outside_s6_strict():
    """S6 外用 strict：地址中的號碼不觸發 counting。"""
    fp = RegexFastPath()
    res = fp.classify("台北市信義路五段七號", State.S2)
    assert res.counting is False


# ── RegexFastPath：arrival ───────────────────────────────────
def test_detect_arrival_variants():
    assert detect_arrival("救護人員到了")
    assert detect_arrival("救護車來了")
    assert detect_arrival("消防人員到場了")
    assert detect_arrival("客戶人員到了")   # 諧音容錯（spike 實測「救護」→「客戶」）
    assert not detect_arrival("他還在喘")


def test_fastpath_arrival_sets_end_signal():
    fp = RegexFastPath()
    res = fp.classify("救護人員到了", State.S6)
    assert res.end_signal is True


# ── keyword 後備分類器（降級路徑）────────────────────────────
def test_keyword_ambulance():
    kw = KeywordFallbackClassifier()
    r = kw.classify("我要叫救護車", State.S0)
    assert r.slots.get(Slot.WANTS_AMBULANCE) == SlotValue.YES


def test_keyword_fire():
    kw = KeywordFallbackClassifier()
    r = kw.classify("我要消防車", State.S0)
    assert r.slots.get(Slot.WANTS_AMBULANCE) == SlotValue.NO


def test_keyword_consciousness_breathing_multislot():
    kw = KeywordFallbackClassifier()
    r = kw.classify("他叫不醒也沒有呼吸", State.S3)
    assert r.slots.get(Slot.CONSCIOUSNESS) == SlotValue.NO
    assert r.slots.get(Slot.BREATHING) == SlotValue.ABSENT


def test_keyword_agonal():
    kw = KeywordFallbackClassifier()
    r = kw.classify("他很久才喘一下", State.S4)
    assert r.slots.get(Slot.BREATHING) == SlotValue.AGONAL


def test_keyword_location_only_in_s2():
    kw = KeywordFallbackClassifier()
    r = kw.classify("台北市信義路一段", State.S2)
    assert r.slots.get(Slot.LOCATION) == SlotValue.PROVIDED


def test_keyword_unknown_returns_empty():
    kw = KeywordFallbackClassifier()
    r = kw.classify("今天天氣真好", State.S2)
    assert r.is_unknown


# ── STEP_DONE / 全局擺好 偵測（三分類器同步）──────────────────
def test_detect_step_done_phrases():
    from server.engine.fastpath import detect_step_done

    for p in ["再來呢", "然後呢", "接下來", "下一步", "好了", "跪好了", "做好了", "完成了"]:
        assert detect_step_done(p), p
    assert not detect_step_done("他沒有反應")


def test_detect_all_positioned_phrases():
    from server.engine.fastpath import detect_all_positioned

    for p in ["都擺好了", "手也放好了", "全部弄好了", "位置都好了", "都就位了"]:
        assert detect_all_positioned(p), p
    assert not detect_all_positioned("好了")  # 單步確認不算全局完成


def test_fastpath_step_done_shortcircuit():
    fp = RegexFastPath()
    r = fp.classify("再來呢", State.S5)
    assert r.step_done is True


def test_fastpath_all_positioned_fills_slot_in_s5():
    fp = RegexFastPath()
    r = fp.classify("我都擺好了手也放好了", State.S5)
    assert r.slots.get(Slot.POSITIONING_DONE) == SlotValue.YES
    assert r.step_done is False  # 全局完成優先於 step_done


def test_keyword_step_done():
    kw = KeywordFallbackClassifier()
    r = kw.classify("然後呢", State.S3)
    assert r.step_done is True


def test_keyword_all_positioned_in_s5():
    kw = KeywordFallbackClassifier()
    r = kw.classify("都擺好了", State.S5)
    assert r.slots.get(Slot.POSITIONING_DONE) == SlotValue.YES
