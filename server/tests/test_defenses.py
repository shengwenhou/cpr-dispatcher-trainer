"""五層防禦各層觸發測試（SPEC 四）。"""
from __future__ import annotations

from server.engine.intents import State
from server.engine.metrics import EventType
from .helpers import AMBULANCE, LOCATION, ids, intent


def _defense_layers(metrics):
    return [ev.data.get("layer") for ev in metrics.events if ev.type == EventType.DEFENSE]


def test_layer1_jump_is_normal(engine, metrics):
    """層 1 跳步＝正常能力，不記為 defense。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    assert 1 not in _defense_layers(metrics)  # 跳步不算防禦觸發


def test_layer2_clarify_then_takeover(engine, metrics):
    """層 2：首次 unknown→clarify；連續第 2 次→takeover＋bridge 重問。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))  # 到 S2

    a1 = engine.on_utterance("嗯嗯嗯", intent(confidence=0.0))  # unknown #1
    assert any(x.startswith("meta_clarify") for x in ids(a1))

    a2 = engine.on_utterance("啦啦啦", intent(confidence=0.0))  # unknown #2 → takeover
    got = ids(a2)
    assert any(x.startswith("meta_takeover") for x in got)
    assert any(x.startswith("meta_bridge") for x in got)  # bridge 前綴
    # 重問當前狀態（S2）問句（canonical 或變體）
    assert any(x.startswith("s2_addr") for x in got)

    layers = _defense_layers(metrics)
    assert layers.count(2) == 2


def test_layer2_streak_resets_on_success(engine, metrics):
    """成功推進後 unknown streak 歸零：下一次 unknown 又從 clarify 開始。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("嗯嗯", intent(confidence=0.0))       # unknown #1 → clarify
    engine.on_utterance("台北市信義路", intent(LOCATION))     # 成功推進 → streak 歸零
    a = engine.on_utterance("呃啊", intent(confidence=0.0))   # 又是 unknown #1 → clarify（非 takeover）
    assert any(x.startswith("meta_clarify") for x in ids(a))
    assert not any(x.startswith("meta_takeover") for x in ids(a))


def test_layer3_faq_answers_then_reasks(engine, metrics):
    """層 3：FAQ 命中 → 播答句 → bridge 重問當前狀態問句。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))  # S2
    engine.on_utterance("台北市信義路", intent(LOCATION))  # S3
    # 在 S3 插問「要不要做人工呼吸」
    a = engine.on_utterance("要不要做人工呼吸", intent(faq_id="faq_rescue_breath_ans"))
    got = ids(a)
    assert "faq_rescue_breath_ans" in got            # 播 FAQ 答句
    assert any(x.startswith("s3_consciousness") for x in got)  # 答完回到 S3 問句
    assert 3 in _defense_layers(metrics)
    # FAQ 不改變狀態
    assert engine.state == State.S3


def test_layer3_faq_resets_unknown_streak(engine, metrics):
    """FAQ 命中也歸零 unknown streak（FAQ 是有效互動）。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))
    engine.on_utterance("台北市", intent(LOCATION))
    engine.on_utterance("嗯嗯", intent(confidence=0.0))  # unknown #1
    engine.on_utterance("要壓多深", intent(faq_id="faq_depth_ans"))  # FAQ → 歸零
    a = engine.on_utterance("呃", intent(confidence=0.0))  # 應該又是 clarify（#1）
    assert any(x.startswith("meta_clarify") for x in ids(a))
    assert not any(x.startswith("meta_takeover") for x in ids(a))


def test_layer4_generation_with_filler(make_engine, metrics):
    """層 4：注入生成器 → unknown 有實質內容時，先 filler 再播生成短句，之後回當前問句。"""
    calls = []

    def fake_gen(utterance, question):
        calls.append((utterance, question))
        return "我懂你的擔心，我們先專心壓胸。"

    eng = make_engine(layer4_generator=fake_gen)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))  # S2
    a = eng.on_utterance("我好害怕他會不會死掉", intent(confidence=0.1))
    got = ids(a)
    # 先 filler（meta_filler_*）再 <dynamic>
    assert any(x.startswith("meta_filler") for x in got)
    assert "<dynamic>" in got
    # 生成器有被呼叫，且帶入當前問句
    assert len(calls) == 1
    assert calls[0][0] == "我好害怕他會不會死掉"
    # 記為層 4
    assert 4 in [ev.data.get("layer") for ev in metrics.events if ev.type == EventType.DEFENSE]


def test_layer4_falls_back_to_layer2_when_generation_fails(make_engine, metrics):
    """層 4 生成失敗（回 None）→ 降級為層 2 clarify。"""
    def failing_gen(utterance, question):
        return None

    eng = make_engine(layer4_generator=failing_gen)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    a = eng.on_utterance("那個那個", intent(confidence=0.0))
    got = ids(a)
    assert any(x.startswith("meta_clarify") for x in got)
    layers = [ev.data.get("layer") for ev in metrics.events if ev.type == EventType.DEFENSE]
    assert 2 in layers


def test_layer4_not_triggered_on_silence(make_engine, metrics):
    """空句（沉默）不走層 4（層 4 針對有實質內容的 unknown）。"""
    calls = []

    def gen(u, q):
        calls.append(u)
        return "x"

    eng = make_engine(layer4_generator=gen)
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    eng.on_utterance("", intent(confidence=0.0))  # 空句
    assert calls == []  # 未呼叫生成器


def test_layer5_tech_fault(engine, metrics):
    """層 5 技術故障句。"""
    engine.start()
    a = engine.tech_fault()
    assert "meta_tech_fault_01" in ids(a)
    layers = [ev.data.get("layer") for ev in metrics.events if ev.type == EventType.DEFENSE]
    assert 5 in layers


def test_layer5_timeout_two_levels(engine, metrics):
    """層 5 沉默分級 timeout：5s→l1、10s→l2，各一次。"""
    engine.start()
    engine.on_utterance("救護車", intent(AMBULANCE))  # 到 S2（turn-taking，會套用 timeout）
    # 尚未到門檻
    assert ids(engine.tick(3.0)) == []
    # 到 5s → l1
    a1 = engine.tick(5.0)
    assert any(x.startswith("meta_timeout_l1") for x in ids(a1))
    # 到 10s → l2
    a2 = engine.tick(10.0)
    assert any(x.startswith("meta_timeout_l2") for x in ids(a2))
    # 再 tick 不重播
    assert ids(engine.tick(12.0)) == []


def test_timeout_suppressed_in_s6(make_engine):
    """S6 壓胸階段不套用沉默 timeout（互動由插播計時器負責）。"""
    from .helpers import LOCATION, NO_BREATH, NO_CONSCIOUS, POSITIONED

    eng = make_engine()
    eng.start()
    eng.on_utterance("救護車", intent(AMBULANCE))
    eng.on_utterance("台北市", intent(LOCATION))
    eng.on_utterance("叫不醒", intent(NO_CONSCIOUS))
    eng.on_utterance("沒呼吸", intent(NO_BREATH))
    eng.on_utterance("好了", intent(POSITIONED))
    assert eng.state == State.S6
    # S6 內長時間沉默：不應觸發 timeout_l1/l2（只可能有插播）
    a = eng.tick(9.0)
    assert not any(x.startswith("meta_timeout") for x in ids(a))
