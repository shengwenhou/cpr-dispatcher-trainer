"""課堂資料持久化：檔案系統 JSONL ＋ JSON manifest（定案，不用 SQLite）。

選型理由（見設計裁決 1）：資料量小（≤30 場/班、MB 級）、單機單講師零寫入併發、引擎已直接
產 JSONL（零轉換）、append-only 對「進行中場次即時落地／斷線重連重建」天然友善、人可讀可
grep、無 DB 維運負擔。報告產出（Word/Excel/dashboard）皆為「掃事件→算指標」的批次，JSONL
全掃遠快於需求。

目錄結構（data_root 由 config.server.data_root 決定；.gitignore 收錄 data/）：
    <data_root>/<class_id>/class.json               課堂 manifest（含 SessionRef 索引）
    <data_root>/<class_id>/<session_id>.jsonl        該場完整 metrics 事件流（逐事件 append）
    <data_root>/<class_id>/<session_id>.meta.json    場次 manifest（含結束時的 summary 快照）

資料模型：Class（課堂）→ StudentSession（多場，各綁學員代號）→ Events（時間戳事件流）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 學員代號／課堂標籤為使用者輸入，壓成安全檔名片段以防路徑穿越（../ 等）與非法字元。
# 保留英數、底線、連字號與中日韓漢字；其餘一律替換為連字號。
_SAFE_RE = re.compile(r"[^0-9A-Za-z_\-一-鿿]+")

# 結案 SessionRef 摘要保留的關鍵指標欄位（供結課列舉免掃全部 JSONL）。
_KEY_METRIC_FIELDS = ("ohca_recognized_s", "compression_start_s", "ems_arrived_s")


def _slug(s: str, fallback: str) -> str:
    s = _SAFE_RE.sub("-", (s or "").strip()).strip("-")
    return s or fallback


def _stamp() -> str:
    """檔名用本地時間戳（可排序、無私有資訊）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    """人可讀的帶時區 ISO 時間（落地 manifest 用）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class SessionMeta:
    """一場學員練習的 manifest（落地為 <session_id>.meta.json）。"""

    session_id: str
    class_id: str
    student_alias: str
    mode: str            # "voice" | "text"
    started_at: str
    status: str = "running"           # running | completed | aborted | error
    ended_at: Optional[str] = None
    summary: dict[str, Any] = field(default_factory=dict)  # 結束時 metrics.summary() 快照


@dataclass
class ClassManifest:
    """一堂課的 manifest（落地為 class.json）。sessions 為 SessionRef 索引清單。"""

    class_id: str
    scenario: str
    locale: str
    label: Optional[str]
    created_at: str
    sessions: list[dict[str, Any]] = field(default_factory=list)


class SessionStore:
    """課堂資料存取層。全同步、無鎖（單機單講師、同時最多一場 active，無寫入併發）。"""

    def __init__(self, data_root: Path) -> None:
        self.root = Path(data_root)

    # ── 路徑 ─────────────────────────────────────────────────
    def _class_dir(self, class_id: str) -> Path:
        return self.root / class_id

    def _class_json(self, class_id: str) -> Path:
        return self._class_dir(class_id) / "class.json"

    def _session_jsonl(self, class_id: str, session_id: str) -> Path:
        return self._class_dir(class_id) / f"{session_id}.jsonl"

    def _session_meta(self, class_id: str, session_id: str) -> Path:
        return self._class_dir(class_id) / f"{session_id}.meta.json"

    # ── JSON 讀寫 ────────────────────────────────────────────
    @staticmethod
    def _write_json(path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _unique_class_id(self, base: str) -> str:
        cid, n = base, 1
        while self._class_dir(cid).exists():
            n += 1
            cid = f"{base}-{n}"
        return cid

    def _unique_session_id(self, class_id: str, base: str) -> str:
        sid, n = base, 1
        while self._session_meta(class_id, sid).exists():
            n += 1
            sid = f"{base}-{n}"
        return sid

    # ── 課堂 ─────────────────────────────────────────────────
    def create_class(self, scenario: str, locale: str, label: Optional[str] = None) -> ClassManifest:
        base = f"{_stamp()}_{_slug(label or scenario, scenario)}"
        class_id = self._unique_class_id(base)
        cm = ClassManifest(
            class_id=class_id, scenario=scenario, locale=locale,
            label=label, created_at=_iso_now(),
        )
        self._write_json(self._class_json(class_id), asdict(cm))
        return cm

    def load_class(self, class_id: str) -> ClassManifest:
        return ClassManifest(**self._read_json(self._class_json(class_id)))

    def list_classes(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "class.json").exists())

    # ── 場次 ─────────────────────────────────────────────────
    def create_session(self, class_id: str, student_alias: str, mode: str) -> SessionMeta:
        base = f"{_stamp()}_{_slug(student_alias, 'student')}"
        session_id = self._unique_session_id(class_id, base)
        meta = SessionMeta(
            session_id=session_id, class_id=class_id, student_alias=student_alias,
            mode=mode, started_at=_iso_now(),
        )
        self._write_json(self._session_meta(class_id, session_id), asdict(meta))
        self._session_jsonl(class_id, session_id).touch()  # 建空事件檔，供 append
        self._add_session_ref(class_id, {
            "session_id": session_id, "student_alias": student_alias,
            "status": "running", "started_at": meta.started_at, "key_metrics": {},
        })
        return meta

    def load_session(self, class_id: str, session_id: str) -> SessionMeta:
        return SessionMeta(**self._read_json(self._session_meta(class_id, session_id)))

    def append_event(self, class_id: str, session_id: str, line: str) -> None:
        """把一筆 metrics 事件（已序列化的 JSON 字串）append 到場次 JSONL（即時落地）。"""
        path = self._session_jsonl(class_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def append_events(self, class_id: str, session_id: str, lines: list[str]) -> None:
        if not lines:
            return
        path = self._session_jsonl(class_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line.rstrip("\n") + "\n")

    def read_events(self, class_id: str, session_id: str) -> list[dict[str, Any]]:
        path = self._session_jsonl(class_id, session_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 損壞的行略過（append-only 局部可救）
        return out

    def finalize_session(self, class_id: str, session_id: str, status: str, summary: dict) -> None:
        """場次結束：更新 meta（status/ended_at/summary）與 class.json 索引的 key_metrics。"""
        meta = self.load_session(class_id, session_id)
        meta.status = status
        meta.ended_at = _iso_now()
        meta.summary = dict(summary or {})
        self._write_json(self._session_meta(class_id, session_id), asdict(meta))
        key_metrics = {k: meta.summary.get(k) for k in _KEY_METRIC_FIELDS}
        self._update_session_ref(class_id, session_id, status=status, key_metrics=key_metrics)

    # ── SessionRef 索引維護 ──────────────────────────────────
    def _add_session_ref(self, class_id: str, ref: dict[str, Any]) -> None:
        cm = self.load_class(class_id)
        cm.sessions.append(ref)
        self._write_json(self._class_json(class_id), asdict(cm))

    def _update_session_ref(self, class_id: str, session_id: str, **fields: Any) -> None:
        cm = self.load_class(class_id)
        for ref in cm.sessions:
            if ref.get("session_id") == session_id:
                ref.update(fields)
                break
        self._write_json(self._class_json(class_id), asdict(cm))

    # ── 斷線重連：從磁碟重建場次快照 ─────────────────────────
    def snapshot(self, class_id: str, session_id: str) -> dict[str, Any]:
        """從磁碟重建場次快照（活著的 runner 另有記憶體版；此為場次已不在記憶體時的後備）。

        以事件流回放出：當前狀態（最後一筆 state_enter）、已填 slot、逐字稿尾段（utterance_in
        的觸發原句）。summary 取自 meta（若已結束）。"""
        meta = self.load_session(class_id, session_id)
        events = self.read_events(class_id, session_id)
        state: Optional[str] = None
        filled: dict[str, Any] = {}
        transcript_tail: list[str] = []
        for e in events:
            etype = e.get("type")
            data = e.get("data", {}) or {}
            if etype == "state_enter":
                state = data.get("state")
            elif etype == "slot_fill":
                filled[data.get("slot")] = data.get("value")
            elif etype == "utterance_in":
                if e.get("trigger_text"):
                    transcript_tail.append(e["trigger_text"])
        return {
            "session_id": session_id, "class_id": class_id,
            "student_alias": meta.student_alias, "mode": meta.mode, "status": meta.status,
            "state": state, "filled": filled,
            "transcript_tail": transcript_tail[-20:],
            "summary": meta.summary,
        }
