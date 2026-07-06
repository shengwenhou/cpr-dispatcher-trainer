"""Runtime 分類編排（fastpath 短路、LLM 降級）與 Provider 骨架測試。"""
from __future__ import annotations

from pathlib import Path

from server.engine.intents import IntentResult, Slot, SlotValue, State
from server.providers.base import LLMProvider
from server.providers.stt_speechanalyzer import SpeechAnalyzerSTT
from server.providers.tts import PrerecordedTTS, TextTTS
from server.runtime import IntentPipeline, execute_action
from server.engine.actions import SpeakAction, SpeakKind


class _RecordingLLM(LLMProvider):
    """記錄是否被呼叫的假 LLM。"""

    def __init__(self, result=None, avail=True):
        self.calls = 0
        self._result = result or IntentResult(source="llm", confidence=0.9)
        self._avail = avail

    def classify_intent(self, text, state, context=None):
        self.calls += 1
        return self._result

    def available(self):
        return self._avail


def test_pipeline_fastpath_shortcircuits_counting_in_s6():
    """S6 數數 → fastpath 直接回傳，不呼叫 LLM（延遲關鍵路徑）。"""
    llm = _RecordingLLM()
    pipe = IntentPipeline(llm=llm)
    res = pipe.classify("一下兩下三下", State.S6)
    assert res.counting is True
    assert llm.calls == 0  # 未呼叫 LLM


def test_pipeline_fastpath_shortcircuits_arrival():
    llm = _RecordingLLM()
    pipe = IntentPipeline(llm=llm)
    res = pipe.classify("救護人員到了", State.S6)
    assert res.end_signal is True
    assert llm.calls == 0


def test_pipeline_uses_llm_when_available():
    result = IntentResult(source="llm", confidence=0.9)
    result.slots[Slot.WANTS_AMBULANCE] = SlotValue.YES
    llm = _RecordingLLM(result=result)
    pipe = IntentPipeline(llm=llm)
    res = pipe.classify("我要救護車", State.S0)
    assert llm.calls == 1
    assert res.slots.get(Slot.WANTS_AMBULANCE) == SlotValue.YES


def test_pipeline_degrades_to_keyword_when_llm_unavailable():
    """LLM 不可用 → keyword 後備。"""
    llm = _RecordingLLM(avail=False)
    pipe = IntentPipeline(llm=llm)
    res = pipe.classify("我要救護車", State.S0)
    assert llm.calls == 0  # 不可用不呼叫
    assert res.source == "keyword_fallback"
    assert res.slots.get(Slot.WANTS_AMBULANCE) == SlotValue.YES


def test_pipeline_no_llm_uses_keyword():
    pipe = IntentPipeline(llm=None)
    res = pipe.classify("他叫不醒沒呼吸", State.S3)
    assert res.source == "keyword_fallback"
    assert res.slots.get(Slot.CONSCIOUSNESS) == SlotValue.NO


def test_execute_action_prerecorded_and_dynamic():
    """execute_action 把動作正確派給 TTS。"""
    spoken = []
    tts = TextTTS(text_lookup=lambda i: f"文本-{i}", on_speak=lambda m, t, d: spoken.append((m, d)))
    execute_action(SpeakAction(kind=SpeakKind.PRERECORDED, line_id="x"), tts, lambda i: "")
    execute_action(
        SpeakAction(kind=SpeakKind.FILLER_THEN_DYNAMIC, filler_id="f", text="即時句"), tts, lambda i: ""
    )
    kinds = [d for _, d in spoken]
    assert kinds == [False, False, True]  # 預錄x1、filler(預錄)x1、dynamicx1


def test_prerecorded_tts_falls_back_when_missing(tmp_path):
    """預錄缺檔 → 走 fallback（此處用假 fallback 記錄）。"""
    calls = []

    class FakeFallback:
        def speak(self, i): calls.append(("speak", i))
        def speak_dynamic(self, t): calls.append(("dyn", t))

    tts = PrerecordedTTS(
        audio_dir=tmp_path, locale="zh-TW", fallback=FakeFallback(), text_lookup=lambda i: "全文"
    )
    tts.speak("不存在的id")
    assert calls == [("dyn", "全文")]  # 缺檔 → say 念全文


def test_prerecorded_tts_uses_real_audio_if_present():
    """若 assets 有真音檔，has_audio 應為 True（不實際播放）。"""
    from server.config import load_config

    cfg = load_config()
    tts = PrerecordedTTS(audio_dir=cfg.audio_dir, locale=cfg.locale, text_lookup=lambda i: "")
    # s0_open_c.wav 應存在（TTS 批次已入庫）
    assert tts.has_audio("s0_open_c") is True


def test_stt_provider_missing_helper_raises(tmp_path):
    """STT helper 不存在 → start() 拋 FileNotFoundError（driver 據此走文字模式/提示）。"""
    import asyncio

    stt = SpeechAnalyzerSTT(helper_path=tmp_path / "nonexistent_helper")

    async def _run():
        try:
            await stt.start()
            return "no-error"
        except FileNotFoundError:
            return "file-not-found"

    assert asyncio.run(_run()) == "file-not-found"


def test_stt_parse_line_events():
    """JSONL 行解析為 STTEvent（對齊 spike 契約）。"""
    from server.providers.base import STTEventType

    p = SpeechAnalyzerSTT._parse_line
    fin = p('{"type":"final","text":"沒有呼吸","t_wall_ms":1200,"audio_start":0.5,"audio_end":1.1}')
    assert fin.type == STTEventType.FINAL and fin.text == "沒有呼吸" and fin.t_wall_ms == 1200
    ep = p('{"type":"endpoint","reason":"vad_silence","t_wall_ms":1500}')
    assert ep.type == STTEventType.ENDPOINT and ep.reason == "vad_silence"
    vol = p('{"type":"volatile","text":"沒有","t_wall_ms":1000}')
    assert vol.type == STTEventType.VOLATILE
    assert p("not json") is None
    assert p('{"type":"unknown"}') is None
