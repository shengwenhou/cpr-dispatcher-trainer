"""SessionRunner：文字模式全場 S0→S7、語音模式 half-duplex gate 三態、數數存活、
雙時鐘凍結、S6 插播由邏輯時鐘管轄（發聲不疊播）。全部用假 STT／假 player，不需麥克風。"""
from __future__ import annotations

import asyncio
import random

from server.config import load_config
from server.engine.fsm import DialogueEngine, EngineConfig
from server.engine.intents import State
from server.engine.metrics import EventType, MetricsRecorder
from server.engine.script_store import ScriptStore
from server.runtime import IntentPipeline
from server.session_runner import RunnerEventType, TextSessionRunner, VoiceSessionRunner

from .conftest import FakeClock
from .helpers import AMBULANCE, LOCATION, NO_BREATH, NO_CONSCIOUS, POSITIONED, intent


def _script():
    return ScriptStore(load_config().script_path, rng=random.Random(1234))


class FakeSTT:
    """假 STT：吐預排事件序列。多數測試不需真跑迴圈。"""

    def __init__(self, events=None):
        self._events = list(events or [])

    async def start(self):
        pass

    async def stop(self):
        pass

    async def events(self):
        for e in self._events:
            yield e


class FakePlayer:
    """假播放器：記錄動作、瞬間返回（不推進時鐘）、可 kill。"""

    def __init__(self):
        self.played = []
        self.killed = 0

    async def play(self, action, text_lookup=None):
        self.played.append(action)

    async def kill(self):
        self.killed += 1

    @property
    def is_playing(self):
        return False


def _voice_runner(clock=None, config=None):
    clock = clock or FakeClock()
    script = _script()
    metrics = MetricsRecorder(clock=clock)
    engine = DialogueEngine(script=script, metrics=metrics, config=config or EngineConfig(), rng=random.Random(1))
    runner = VoiceSessionRunner(
        engine=engine, pipeline=IntentPipeline(llm=None), metrics=metrics, script=script,
        now_fn=clock, stt=FakeSTT(), player=FakePlayer(), echo_tail_ms=0, echo_similarity_threshold=0.6,
    )
    return runner, engine, metrics, clock


def _drive_to_s6(engine, clock):
    engine.start()
    engine.on_utterance("要救護車", intent(AMBULANCE), now=clock.now())
    engine.on_utterance("台北市信義路", intent(LOCATION), now=clock.now())
    engine.on_utterance("叫不醒", intent(NO_CONSCIOUS), now=clock.now())
    engine.on_utterance("沒呼吸", intent(NO_BREATH), now=clock.now())
    engine.on_utterance("都擺好了", intent(POSITIONED), now=clock.now())
    assert engine.state == State.S6


# ── 文字模式：全場 S0→S7（無 LLM，走 fastpath＋keyword 降級）──────────
def test_text_runner_full_flow_s0_to_s7():
    clock = FakeClock()
    script = _script()
    metrics = MetricsRecorder(clock=clock)
    engine = DialogueEngine(script=script, metrics=metrics, config=EngineConfig(), rng=random.Random(7))
    runner = TextSessionRunner(engine=engine, pipeline=IntentPipeline(llm=None), metrics=metrics, script=script, now_fn=clock)

    async def go():
        await runner.start()
        assert engine.state == State.S0

        async def feed(t, dt=0.5):
            clock.advance(dt)
            await runner.submit_final(t)

        await feed("我要救護車"); assert engine.state == State.S2
        await feed("台北市大安區忠孝東路四段一號"); assert engine.state == State.S3
        await feed("他叫不醒，沒有反應"); assert engine.state == State.S4
        await feed("他沒有在呼吸"); assert engine.state == State.S5
        await feed("我跪好了，手也放好了"); assert engine.state == State.S6
        await feed("一下兩下三下四下")
        await feed("救護人員到了")

    asyncio.run(go())
    assert engine.finished
    s = metrics.summary()
    assert s["ohca_recognized_s"] is not None
    assert s["compression_start_s"] is not None
    assert s["ems_arrived_s"] is not None
    # tts_play 起訖有進 metrics 存證（裁決 3c）
    tts = [e for e in metrics.events if e.type == RunnerEventType.TTS_PLAY]
    assert any(e.data.get("phase") == "start" for e in tts)
    assert any(e.data.get("phase") == "end" for e in tts)


