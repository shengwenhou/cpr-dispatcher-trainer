"""TTS Provider 實作：PrerecordedTTS（afplay 播預錄）＋ SayTTS（macOS say 後備）＋文字模式假物。

- PrerecordedTTS：speak(id) → afplay assets/audio/<locale>/<id>.wav。cache key 含 locale
  （SPEC 八之一：語音資產 cache key 含 locale 欄位）。speak_dynamic 委派給 fallback。
- SayTTS：speak_dynamic 用 macOS `say -v <voice>`（層 4 即時生成的後備，不需網路）。
- TextTTS：文字模式——不出聲，交由 driver 印出 id＋全文（此類只記錄，實際列印在 harness）。

播放走 subprocess（afplay/say）。同步介面 speak/speak_dynamic 會阻塞到播完；driver 若需
非阻塞可在 thread 執行，或用 async 版本 aspeak（本檔提供）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .base import TTSProvider


class SayTTS(TTSProvider):
    """macOS say 後備。主要用於 speak_dynamic（層 4）；speak(id) 無預錄時也可硬念（但正常不會）。"""

    def __init__(self, voice: str = "Meijia", text_lookup: Optional[Callable[[str], str]] = None) -> None:
        self.voice = voice
        self._text_lookup = text_lookup  # id → 全文（讓 speak(id) 也能念）

    def speak(self, utterance_id: str) -> None:
        text = self._text_lookup(utterance_id) if self._text_lookup else utterance_id
        self.speak_dynamic(text)

    def speak_dynamic(self, text: str) -> None:
        if not text:
            return
        subprocess.run(["say", "-v", self.voice, text], check=False)


class PrerecordedTTS(TTSProvider):
    """預錄音檔播放。speak(id) 走 afplay；speak_dynamic 委派 fallback（say）。"""

    def __init__(
        self,
        audio_dir: Path,
        locale: str,
        fallback: Optional[TTSProvider] = None,
        text_lookup: Optional[Callable[[str], str]] = None,
    ) -> None:
        # cache key 含 locale：實際路徑 <audio_dir>/<id>.wav，其中 audio_dir 已含 locale 層。
        self.audio_dir = Path(audio_dir)
        self.locale = locale
        self.fallback = fallback or SayTTS(text_lookup=text_lookup)
        self._text_lookup = text_lookup

    def _path_for(self, utterance_id: str) -> Path:
        return self.audio_dir / f"{utterance_id}.wav"

    def speak(self, utterance_id: str) -> None:
        path = self._path_for(utterance_id)
        if path.exists():
            subprocess.run(["afplay", str(path)], check=False)
        else:
            # 預錄缺檔：退回 say 念全文（保證不啞火），並讓 driver 有機會記錄缺檔
            text = self._text_lookup(utterance_id) if self._text_lookup else utterance_id
            self.fallback.speak_dynamic(text)

    def speak_dynamic(self, text: str) -> None:
        # 層 4 即時生成句無預錄 → 走 say 後備
        self.fallback.speak_dynamic(text)

    def has_audio(self, utterance_id: str) -> bool:
        return self._path_for(utterance_id).exists()


class TextTTS(TTSProvider):
    """文字模式假 TTS：不出聲，只把播放請求收集起來（實際列印由 harness 負責）。

    driver 直接讀 spoken 清單，或注入 on_speak callback 即時列印。"""

    def __init__(
        self,
        text_lookup: Optional[Callable[[str], str]] = None,
        on_speak: Optional[Callable[[str, str, bool], None]] = None,
    ) -> None:
        self._text_lookup = text_lookup
        self._on_speak = on_speak  # (id_or_marker, text, is_dynamic)
        self.spoken: list[tuple[str, str, bool]] = []

    def speak(self, utterance_id: str) -> None:
        text = self._text_lookup(utterance_id) if self._text_lookup else ""
        self.spoken.append((utterance_id, text, False))
        if self._on_speak:
            self._on_speak(utterance_id, text, False)

    def speak_dynamic(self, text: str) -> None:
        self.spoken.append(("<dynamic>", text, True))
        if self._on_speak:
            self._on_speak("<dynamic>", text, True)
