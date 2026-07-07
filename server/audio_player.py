"""語音模式播放器：async 驅動 afplay（預錄）／say（動態），可即時 kill（緊急中止）。

為何 driver 自帶播放器而非直接呼叫 TTSProvider：
- TTSProvider.speak/speak_dynamic 是 blocking（subprocess.run），會卡住 event loop，語音模式
  不可用。本播放器改以 asyncio.create_subprocess_exec 播放並 await 至播完，全程不卡 loop。
- 緊急中止鈕要求「立刻停聲」（裁決 5）→ kill() 殺掉當前子程序讓喇叭即時靜音。
- TTSProvider 不重寫：路徑由 config.audio_dir 組（<audio_dir>/<id>.wav，cache key 已含 locale）。

只有一支喇叭 → 一次只播一句（呼叫端序列化），故本類只追蹤單一當前子程序。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional

from .engine.actions import SpeakAction, SpeakKind


class AudioPlayer:
    def __init__(
        self,
        audio_dir: Path,
        say_voice: str = "Meijia",
        text_lookup: Optional[Callable[[str], str]] = None,
        player_cmd: str = "afplay",
        tts_cmd: str = "say",
    ) -> None:
        self.audio_dir = Path(audio_dir)
        self.say_voice = say_voice
        self._text_lookup = text_lookup
        self.player_cmd = player_cmd
        self.tts_cmd = tts_cmd
        self._proc: Optional[asyncio.subprocess.Process] = None

    def _path_for(self, line_id: str) -> Path:
        return self.audio_dir / f"{line_id}.wav"

    async def _run(self, *args: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._proc.wait()
        finally:
            self._proc = None

    async def _afplay(self, path: Path) -> None:
        await self._run(self.player_cmd, str(path))

    async def _say(self, text: str) -> None:
        if text:
            await self._run(self.tts_cmd, "-v", self.say_voice, text)

    async def play(self, action: SpeakAction, text_lookup: Optional[Callable[[str], str]] = None) -> None:
        """播一個 SpeakAction，await 至播完。缺預錄檔則退回 say 念全文（保證不啞火）。"""
        lookup = text_lookup or self._text_lookup
        if action.kind == SpeakKind.PRERECORDED and action.line_id:
            path = self._path_for(action.line_id)
            if path.exists():
                await self._afplay(path)
            else:
                await self._say(lookup(action.line_id) if lookup else action.line_id)
        elif action.kind == SpeakKind.DYNAMIC:
            await self._say(action.text or "")
        elif action.kind == SpeakKind.FILLER_THEN_DYNAMIC:
            if action.filler_id:
                fpath = self._path_for(action.filler_id)
                if fpath.exists():
                    await self._afplay(fpath)
                elif lookup:
                    await self._say(lookup(action.filler_id))
            await self._say(action.text or "")

    async def kill(self) -> None:
        """立即殺掉當前播放子程序（緊急中止）。無在播則 no-op。"""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        self._proc = None

    @property
    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None