# ── 語音模式 half-duplex gate 三態（裁決 4）────────────────────────
def test_voice_gate_three_states():
    runner, engine, _, _ = _voice_runner()

    # 非 S6 硬 gate：發聲窗內一律丟棄
    engine.state = State.S3
    runner._freeze_begin()
    accept, reason = runner._classify_gate("他叫不醒沒有反應")
    assert accept is False and reason == "hard_gate"

    # S6 軟 gate：只認 counting / end_signal
    engine.state = State.S6
    assert runner._classify_gate("一下兩下三下")[0] is True          # 數數 bypass
    assert runner._classify_gate("救護人員到了")[0] is True           # 結束訊號 bypass
    ac, rc = runner._classify_gate("醫師我好緊張")
    assert ac is False and rc == "s6_gate_nonmatch"                   # 其餘丟棄
    runner._freeze_end()

    # echo 相似度（發聲窗外）：與剛播出插播高度相似 → 丟棄
    runner._last_spoken_text = "保持相同速度往下壓不要忽快忽慢"
    ac2, rc2 = runner._classify_gate("保持相同速度往下壓不要忽快忽慢")
    assert ac2 is False and rc2 == "echo_similarity"
    # 學員真正數數與插播文字不相似 → 不會被 echo 濾掉
    assert runner._classify_gate("一下兩下三下四下")[0] is True


def test_voice_gate_drop_is_recorded():
    runner, engine, metrics, _ = _voice_runner()
    engine.state = State.S3
    runner._freeze_begin()
    asyncio.run(runner.submit_final("這句話應該被硬 gate 擋下"))
    drops = [e for e in metrics.events if e.type == RunnerEventType.GATE_DROPPED]
    assert len(drops) == 1
    assert drops[0].data["reason"] == "hard_gate"
    assert drops[0].trigger_text == "這句話應該被硬 gate 擋下"


def test_s6_counting_survives_speaking_window():
    """S6 發聲窗（插播播放中）內，學員數數仍被引擎接收——起壓／持續壓胸不丟。"""
    runner, engine, metrics, clock = _voice_runner()
    _drive_to_s6(engine, clock)
    runner._freeze_begin()  # 模擬插播語音播放中
    asyncio.run(runner.submit_final("一下兩下三下"))
    comp = [e for e in metrics.events if e.type == EventType.COMPRESSION_START]
    assert len(comp) == 1
    # 同窗內非數數句被丟棄並記成 GATE_DROPPED
    asyncio.run(runner.submit_final("醫師我手好痠"))
    drops = [e for e in metrics.events if e.type == RunnerEventType.GATE_DROPPED]
    assert any(d.data["reason"] == "s6_gate_nonmatch" for d in drops)


# ── 雙時鐘：發聲窗內邏輯時鐘凍結（裁決 2）─────────────────────────
def test_dual_clock_freezes_during_speaking():
    runner, _, _, clock = _voice_runner()
    assert runner.logical_now() == 0.0
    clock.advance(2.0)
    assert runner.logical_now() == 2.0            # 未發聲：logical == real
    runner._freeze_begin()                        # 發聲開始 @ real=2
    clock.advance(3.0)                            # 播放 3s
    assert runner.logical_now() == 2.0            # 凍結
    runner._freeze_end()                          # real=5, 累計凍結 3s
    clock.advance(4.0)                            # 靜默 4s, real=9
    assert runner.logical_now() == 6.0            # 2 + 4（扣除 3s 發聲）


def test_s6_insert_governed_by_logical_clock():
    """S6 插播倒數用邏輯時鐘：長發聲窗不觸發插播；發聲結束後滿間隔才恰好一次。"""
    clock = FakeClock()
    script = _script()
    metrics = MetricsRecorder(clock=clock)
    engine = DialogueEngine(
        script=script, metrics=metrics,
        config=EngineConfig(s6_insert_min_s=15.0, s6_insert_max_s=15.0), rng=random.Random(1),
    )
    runner = VoiceSessionRunner(
        engine=engine, pipeline=IntentPipeline(llm=None), metrics=metrics, script=script,
        now_fn=clock, stt=FakeSTT(), player=FakePlayer(), echo_tail_ms=0,
    )
    _drive_to_s6(engine, clock)  # now=0 進 S6 → 下次插播排在 logical 15

    def n_inserts(actions):
        return sum(1 for x in actions if x.line_id and x.line_id.startswith("s6_encourage"))

    async def go():
        runner._freeze_begin()            # 插播語音播 20s（真實時間）
        clock.advance(20.0)
        a1 = await runner.tick_once()
        assert n_inserts(a1) == 0         # 邏輯凍結 → 不插播
        runner._freeze_end()
        clock.advance(15.0)               # 發聲後邏輯推進滿 15s
        a2 = await runner.tick_once()
        assert n_inserts(a2) == 1         # 恰好一次

    asyncio.run(go())


