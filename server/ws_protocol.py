"""WebSocket 訊息協定：統一 envelope 與訊息型別。文字模式與語音模式共用同一套協定。

定案 envelope 格式（裁決 3a）：
    {"type": <str>, "session_id": <str|省略>, "payload": {...}}

i18n 紀律：
- 協定本身 language-neutral——payload 只帶資料與 id，不帶任何使用者可見的完成句／台詞。
- 派遣員台詞以 line_id＋text（text 來自 server 端已 locale 化的台詞庫）傳遞。
- error／degraded 的使用者可見文案一律以 message_key 傳遞（裁決 3b），前端查 i18n 資源檔，
  **不送中文原文**；細節欄位（如 detail）僅供除錯，非直接顯示字串。

本檔只定義編解碼與型別常數，無任何硬編碼的使用者可見字串。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MsgType(str, Enum):
    """雙向訊息型別。值為穩定字串，直接進 envelope 的 type 欄位。"""

    # server → client
    HELLO = "hello"                    # 連線建立；payload 帶 locale/scenario/active_session
    CLASS_CREATED = "class_created"    # 建課完成
    SESSION_STARTED = "session_started"  # 場次就緒
    TRANSCRIPT = "transcript"          # 即時逐字稿（kind: partial/final）
    STATE_CHANGE = "state_change"      # FSM 狀態變更（講師狀態指示）
    TTS_PLAY = "tts_play"              # 台詞播放起訖（event: start/end）
    METRIC = "metric"                  # 原始 metrics 事件串流
    SESSION_ENDED = "session_ended"    # 場次結束＋個人指標推送
    SNAPSHOT = "snapshot"              # resume 回應：重建畫面用快照
    CLASS_ENDED = "class_ended"        # 結課完成（資料就緒）
    ERROR = "error"                    # 錯誤（message_key）
    DEGRADED = "degraded"             # 服務降級，如 STT 故障（message_key）

    # client → server
    CREATE_CLASS = "create_class"      # 建課堂：scenario/locale/label?
    START_SESSION = "start_session"    # 開場次：student_alias/mode(voice|text)
    STUDENT_FINAL = "student_final"    # 文字模式送一句 final：text
    ABORT_SESSION = "abort_session"    # 緊急中止：reason?
    END_SESSION = "end_session"        # 手動結束場次
    RESUME = "resume"                  # 斷線重連（僅 session_id，裁決 6）
    END_CLASS = "end_class"            # 結束課堂


@dataclass
class Envelope:
    """一則 WS 訊息。session_id 可選（建課／hello 等場次前訊息可省略）。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "payload": self.payload}
        if self.session_id is not None:  # 省略 None，符合「session_id 可選」
            d["session_id"] = self.session_id
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Envelope":
        if not isinstance(d, dict) or "type" not in d:
            raise ValueError("invalid envelope: missing 'type'")
        raw_payload = d.get("payload")
        if raw_payload is None:
            payload: dict[str, Any] = {}
        elif isinstance(raw_payload, dict):
            payload = raw_payload
        else:  # payload 存在但非物件（如 list）→ 拒絕，不靜默吞掉
            raise ValueError("invalid envelope: 'payload' must be an object")
        return cls(type=str(d["type"]), payload=payload, session_id=d.get("session_id"))

    @classmethod
    def from_json(cls, s: str) -> "Envelope":
        return cls.from_dict(json.loads(s))


def make(type: "MsgType | str", payload: Optional[dict] = None, session_id: Optional[str] = None) -> Envelope:
    """建構一則訊息 envelope。"""
    t = type.value if isinstance(type, MsgType) else str(type)
    return Envelope(type=t, payload=dict(payload or {}), session_id=session_id)


def error(message_key: str, session_id: Optional[str] = None, **extra: Any) -> Envelope:
    """錯誤訊息：使用者可見文案以 message_key 傳遞（前端查 i18n），不含中文原文。"""
    payload: dict[str, Any] = {"message_key": message_key}
    payload.update(extra)
    return Envelope(type=MsgType.ERROR.value, payload=payload, session_id=session_id)


def degraded(message_key: str, session_id: Optional[str] = None, **extra: Any) -> Envelope:
    """服務降級訊息（如 STT helper 故障）：同樣以 message_key 傳遞。"""
    payload: dict[str, Any] = {"message_key": message_key}
    payload.update(extra)
    return Envelope(type=MsgType.DEGRADED.value, payload=payload, session_id=session_id)
