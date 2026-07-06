"""Metrics 事件流：monotonic 時間戳、每筆附觸發原句，可序列化為 JSON lines。

對應 SPEC 第六節 Time Metrics：
- 辨識 OHCA 時間 = 進入 S5 時刻 − 通話開始
- 開始按壓時間   = S6 首次偵測數數 − 通話開始
- EMS 抵達時間   = S7 觸發 − 通話開始
- 各階段停留時間 = 逐狀態
- 對話占比       = 學員／系統／沉默時間

設計：
- 時間戳一律用 monotonic clock（time.monotonic()），單位秒；避免掛鐘回撥污染量測。
- 事件流是引擎輸出的事實紀錄，下階段報告（Word／Excel／dashboard）以此為單一資料來源。
- 本模組不做臨床判讀，只忠實記事並算衍生指標；台詞內容一律以 id 表示（不落原文於此，
  播放全文由 TTS 事件另記），使用者可見字串不在此 hardcode。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class EventType(str, Enum):
    """事件種類。值為穩定字串，直接序列化進 JSONL，下階段報告依此欄位分流。"""

    SESSION_START = "session_start"        # 通話開始（t0 基準）
    STATE_ENTER = "state_enter"            # 進入某狀態
    STATE_EXIT = "state_exit"              # 離開某狀態（附停留秒數）
    UTTERANCE_IN = "utterance_in"          # 收到一筆學員輸入（final）
    INTENT = "intent"                      # 意圖分類結果（含來源：llm／regex_fastpath／keyword_fallback）
    SYSTEM_SPEAK = "system_speak"          # 系統播出一句台詞（附 id 與層級）
    SLOT_FILL = "slot_fill"                # 某 slot 被填（附值與來源句）
    DEFENSE = "defense"                    # 五層防禦觸發（附 layer 與細節）
    COMPRESSION_START = "compression_start"  # ★S6 首次偵測數數（起壓時間戳）
    OHCA_RECOGNIZED = "ohca_recognized"    # ★進入 S5（辨識 OHCA 時間戳）
    S5_SUBSTEP = "s5_substep"              # S5 擺位子步驟播放（附 step 名與推進方式 confirmed/auto/skipped）
    EMS_ARRIVED = "ems_arrived"            # ★S7 觸發（EMS 抵達）
    TIMEOUT = "timeout"                    # 沉默分級 timeout 觸發
    SESSION_END = "session_end"            # 通話結束


@dataclass
class MetricEvent:
    """單筆事件。

    t_mono: 自 session 建立起算的相對秒數（monotonic，單調遞增）。
    type:   事件種類。
    trigger_text: 觸發本事件的原句（SPEC「每筆事件附觸發原句」，可回溯）；系統事件可為 None。
    data:   事件細節（state 名、slot 名、utterance_id、layer 等），任意可 JSON 化 dict。
    """

    t_mono: float
    type: EventType
    trigger_text: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        d["type"] = self.type.value
        return json.dumps(d, ensure_ascii=False)


class MetricsRecorder:
    """事件記錄器。持有 session 起始 monotonic 基準，統一產出相對時間戳。

    clock 可注入（測試用假時鐘），預設 time.monotonic。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._t0 = clock()
        self.events: list[MetricEvent] = []

    def now(self) -> float:
        """自 session 開始的相對秒數。"""
        return self._clock() - self._t0

    def record(
        self,
        type: EventType,
        trigger_text: Optional[str] = None,
        **data: Any,
    ) -> MetricEvent:
        ev = MetricEvent(t_mono=round(self.now(), 4), type=type, trigger_text=trigger_text, data=data)
        self.events.append(ev)
        return ev

    # ── 序列化 ────────────────────────────────────────────────
    def to_jsonl(self) -> str:
        """整段事件流序列化為 JSON lines（供下階段報告消費）。"""
        return "\n".join(ev.to_json() for ev in self.events)

    def dump_jsonl(self, path) -> None:
        from pathlib import Path

        Path(path).write_text(self.to_jsonl() + "\n", encoding="utf-8")

    # ── 衍生指標（SPEC 第六節）────────────────────────────────
    def _first_time(self, type: EventType) -> Optional[float]:
        for ev in self.events:
            if ev.type == type:
                return ev.t_mono
        return None

    def summary(self) -> dict[str, Any]:
        """計算 SPEC 第六節關鍵指標與各狀態停留時間。

        回傳的時間一律為「距通話開始的秒數」（None 表該節點未發生）。
        各狀態停留時間由 STATE_EXIT 事件的 data['dwell_s'] 匯總。
        對話占比由 UTTERANCE_IN／SYSTEM_SPEAK／沉默估算（見下）。
        """
        ohca_s = self._first_time(EventType.OHCA_RECOGNIZED)
        compression_s = self._first_time(EventType.COMPRESSION_START)
        ems_s = self._first_time(EventType.EMS_ARRIVED)
        end_s = self._first_time(EventType.SESSION_END)

        # 各狀態停留時間（可能同一狀態多次進出，累加）
        dwell: dict[str, float] = {}
        for ev in self.events:
            if ev.type == EventType.STATE_EXIT:
                st = ev.data.get("state")
                dwell[st] = dwell.get(st, 0.0) + float(ev.data.get("dwell_s", 0.0))

        # 五層防禦觸發統計（debriefing 素材）
        defense_counts: dict[str, int] = {}
        for ev in self.events:
            if ev.type == EventType.DEFENSE:
                layer = str(ev.data.get("layer"))
                defense_counts[layer] = defense_counts.get(layer, 0) + 1

        return {
            "ohca_recognized_s": ohca_s,          # 辨識 OHCA 時間
            "compression_start_s": compression_s,  # 開始按壓時間
            "ems_arrived_s": ems_s,                # EMS 抵達時間
            "session_end_s": end_s,
            "state_dwell_s": dwell,                 # 各階段停留
            "defense_counts": defense_counts,       # fallback 觸發統計
            "total_events": len(self.events),
        }
