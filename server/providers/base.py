"""三 Provider 抽象介面：STT／LLM／TTS。實作與介面分離，切換實作＝改設定一行。

- STTProvider：語音轉文字。start/stop 生命週期＋事件 async iterator（volatile/final/endpoint）。
- LLMProvider：意圖分類。classify_intent(text, state_context) → IntentResult。
- TTSProvider：語音輸出。speak(utterance_id) 播預錄；speak_dynamic(text) 即時生成（層 4）。

介面刻意最小化，讓文字模式假實作與真實作能無縫替換（driver 只依賴這些抽象）。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional

from ..engine.intents import IntentResult, State


# ── STT 事件（對齊 spike JSONL 契約）────────────────────────────
class STTEventType(str, Enum):
    VOLATILE = "volatile"    # 未定稿（即時）片段
    FINAL = "final"          # 定稿片段（送 FSM 的主要輸入）
    ENDPOINT = "endpoint"    # 語義斷句（reason: vad_silence / natural）
    STATUS = "status"        # 診斷（走 stderr；一般不進事件流，除錯時可觀察）
    ERROR = "error"          # helper 進程異常／退出


@dataclass
class STTEvent:
    """一筆 STT 事件。欄位對齊 spike README 的 JSONL 契約，缺項為 None。"""

    type: STTEventType
    text: Optional[str] = None
    t_wall_ms: Optional[int] = None
    audio_start: Optional[float] = None
    audio_end: Optional[float] = None
    reason: Optional[str] = None            # endpoint 用
    latency_since_audio_end_ms: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)


class STTProvider(abc.ABC):
    """STT 抽象介面。"""

    @abc.abstractmethod
    async def start(self) -> None:
        """啟動轉寫（真實作＝拉起 helper 子程序並就緒 results 消費）。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止並釋放資源（真實作＝SIGINT helper、等待收尾、防殭屍）。"""

    @abc.abstractmethod
    def events(self) -> AsyncIterator[STTEvent]:
        """事件 async iterator：逐筆吐 volatile/final/endpoint。"""


class LLMProvider(abc.ABC):
    """LLM 意圖分類抽象介面。"""

    @abc.abstractmethod
    def classify_intent(self, text: str, state: State, context: Optional[dict] = None) -> IntentResult:
        """把一句學員原句在給定狀態脈絡下分類為 IntentResult（多 slot＋信心＋FAQ／結束訊號）。

        同步介面：意圖分類延遲在延遲預算內（150–400ms），driver 可用 thread offload 避免阻塞
        event loop（見 runtime）。不可用時應回傳低信心／空結果，不得拋例外中斷對話。
        """

    @abc.abstractmethod
    def available(self) -> bool:
        """LLM 是否可用（認證／模型就緒）。不可用時 driver 走 RegexFastPath＋關鍵字降級。"""


class TTSProvider(abc.ABC):
    """TTS 抽象介面。"""

    @abc.abstractmethod
    def speak(self, utterance_id: str) -> None:
        """播放某台詞 id 的預錄音檔（cache key 含 locale）。"""

    @abc.abstractmethod
    def speak_dynamic(self, text: str) -> None:
        """即時生成並播放一段全文（層 4 後備；預錄無此句時用）。"""
