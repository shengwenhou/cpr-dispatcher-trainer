"""SessionRunner：把引擎接到 WS 層。文字模式與語音模式共用同一顆引擎、同一 pipeline、同一
協定與持久化——差異只在「輸入來源」與「動作執行器」。VoiceDriver 的實質內容在此。

共用（SessionRunner 基底）
- 雙時鐘（裁決 2）：metrics 用真 monotonic（SPEC 第六節絕對時間正確）；餵給 engine 的
  on_utterance/tick 的 now 用「邏輯時鐘」＝真經過時間 − 累計發聲窗時長；發聲窗內凍結。
  → 播台詞不算沉默、S6 插播與 S5 auto-advance 從發聲結束才續走。driver 完全不碰引擎 private。
- metrics → WS/落地：每次引擎互動後 diff `metrics.events` 尾端，逐筆落地 JSONL ＋ 推 WS
  （generic metric ＋ 具名便利訊息 state_change/session_ended）。**不改 MetricsRecorder**。
- 分類編排：主協程同步跑 RegexFastPath 快篩；命中即用（S6 數數/結束/step_done/短答/slot），
  否則把 IntentPipeline.classify（內含 LLM blocking）offload 到 executor，不卡 event loop。

TextSessionRunner：輸入來自 WS student_final；不出聲，動作轉 WS tts_play；無發聲窗。
VoiceSessionRunner：輸入來自 STT 事件流；動作交 AudioPlayer 播放；half-duplex gate（裁決 4）。

並發正確性：單一 asyncio event loop，所有 engine.* 與 _drain_metrics 皆同步、內部無 await
→ 在協作式排程下各為原子操作，await 邊界之間狀態一致，故不需鎖。
"""
from __future__ import annotations

import asyncio
import difflib
import time
from enum import Enum
from typing import Any, Callable, Optional

from . import ws_protocol as wsp
from .engine.actions import SpeakAction, SpeakKind
from .engine.fastpath import RegexFastPath
from .engine.intents import State
from .engine.metrics import EventType


class RunnerEventType(str, Enum):
    """補充事件型別：與引擎 metrics 事件併存於同一 events list、同一 JSONL 事件流（存證用）。

    不修改 engine/metrics.py 的前提下擴充——MetricsRecorder.record 不驗證型別，
    MetricEvent.to_json 只取 .value，str-Enum 即完全相容（與 EventType 同機制）。
    """

    GATE_DROPPED = "gate_dropped"  # half-duplex 丟棄的 STT final（附原句與原因，debriefing 素材）
    TTS_PLAY = "tts_play"          # 實際播放起訖（有別於引擎 system_speak＝「意圖播出」）
    SESSION_FINALIZED = "session_finalized"  # 手動結束／中止的存證（data.status；自然完成走引擎 SESSION_END）
    STT_STATUS = "stt_status"      # STT helper 的 stderr 診斷（落地供事後排查，不推前端）
    STT_FINAL = "stt_final"        # STT 定稿逐字稿（含音訊座標與延遲；SPEC 第七節延遲分析＋echo 判定）


# 文字正規化用：比對 echo 相似度前剝除標點與空白。
_PUNCT = set("，。？！、～…,.?!~ 　「」『』（）()[]{};:；：·—-")


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in _PUNCT and not ch.isspace())


def _action_text(action: SpeakAction, script) -> str:
    """取動作對應的全文（供 tts_play payload 與 echo 比對）。"""
    if action.kind == SpeakKind.PRERECORDED and action.line_id and script is not None:
        try:
            return script.text_of(action.line_id)
        except Exception:
            return ""
    return action.text or ""


def _event_to_dict(ev) -> dict[str, Any]:
    tv = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
    return {"t_mono": ev.t_mono, "type": tv, "trigger_text": ev.trigger_text, "data": ev.data}


