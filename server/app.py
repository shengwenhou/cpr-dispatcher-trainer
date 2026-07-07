"""FastAPI 課堂模式後端：health ＋ WebSocket 對話端點 ＋ 前端靜態檔。

- GET /health：存活探測，回報 locale／scenario／台詞庫是否載入。
- WS  /ws/classroom：課堂操作台。文字模式與語音模式共用同一協定（server/ws_protocol.py）。
  場次狀態存 server（Hub），不綁 WS 生命週期——瀏覽器誤刷新不毀進行中場次；重連帶 session_id
  取快照重建畫面（裁決 6）。
- 靜態前端：若 web/ 目錄存在則 mount 於 "/"；不存在也不影響 server 啟動（前端由另一 worker 產出）。

啟動：start.command 雙擊；或 uvicorn server.app:app --host $CPR_BIND_HOST --port $CPR_BIND_PORT。

i18n：本檔不送任何使用者可見中文字串；錯誤／降級一律以 message_key 傳遞（前端查 i18n 資源）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from . import ws_protocol as wsp
from .config import load_config
from .engine.script_store import ScriptStore
from .factory import build_text_runner, build_voice_runner
from .session_store import SessionStore

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles
except ImportError as e:  # FastAPI 未安裝時給清楚訊息（引擎與 harness 不依賴 web 亦可運作）
    raise ImportError(
        "需要 fastapi/uvicorn 才能啟動 web 骨架，請先 pip install -r requirements.txt。"
        "（引擎與文字模式 harness 不需 web 依賴即可運作。）"
    ) from e

app = FastAPI(title="CPR Dispatcher Trainer (classroom mode)")

_cfg = load_config()
_script_ok = False
_script_err = ""
try:
    _script = ScriptStore(_cfg.script_path)
    _script_ok = True
except Exception as e:  # 台詞庫載入失敗不應讓 health 直接 500，回報狀態即可
    _script_err = f"{type(e).__name__}: {e}"


@app.get("/health")
def health() -> dict:
    """存活探測。回報設定與台詞庫載入狀態，供課堂啟動前自檢。"""
    return {
        "status": "ok",
        "locale": _cfg.locale,
        "scenario": _cfg.scenario,
        "script_loaded": _script_ok,
        "script_error": _script_err or None,
        "stt_provider": _cfg.stt.provider,
        "llm_provider": _cfg.llm.provider,
        "tts_provider": _cfg.tts.provider,
    }


class Hub:
    """課堂執行期集中狀態：一台講師 Mac 同時最多一場 active（單麥克風／喇叭）。

    runner 與其背景 task 存於 server，橫跨 WS 連線生命週期——斷線不毀場次。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.store = SessionStore(cfg.server.data_root)
        self.runners: dict[str, Any] = {}               # session_id -> runner
        self.tasks: dict[str, list[asyncio.Task]] = {}  # session_id -> 背景 tasks
        self.class_id: Optional[str] = None             # 最近建立的課堂

    def active_session_id(self) -> Optional[str]:
        for sid, r in self.runners.items():
            if not getattr(r, "_stopped", False) and not r.engine.finished:
                return sid
        return None

    def cancel_tasks(self, session_id: str) -> None:
        for tsk in self.tasks.pop(session_id, []):
            tsk.cancel()


HUB = Hub(_cfg)


async def _pump(ws: "WebSocket", conn_q: "asyncio.Queue") -> None:
    """單一 writer：把連線出站佇列的訊息送到 WS（避免多協程同時寫同一 socket）。"""
    try:
        while True:
            msg = await conn_q.get()
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
    except (asyncio.CancelledError, WebSocketDisconnect):
        return
    except Exception:
        return


async def _forward(src_q: "asyncio.Queue", conn_q: "asyncio.Queue") -> None:
    """把 runner 出站佇列橋接到連線佇列（讓所有訊息經同一 writer）。"""
    try:
        while True:
            msg = await src_q.get()
            conn_q.put_nowait(msg)
    except asyncio.CancelledError:
        return


@app.websocket("/ws/classroom")
async def ws_session(ws: "WebSocket") -> None:
    await ws.accept()
    conn_q: "asyncio.Queue" = asyncio.Queue()
    writer = asyncio.create_task(_pump(ws, conn_q))
    forwarder: dict[str, Optional[asyncio.Task]] = {"t": None}

    def emit(env: wsp.Envelope) -> None:
        conn_q.put_nowait(env.to_dict())

    def bind_runner(session_id: str) -> None:
        """把某場次 runner 的出站串流橋接到本連線（連線／重連時呼叫）。"""
        if forwarder["t"] is not None:
            forwarder["t"].cancel()
        runner = HUB.runners.get(session_id)
        if runner is not None:
            forwarder["t"] = asyncio.create_task(_forward(runner.out_q, conn_q))

    # hello：帶目前 active_session（重連時前端據此送 resume）
    emit(wsp.make(wsp.MsgType.HELLO, payload={
        "locale": _cfg.locale, "scenario": _cfg.scenario,
        "active_session": HUB.active_session_id(),
    }))
    act = HUB.active_session_id()
    if act:
        bind_runner(act)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                env = wsp.Envelope.from_json(raw)
            except Exception:
                emit(wsp.error("bad_envelope"))
                continue
            await _handle(env, emit, bind_runner)
    except WebSocketDisconnect:
        pass
    finally:
        # 斷線只收連線相關 task；runner 與其背景 task 續留 server（裁決 6）
        writer.cancel()
        if forwarder["t"] is not None:
            forwarder["t"].cancel()