# ── 緊急中止：語音模式立刻 kill 播放子程序 ────────────────────────
def test_voice_abort_kills_player():
    runner, engine, _, clock = _voice_runner()
    _drive_to_s6(engine, clock)
    asyncio.run(runner.abort())
    assert runner.player.killed == 1
    assert runner._stopped is True


# ── 回歸：邏輯時鐘基準統一（真 monotonic 大絕對值）──────────────────
def test_dwell_relative_with_large_absolute_clock():
    """真時鐘情境：clock 從大的非零絕對值起跳（模擬 time.monotonic），驗證 summary 的 s0
    dwell 與各指標仍為正確的相對小值——而非 monotonic 絕對值量級（曾出現 s0≈124823 的 bug）。

    虛擬時鐘從 0 起跳的既有測試抓不到此坑（S0 進入基準恰為 ≈0）；本測試以大 offset 才能觸發。"""
    clock = FakeClock()
    clock.set(100000.0)  # 模擬本機 monotonic 絕對值量級
    script = _script()
    metrics = MetricsRecorder(clock=clock)  # metrics t0 = 100000
    engine = DialogueEngine(script=script, metrics=metrics, config=EngineConfig(), rng=random.Random(7))
    runner = TextSessionRunner(engine=engine, pipeline=IntentPipeline(llm=None), metrics=metrics, script=script, now_fn=clock)

    async def go():
        await runner.start()

        async def feed(t, dt=0.5):
            clock.advance(dt)
            await runner.submit_final(t)

        await feed("我要救護車")
        await feed("台北市大安區忠孝東路四段一號")
        await feed("他叫不醒，沒有反應")
        await feed("他沒有在呼吸")
        await feed("我跪好了，手也放好了")
        await feed("一下兩下三下四下")
        await feed("救護人員到了")

    asyncio.run(go())
    assert engine.finished
    s = metrics.summary()
    # S0 dwell 為相對小值（若基準未統一會爆成 ≈100000）
    assert s["state_dwell_s"]["s0"] < 5.0
    # 所有階段停留都是相對小值
    assert all(v < 100.0 for v in s["state_dwell_s"].values())
    # 關鍵指標維持相對正確（別改壞）
    assert 0.0 < s["ohca_recognized_s"] < 10.0
    assert 0.0 < s["compression_start_s"] < 10.0
    assert 0.0 < s["ems_arrived_s"] < 10.0


# ── 座標式 echo gate（合成 final 延後到達的 echo 防禦）────────────────
def test_voice_gate_echo_overlap_by_interval():
    """語音發生時段與播放時段重疊 → 無條件丟棄（含 S6 數數 pattern 的 echo）。"""
    runner, engine, _, _ = _voice_runner()
    # 模擬剛播完一句（牆鐘 100.0–106.5，含 tail）
    runner._play_intervals.append((100.0, 106.5))

    # 完全落在播放時段內的「開場白 echo」→ 丟（即使已離開發聲窗）
    engine.state = State.S1
    ac, rc = runner._classify_gate("請問你要消防車還是救護車", (101.0, 105.0))
    assert ac is False and rc == "echo_overlap"

    # S6：示範數數的 echo（內容像數數）也一樣被座標 gate 丟——時間戳不被 echo 污染
    engine.state = State.S6
    ac2, rc2 = runner._classify_gate("一下兩下三下", (101.0, 104.0))
    assert ac2 is False and rc2 == "echo_overlap"

    # 播放結束後才發生的真學員發言（零重疊）→ 放行
    engine.state = State.S1
    assert runner._classify_gate("我要救護車", (107.0, 108.5))[0] is True

    # 重疊不到一半（學員在播放尾端開口、大半在播放後）→ 放行
    assert runner._classify_gate("我要救護車", (106.0, 110.0))[0] is True

    # 文字模式（無座標）不受座標 gate 影響
    assert runner._classify_gate("我要救護車", None)[0] is True