class SessionRunner:
    """執行期基底：持引擎＋pipeline＋metrics＋（可選）store＋出站佇列。"""

    def __init__(
        self,
        *,
        engine,
        pipeline,
        metrics,
        script=None,
        store=None,
        class_id: Optional[str] = None,
        session_id: Optional[str] = None,
        now_fn: Callable[[], float] = time.monotonic,
        out_queue: Optional["asyncio.Queue"] = None,
        echo_tail_ms: int = 400,
        echo_similarity_threshold: float = 0.6,
        tick_interval_ms: int = 250,
    ) -> None:
        self.engine = engine
        self.pipeline = pipeline
        self.metrics = metrics
        self.script = script if script is not None else getattr(engine, "script", None)
        self.store = store
        self.class_id = class_id
        self.session_id = session_id
        self._now = now_fn
        self.out_q: "asyncio.Queue" = out_queue if out_queue is not None else asyncio.Queue()
        self._fastpath = RegexFastPath()

        # 雙時鐘：發聲窗凍結。_freeze_start=None 表示非發聲中。
        self._frozen_accum = 0.0
        self._freeze_start: Optional[float] = None

        # metrics → WS/落地 游標
        self._emit_cursor = 0
        self._prev_state: Optional[str] = None

        # echo 相似度後備（雙保險）：僅在「剛結束發聲」的短暫 grace 窗內啟用，濾殘響漏過
        # tail 的自聽。_last_speak_end_real 恆為 -inf 直到首次發聲結束——文字模式永不發聲，
        # 故永不啟用（問句與其答句常共用關鍵詞，全域啟用會誤殺學員的合法回答）。
        self._last_spoken_text = ""
        self._last_speak_end_real = float("-inf")
        self.echo_tail_ms = echo_tail_ms
        self.echo_similarity_threshold = echo_similarity_threshold
        self._echo_grace_s = echo_tail_ms / 1000.0 + 0.5  # tail 之外再給 0.5s 殘響 grace
        self.tick_interval_ms = tick_interval_ms

        self._stopped = False

    # ── 雙時鐘 ───────────────────────────────────────────────
    def logical_now(self) -> float:
        """邏輯時鐘：距 session 開始的經過時間 − 累計發聲窗時長；發聲窗內凍結。餵給 engine 的 now。

        基準統一（關鍵）：真經過時間取自 `metrics.now()`（＝ clock − metrics 的 session t0，
        已相對化），與引擎 `start()` 中 S0 進入時間（同樣取 `metrics.now()`）共用同一 t0。
        若改用 `self._now()`（time.monotonic 絕對值）為基底，S0 的 STATE_EXIT dwell 會是
        「絕對值 − ≈0」而爆成 monotonic 量級（真時鐘才觸發；虛擬時鐘從 0 起跳所以測不到）。
        發聲凍結量以 `self._now()` 的差值累計（純時距，與基底無關），故兩者可安全相減。"""
        r = self.metrics.now()  # 相對化的真經過時間（與引擎 S0 基準同 t0）
        frozen = self._frozen_accum
        if self._freeze_start is not None:
            frozen += self._now() - self._freeze_start
        return r - frozen

    def _freeze_begin(self) -> None:
        if self._freeze_start is None:
            self._freeze_start = self._now()

    def _freeze_end(self) -> None:
        if self._freeze_start is not None:
            self._frozen_accum += self._now() - self._freeze_start
            self._freeze_start = None
            self._last_speak_end_real = self._now()  # echo grace 窗自此起算

    def _is_speaking(self) -> bool:
        return self._freeze_start is not None

    # ── 出站與落地 ───────────────────────────────────────────
    def _emit(self, env: wsp.Envelope) -> None:
        try:
            self.out_q.put_nowait(env.to_dict())
        except Exception:
            pass

    def _persist_line(self, line: str) -> None:
        if self.store is not None and self.class_id and self.session_id:
            try:
                self.store.append_event(self.class_id, self.session_id, line)
            except Exception:
                pass

    def _drain_metrics(self) -> None:
        """把新增的 metrics 事件：落地 JSONL ＋ 推 WS（generic metric ＋ 具名便利訊息）。

        同步、無 await → 協作式排程下為原子操作，多協程呼叫安全。"""
        events = self.metrics.events
        while self._emit_cursor < len(events):
            ev = events[self._emit_cursor]
            self._emit_cursor += 1
            self._persist_line(ev.to_json())
            self._emit(wsp.make(wsp.MsgType.METRIC, payload=_event_to_dict(ev), session_id=self.session_id))
            tv = ev.type.value if hasattr(ev.type, "value") else str(ev.type)
            if tv == EventType.STATE_ENTER.value:
                to_state = (ev.data or {}).get("state")
                self._emit(wsp.make(
                    wsp.MsgType.STATE_CHANGE,
                    payload={"from": self._prev_state, "to": to_state, "trigger_text": ev.trigger_text},
                    session_id=self.session_id,
                ))
                self._prev_state = to_state
            elif tv == EventType.SESSION_END.value:
                self._emit(wsp.make(
                    wsp.MsgType.SESSION_ENDED,
                    payload={"summary": self.metrics.summary()},
                    session_id=self.session_id,
                ))

    # ── half-duplex gate（裁決 4）───────────────────────────
    def _classify_gate(self, text: str) -> tuple[bool, Optional[str]]:
        """決定一筆 STT final 是否放行。回傳 (accept, drop_reason)。

        - 發聲窗內、非 S6：硬 gate，一律丟棄（系統說話時嚴格輪流，echo 風險 > 價值）。
        - 發聲窗內、S6：軟 gate，只認 counting／end_signal（fastpath），其餘丟棄（濾插播 echo；
          學員數數 pattern 與插播文字天然分離，數數不丟）。
        - 發聲窗外：放行；但與剛播出台詞高度相似者判為殘響 echo 丟棄（雙保險）。
        """
        if self._is_speaking():
            if self.engine.state == State.S6:
                fp = self._fastpath.classify(text, State.S6)
                if fp.counting or fp.end_signal:
                    return True, None
                return False, "s6_gate_nonmatch"
            return False, "hard_gate"
        if self._echo_similar(text):
            return False, "echo_similarity"
        return True, None

    def _echo_similar(self, text: str) -> bool:
        ref = self._last_spoken_text
        if not ref or not text:
            return False
        # 僅在剛結束發聲的 grace 窗內比對（文字模式永不發聲 → 恆不啟用）
        if (self._now() - self._last_speak_end_real) > self._echo_grace_s:
            return False
        a, b = _norm(text), _norm(ref)
        if len(a) < 4:  # 極短句不做相似度判斷，避免誤殺學員的短回應
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() >= self.echo_similarity_threshold

    def _record_gate_drop(self, text: str, reason: Optional[str]) -> None:
        self.metrics.record(
            RunnerEventType.GATE_DROPPED, trigger_text=text,
            reason=reason, state=self.engine.state.value,
        )
        self._drain_metrics()

    # ── 分類編排 ─────────────────────────────────────────────
    def _fp_hits(self, fp) -> bool:
        """fastpath 是否已足以定案（不需 LLM），與 IntentPipeline 短路條件一致。"""
        return bool(
            fp.end_signal
            or fp.step_done
            or (self.engine.state == State.S6 and fp.counting)
            or fp.slots
        )

    async def _classify(self, text: str, state: State):
        """主協程同步快篩；未命中則把整包 classify offload 到 executor（不卡 event loop）。"""
        fp = self._fastpath.classify(text, state)
        if self._fp_hits(fp):
            return fp
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.pipeline.classify, text, state)

    # ── 主輸入處理（共用）────────────────────────────────────
    async def submit_final(self, text: str) -> None:
        """消化一筆 final 文字（語音模式由 STT 協程呼叫；文字模式由 WS student_final 呼叫）。"""
        if self._stopped or self.engine.finished or not text:
            return
        accept, reason = self._classify_gate(text)
        if not accept:
            self._record_gate_drop(text, reason)
            return
        now = self.logical_now()
        result = await self._classify(text, self.engine.state)
        actions = self.engine.on_utterance(text, result, now=now)  # 同步、瞬間
        self._drain_metrics()
        await self._run_actions(actions)
        self._after_engine()

    # ── 時間推進（共用）──────────────────────────────────────
    async def tick_once(self) -> list[SpeakAction]:
        if self._stopped or self.engine.finished:
            return []
        actions = self.engine.tick(self.logical_now())
        self._drain_metrics()
        if actions:
            await self._run_actions(actions)
        return actions

    async def tick_loop(self) -> None:
        interval = self.tick_interval_ms / 1000.0
        while not self._stopped and not self.engine.finished:
            await asyncio.sleep(interval)
            await self.tick_once()

    # ── 生命週期 ─────────────────────────────────────────────
    async def start(self) -> None:
        actions = self.engine.start()
        self._drain_metrics()
        await self._run_actions(actions)

    def _after_engine(self) -> None:
        if self.engine.finished and not self._stopped:
            self._stopped = True
            self._finalize("completed")

    def _finalize(self, status: str) -> None:
        """收尾統一收口：存證事件＋落地 meta＋推前端回饋。

        自然完成（completed）的前端回饋由 _drain_metrics 對引擎 SESSION_END 推 session_ended，
        此處不重複；手動結束（ended）與中止（aborted）沒有引擎事件，必須在這裡補推——
        否則前端永遠不知道場次已死（實測踩過的坑）。"""
        if status in ("ended", "aborted"):
            self.metrics.record(RunnerEventType.SESSION_FINALIZED, status=status)
            self._drain_metrics()
        if self.store is not None and self.class_id and self.session_id:
            try:
                self.store.finalize_session(self.class_id, self.session_id, status, self.metrics.summary())
            except Exception:
                pass
        if status == "aborted":
            self._emit(wsp.make(
                wsp.MsgType.SESSION_ABORTED,
                payload={"summary": self.metrics.summary()}, session_id=self.session_id,
            ))
        elif status == "ended":
            # 講師提早收場：沿用 session_ended（前端顯示指標卡，未達節點為 null）
            self._emit(wsp.make(
                wsp.MsgType.SESSION_ENDED,
                payload={"summary": self.metrics.summary(), "manual": True}, session_id=self.session_id,
            ))

    async def abort(self) -> None:
        """緊急中止（文字模式：無聲可停）。"""
        if self._stopped:
            return
        self._stopped = True
        self._finalize("aborted")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if not self.engine.finished:
            self._finalize("ended")  # 講師手動提早收場＝正常結束（非中止）

    def snapshot(self) -> dict[str, Any]:
        """從記憶體產生場次快照（斷線重連用；活著的 runner 走此，死掉的走 store.snapshot）。"""
        filled = {s.value: v.value for s, v in self.engine.filled.items()}
        return {
            "session_id": self.session_id, "class_id": self.class_id,
            "state": self.engine.state.value, "filled": filled,
            "finished": self.engine.finished, "summary": self.metrics.summary(),
        }

    # ── 動作執行（子類實作）──────────────────────────────────
    async def _run_actions(self, actions: list[SpeakAction]) -> None:
        raise NotImplementedError

    def _emit_tts(self, action: SpeakAction, phase: str, text: str) -> None:
        """記 TTS_PLAY 事件（存證，裁決 3c）＋ 推 WS 具名 tts_play。start 與 end 皆呼叫。"""
        self.metrics.record(
            RunnerEventType.TTS_PLAY, line_id=action.line_id, layer=action.layer,
            kind=action.kind.value, phase=phase, text=text,
        )
        self._drain_metrics()
        self._emit(wsp.make(
            wsp.MsgType.TTS_PLAY,
            payload={"event": phase, "line_id": action.line_id, "text": text,
                     "layer": action.layer, "kind": action.kind.value},
            session_id=self.session_id,
        ))


