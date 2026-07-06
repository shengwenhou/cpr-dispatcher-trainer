"""Runtime driver：把 Provider 接到 DialogueEngine。文字模式與語音模式共用同一顆引擎。

職責：
- 分類編排（延遲關鍵路徑）：對每筆 final 輸入，**先跑 RegexFastPath**；若 fastpath 已解決
  （S6 數數／結束訊號），可不呼叫 LLM（省往返）。否則走 LLM 分類；LLM 不可用／逾時 →
  keyword 後備分類（降級路徑），保證文字模式仍跑得完。
- 執行動作：把引擎回傳的 SpeakAction 交 TTS（預錄 / 動態 / filler+動態）。
- 時間推進：週期呼叫 engine.tick(now) 驅動 S6 插播與沉默 timeout。

TextModeDriver：同步、決定性，供 harness 與 pytest 使用（stdin→final，stdout→台詞）。
VoiceDriver：async，消費 STTProvider 事件流；本階段能啟動即可，真語音整測留待 UI 階段。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .engine.actions import SpeakAction, SpeakKind
from .engine.fastpath import KeywordFallbackClassifier, RegexFastPath
from .engine.fsm import DialogueEngine
from .engine.intents import IntentResult, State
from .providers.base import LLMProvider, TTSProvider


class IntentPipeline:
    """分類編排：fastpath → LLM →（降級）keyword。回傳送進引擎的 IntentResult。

    引擎內部也會再跑一次 fastpath（雙保險），故此處 fastpath 主要目的是「決定要不要呼叫 LLM」。
    """

    def __init__(self, llm: Optional[LLMProvider] = None) -> None:
        self.llm = llm
        self._fastpath = RegexFastPath()
        self._keyword = KeywordFallbackClassifier()

    def classify(self, text: str, state: State) -> IntentResult:
        # 1) fastpath：S6 數數／結束訊號／請求下一步／S5 全部擺好 直接短路，不勞 LLM
        #    （延遲關鍵路徑）。「再來呢／好了」等超短句必然高頻，不必花 LLM 往返。
        fp = self._fastpath.classify(text, state)
        if (
            fp.end_signal
            or fp.step_done
            or (state == State.S6 and fp.counting)
            or fp.slots  # S5 全部擺好 → 帶 POSITIONING_DONE slot
        ):
            return fp

        # 2) LLM（可用時）
        if self.llm is not None and self.llm.available():
            res = self.llm.classify_intent(text, state)
            # LLM 回了東西（有 slot／faq／結束訊號／請求下一步／夠信心）就用它
            if not res.is_unknown or res.confidence > 0:
                # 合併 fastpath 的弱訊號（S6 外 counting、step_done）
                if fp.counting:
                    res.counting = True
                if fp.step_done:
                    res.step_done = True
                return res

        # 3) 降級：keyword 後備
        kw = self._keyword.classify(text, state)
        if fp.counting:
            kw.counting = True
        if fp.end_signal:
            kw.end_signal = True
        if fp.step_done:
            kw.step_done = True
        return kw


def execute_action(action: SpeakAction, tts: TTSProvider, text_of: Callable[[str], str]) -> None:
    """把單一 SpeakAction 交給 TTS 執行。"""
    if action.kind == SpeakKind.PRERECORDED and action.line_id:
        tts.speak(action.line_id)
    elif action.kind == SpeakKind.DYNAMIC and action.text:
        tts.speak_dynamic(action.text)
    elif action.kind == SpeakKind.FILLER_THEN_DYNAMIC:
        if action.filler_id:
            tts.speak(action.filler_id)  # 先播 filler 掩飾延遲
        if action.text:
            tts.speak_dynamic(action.text)


@dataclass
class TextModeDriver:
    """文字模式 driver：同步驅動。輸入為「已是 final 的文字」，輸出為 SpeakAction（由呼叫端列印）。

    決定性：時間由外部注入（feed 的 now 參數），不依賴真時鐘 → pytest 可精確控時。
    """

    engine: DialogueEngine
    pipeline: IntentPipeline
    tts: TTSProvider
    text_of: Callable[[str], str]

    def start(self) -> list[SpeakAction]:
        actions = self.engine.start()
        self._run(actions)
        return actions

    def feed(self, text: str, now: Optional[float] = None) -> list[SpeakAction]:
        """餵一句 final 文字。回傳引擎產生的動作（已執行 TTS）。"""
        result = self.pipeline.classify(text, self.engine.state)
        actions = self.engine.on_utterance(text, result, now=now)
        self._run(actions)
        return actions

    def tick(self, now: float) -> list[SpeakAction]:
        actions = self.engine.tick(now)
        self._run(actions)
        return actions

    def _run(self, actions: list[SpeakAction]) -> None:
        for a in actions:
            execute_action(a, self.tts, self.text_of)
