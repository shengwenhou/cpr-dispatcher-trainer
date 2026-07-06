"""Provider 子套件：抽象介面與各實作。

工廠函式 build_* 依 config 選擇實作（切換＝改設定，不動呼叫端）。
"""
from .base import LLMProvider, STTEvent, STTEventType, STTProvider, TTSProvider

__all__ = [
    "LLMProvider",
    "STTProvider",
    "TTSProvider",
    "STTEvent",
    "STTEventType",
]
