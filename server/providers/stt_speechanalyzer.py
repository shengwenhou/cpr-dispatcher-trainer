"""SpeechAnalyzerSTT：以 subprocess 驅動 spike/stt_spike binary，消費其 stdout JSONL 事件流。

職責分工（重要）：
- spike binary 內部已處理 PROGRESS「六項工程發現」中屬於 SDK 管線的部分：不可依賴自然
  finalize（週期 flush＋VAD）、results 消費迴圈就緒順序、音訊格式轉換保證、SIGINT 收尾。
  → 本 Provider **不重新發明**這些；只負責「正確消費事件流」與「管理進程生命週期」。
- 進程生命週期：啟動（帶 --locale/--silence-ms/--flush-ms）、逐行解析 stdout JSONL、
  收尾時送 SIGINT 讓 helper 走它內建的優雅收尾（spike 保證 3s 內硬退出），
  有界等待後仍未退則 SIGKILL；一律 wait() 回收，防殭屍。

stderr（status/診斷）另開 drain task 吞掉避免管線塞滿；需要時可轉為 STATUS 事件觀察。

本 Provider 只在 macOS + 已編譯 helper + 有 GUI session（麥克風 TCC）時能真正出事件；
無這些條件時 start() 會拋清楚錯誤，driver 據此走文字模式或提示。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import AsyncIterator, Optional

from .base import STTEvent, STTEventType, STTProvider


class SpeechAnalyzerSTT(STTProvider):
    def __init__(
        self,
        helper_path: Path,
        locale: str = "zh_TW",
        silence_ms: int = 600,
        flush_ms: int = 700,
        shutdown_grace_s: float = 5.0,
        emit_status: bool = False,
        dump_path: Optional[Path] = None,
    ) -> None:
        self.helper_path = Path(helper_path)
        self.locale = locale
        self.silence_ms = silence_ms
        self.flush_ms = flush_ms
        self.shutdown_grace_s = shutdown_grace_s
        self.emit_status = emit_status
        # 診斷用：把 analyzer 實際收到的音訊落地 WAV（--dump-audio），事後可 --wav 回餵驗證轉換品質
        self.dump_path = Path(dump_path) if dump_path else None

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._queue: asyncio.Queue[Optional[STTEvent]] = asyncio.Queue()
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── 生命週期 ──────────────────────────────────────────────
    async def start(self) -> None:
        if not self.helper_path.exists():
            raise FileNotFoundError(
                f"STT helper 不存在：{self.helper_path}（請先於 spike/ 執行 build.sh 編譯 stt_spike）"
            )
        if not os.access(self.helper_path, os.X_OK):
            raise PermissionError(f"STT helper 不可執行：{self.helper_path}")

        # live 模式（無 --wav）：麥克風擷取。參數化 locale／VAD／flush（i18n 紀律、可調）。
        args = [
            str(self.helper_path),
            "--locale", self.locale,
            "--silence-ms", str(self.silence_ms),
            "--flush-ms", str(self.flush_ms),
        ]
        if self.dump_path is not None:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            args += ["--dump-audio", str(self.dump_path)]
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._drain_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        """優雅收尾：送 SIGINT（觸發 helper 內建收尾）→ 有界等待 → 仍未退則 SIGKILL → wait 回收。"""
        self._stopping = True
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)  # helper 用 DispatchSource 接 SIGINT 優雅收尾
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.shutdown_grace_s)
            except asyncio.TimeoutError:
                # helper 內建 3s 硬退出仍失效的極端情況：強制殺
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        # 收掉 reader tasks
        for t in (self._stdout_task, self._stderr_task):
            if t is not None:
                t.cancel()
        # 事件流終止哨兵
        await self._queue.put(None)

    # ── 事件流 ────────────────────────────────────────────────
    async def events(self) -> AsyncIterator[STTEvent]:
        """逐筆吐事件，直到收到終止哨兵（None）。"""
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    # ── 內部：stdout JSONL 解析 ───────────────────────────────
    async def _drain_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                line = await stream.readline()
                if not line:  # EOF：helper 退出
                    break
                ev = self._parse_line(line.decode("utf-8", errors="replace").strip())
                if ev is not None:
                    await self._queue.put(ev)
        except asyncio.CancelledError:
            pass
        finally:
            # helper 進程結束：若非主動 stop，補一筆 ERROR 供 driver 走層 5 技術故障
            if not self._stopping:
                rc = self._proc.returncode if self._proc else None
                await self._queue.put(
                    STTEvent(type=STTEventType.ERROR, raw={"reason": "helper_exited", "returncode": rc})
                )
            await self._queue.put(None)

    async def _drain_stderr(self) -> None:
        """吞掉 stderr（status 診斷）避免管線塞滿；emit_status 時轉 STATUS 事件。"""
        assert self._proc is not None and self._proc.stderr is not None
        stream = self._proc.stderr
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                if self.emit_status:
                    txt = line.decode("utf-8", errors="replace").strip()
                    await self._queue.put(STTEvent(type=STTEventType.STATUS, text=txt))
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _parse_line(line: str) -> Optional[STTEvent]:
        """把一行 JSONL 轉成 STTEvent。非 JSON 或缺 type 一律忽略（穩健性優先）。"""
        if not line:
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        t = d.get("type")
        if t == "volatile":
            return STTEvent(
                type=STTEventType.VOLATILE,
                text=d.get("text"),
                t_wall_ms=d.get("t_wall_ms"),
                audio_start=d.get("audio_start"),
                audio_end=d.get("audio_end"),
                raw=d,
            )
        if t == "final":
            return STTEvent(
                type=STTEventType.FINAL,
                text=d.get("text"),
                t_wall_ms=d.get("t_wall_ms"),
                audio_start=d.get("audio_start"),
                audio_end=d.get("audio_end"),
                latency_since_audio_end_ms=d.get("latency_since_audio_end_ms"),
                raw=d,
            )
        if t == "endpoint":
            return STTEvent(
                type=STTEventType.ENDPOINT,
                reason=d.get("reason"),
                t_wall_ms=d.get("t_wall_ms"),
                raw=d,
            )
        if t == "status":
            return STTEvent(type=STTEventType.STATUS, text=d.get("msg"), t_wall_ms=d.get("t_wall_ms"), raw=d)
        return None
