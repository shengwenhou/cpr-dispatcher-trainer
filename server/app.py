"""最小 FastAPI 骨架：health endpoint ＋ WebSocket endpoint 預留。

本階段重點在引擎，不在 web；此檔只提供未來課堂 UI（下階段）接入的骨架：
- GET /health：存活探測，回報 locale／scenario／台詞庫是否載入成功。
- WS  /ws/session：預留的對話 WebSocket。目前僅回 echo 與狀態，實際把前端接上 runtime
  的整合留待 UI 階段（SPEC 第九節第 4 項）。

啟動（開發）：uvicorn server.app:app --reload
"""
from __future__ import annotations

import json

from .config import load_config
from .engine.script_store import ScriptStore

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
except ImportError as e:  # FastAPI 未安裝時給清楚訊息（引擎與 harness 不依賴 web 亦可運作）
    raise ImportError(
        "需要 fastapi/uvicorn 才能啟動 web 骨架，請先 pip install -r requirements.txt。"
        "（引擎與文字模式 harness 不需 web 依賴即可運作。）"
    ) from e

app = FastAPI(title="CPR Dispatcher Trainer (engine skeleton)")

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


@app.websocket("/ws/session")
async def ws_session(ws: WebSocket) -> None:
    """預留的對話 WebSocket 骨架。

    目前僅示範連線與訊息往返；把 STT/引擎/TTS 串進來的完整整合留待 UI 階段。
    協定（暫定）：client 送 {"type":"text","text":...} 模擬一句 final，server 回目前狀態。
    """
    await ws.accept()
    await ws.send_text(json.dumps({"type": "hello", "locale": _cfg.locale, "scenario": _cfg.scenario}))
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                data = {"type": "raw", "text": msg}
            # 骨架：僅回 echo。實際引擎驅動於 UI 階段接入。
            await ws.send_text(json.dumps({"type": "echo", "received": data}, ensure_ascii=False))
    except WebSocketDisconnect:
        return