class TextSessionRunner(SessionRunner):
    """文字模式：無麥克風／喇叭，開發與遠端測試用。動作轉 WS tts_play（起訖同刻記 metrics 存證）。

    無音訊 → 無發聲窗凍結（logical == real）；gate 恆放行。"""

    async def _run_actions(self, actions: list[SpeakAction]) -> None:
        for a in actions:
            text = _action_text(a, self.script)
            self._emit_tts(a, "start", text)
            self._emit_tts(a, "end", text)
            if text:
                self._last_spoken_text = text


class VoiceSessionRunner(SessionRunner):
    """語音模式：消費 STT 事件流 → gate → 分類 → 引擎；動作交 AudioPlayer 播放（發聲窗凍結）。"""

    def __init__(self, *, stt, player, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stt = stt
        self.player = player
        self.degraded = False

    async def _run_actions(self, actions: list[SpeakAction]) -> None:
        for a in actions:
            await self._speak(a)

    # 單句播放上限秒數：預錄句最長 <15s、say 動態句 <10s；超過即視為播放器卡死，
    # kill 止血——播放與 STT 消費在同一條 await 鏈上，放任卡死會癱瘓整場（實測教訓）。
    PLAY_TIMEOUT_S = 30.0

    async def _speak(self, action: SpeakAction) -> None:
        """播一句：發聲窗凍結邏輯時鐘 → afplay/say → 記起訖 → 尾端緩衝 → 解除凍結。

        end 事件與解凍以 finally 保證：緊急中止會 cancel 本協程（await 中拋 CancelledError），
        若不走 finally，「說話中」指示與凍結的邏輯時鐘會永遠掛著（實測踩過的坑）。
        echo tail 屬於發聲窗（殘響期間 gate 仍須生效），故 tail 在 finally 解凍之前等。"""
        text = _action_text(action, self.script)
        self._freeze_begin()
        self._emit_tts(action, "start", text)
        try:
            try:
                await asyncio.wait_for(
                    self.player.play(action, self.script.text_of if self.script is not None else None),
                    timeout=self.PLAY_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                await self.player.kill()  # 播放器卡死：止血，對話繼續
            except Exception:
                pass  # 播放失敗不可中斷對話（缺檔已由 player 退 say）；CancelledError 不在此列，照常傳播
            await self._tail_wait()  # 中止取消時跳過（例外傳播中），直接進 finally 收尾
        finally:
            self._emit_tts(action, "end", text)
            if text:
                self._last_spoken_text = text
            self._freeze_end()

    async def _tail_wait(self) -> None:
        t = self.echo_tail_ms / 1000.0
        if t > 0:
            await asyncio.sleep(t)

    # ── STT 事件消費 ─────────────────────────────────────────
    async def consume_stt(self) -> None:
        from .providers.base import STTEventType

        try:
            async for ev in self.stt.events():
                if self._stopped:
                    break
                if ev.type == STTEventType.VOLATILE and ev.text:
                    self._emit(wsp.make(
                        wsp.MsgType.TRANSCRIPT,
                        payload={"kind": "partial", "text": ev.text,
                                 "audio_start": ev.audio_start, "audio_end": ev.audio_end},
                        session_id=self.session_id,
                    ))
                elif ev.type == STTEventType.FINAL and ev.text:
                    # 逐字稿先落地＋推 WS（講師看見 STT 聽到什麼），再走 gate/引擎（可能被 gate 丟棄）。
                    # audio_start/audio_end 為該段語音在音訊軸上的座標——與 tts_play 起訖比對
                    # 即可鐵證判定「這筆是不是喇叭 echo」與辨識延遲（final 牆鐘 − audio_end）。
                    self.metrics.record(
                        RunnerEventType.STT_FINAL, text=ev.text,
                        audio_start=ev.audio_start, audio_end=ev.audio_end,
                        latency_ms=ev.latency_since_audio_end_ms,
                    )
                    self._drain_metrics()
                    self._emit(wsp.make(
                        wsp.MsgType.TRANSCRIPT,
                        payload={"kind": "final", "text": ev.text,
                                 "audio_start": ev.audio_start, "audio_end": ev.audio_end,
                                 "latency_ms": ev.latency_since_audio_end_ms},
                        session_id=self.session_id,
                    ))
                    await self.submit_final(ev.text)
                elif ev.type == STTEventType.STATUS:
                    # helper 的 stderr 診斷落地事件流（ready／asset／audio 啟動等），
                    # 不推前端；實測「STT 零事件」的排查全靠這個。
                    self.metrics.record(RunnerEventType.STT_STATUS, text=ev.text)
                    self._drain_metrics()
                elif ev.type == STTEventType.ERROR:
                    await self._on_stt_error(ev.raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._on_stt_error({"reason": "consumer_exception"})

    async def _on_stt_error(self, raw: Any) -> None:
        """STT helper 異常：層 5 技術故障句請講師介入，推 degraded（message_key）。"""
        self.degraded = True
        actions = self.engine.tech_fault()
        self._drain_metrics()
        self._emit(wsp.degraded("stt_helper_error", session_id=self.session_id, detail=raw))
        await self._run_actions(actions)

    async def abort(self) -> None:
        """緊急中止：立刻殺播放子程序讓喇叭靜音、停 STT、落地 aborted。"""
        if self._stopped:
            return
        self._stopped = True
        try:
            await self.player.kill()
        except Exception:
            pass
        try:
            await self.stt.stop()
        except Exception:
            pass
        self._finalize("aborted")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            await self.stt.stop()
        except Exception:
            pass
        if not self.engine.finished:
            self._finalize("ended")  # 講師手動提早收場＝正常結束（非中止）