async def _handle(env: "wsp.Envelope", emit, bind_runner) -> None:
    t = env.type
    p = env.payload

    if t == wsp.MsgType.CREATE_CLASS.value:
        scenario = p.get("scenario", _cfg.scenario)
        locale = p.get("locale", _cfg.locale)
        cm = HUB.store.create_class(scenario, locale, label=p.get("label"))
        HUB.class_id = cm.class_id
        emit(wsp.make(wsp.MsgType.CLASS_CREATED, payload={
            "class_id": cm.class_id, "scenario": cm.scenario, "locale": cm.locale,
        }))
        return

    if t == wsp.MsgType.START_SESSION.value:
        class_id = p.get("class_id") or HUB.class_id
        if not class_id:
            emit(wsp.error("no_class"))
            return
        alias = p.get("student_alias", "")
        mode = p.get("mode", "text")
        meta = HUB.store.create_session(class_id, alias, mode)
        sid = meta.session_id
        if mode == "voice":
            runner = build_voice_runner(_cfg, HUB.store, class_id, sid)
        else:
            runner = build_text_runner(_cfg, HUB.store, class_id, sid)
        HUB.runners[sid] = runner
        bind_runner(sid)  # 先橋接，開場動作才會即時串流到前端
        emit(wsp.make(wsp.MsgType.SESSION_STARTED, payload={
            "student_alias": alias, "mode": mode, "state": runner.engine.state.value,
        }, session_id=sid))

        # 語音模式：先啟動 STT（results 消費先於 audio 已由 spike binary 內建保證）
        if mode == "voice":
            try:
                await runner.stt.start()
            except Exception as e:
                emit(wsp.degraded("stt_start_failed", session_id=sid, detail=str(e)))
                await runner.stop()
                return
            HUB.tasks.setdefault(sid, []).append(asyncio.create_task(runner.consume_stt()))

        await runner.start()  # 開場白
        HUB.tasks.setdefault(sid, []).append(asyncio.create_task(runner.tick_loop()))
        return

    if t == wsp.MsgType.STUDENT_FINAL.value:
        sid = env.session_id or HUB.active_session_id()
        runner = HUB.runners.get(sid) if sid else None
        if runner is not None:
            await runner.submit_final(p.get("text", ""))
        return

    if t == wsp.MsgType.ABORT_SESSION.value:
        sid = env.session_id or HUB.active_session_id()
        runner = HUB.runners.get(sid) if sid else None
        if runner is not None:
            await runner.abort()
            HUB.cancel_tasks(sid)
        return

    if t == wsp.MsgType.END_SESSION.value:
        sid = env.session_id or HUB.active_session_id()
        runner = HUB.runners.get(sid) if sid else None
        if runner is not None:
            await runner.stop()
            HUB.cancel_tasks(sid)
        return

    if t == wsp.MsgType.RESUME.value:
        sid = env.session_id
        runner = HUB.runners.get(sid) if sid else None
        if runner is not None:
            bind_runner(sid)
            snap = runner.snapshot()
            # snapshot 補回學員代號與模式（runner 不持有；前端重連後要能還原標頭）
            if HUB.class_id:
                try:
                    meta = HUB.store.load_session(HUB.class_id, sid)
                    snap.setdefault("student_alias", meta.student_alias)
                    snap.setdefault("mode", meta.mode)
                except Exception:
                    pass
            emit(wsp.make(wsp.MsgType.SNAPSHOT, payload=snap, session_id=sid))
        elif sid and HUB.class_id:
            try:
                snap = HUB.store.snapshot(HUB.class_id, sid)
                emit(wsp.make(wsp.MsgType.SNAPSHOT, payload=snap, session_id=sid))
            except Exception:
                emit(wsp.error("session_not_found", session_id=sid))
        else:
            emit(wsp.error("session_not_found", session_id=sid))
        return

    if t == wsp.MsgType.END_CLASS.value:
        class_id = p.get("class_id") or HUB.class_id
        if not class_id:
            emit(wsp.error("no_class"))
            return
        for sid, runner in list(HUB.runners.items()):
            if not getattr(runner, "_stopped", False) and not runner.engine.finished:
                await runner.stop()
                HUB.cancel_tasks(sid)
        cm = HUB.store.load_class(class_id)
        emit(wsp.make(wsp.MsgType.CLASS_ENDED, payload={
            "class_id": class_id, "students": cm.sessions,
        }))
        return

    emit(wsp.error("unknown_type", detail=t))


# ── 前端靜態檔（存在才 mount；不存在不影響 server 啟動）───────────────
if _cfg.server.web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_cfg.server.web_dir), html=True), name="web")
