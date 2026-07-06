"""引擎輸出動作：FSM 決定「要播什麼」，由 driver 翻譯成實際 TTS 呼叫。

把「決策」與「副作用（播音）」分離：引擎回傳 SpeakAction 清單（純資料），
driver（文字模式 harness 或真語音 runtime）負責執行。好處：
- 引擎完全同步、決定性、可單元測試（不碰音訊、不碰時鐘以外的 I/O）。
- 文字模式與語音模式共用同一顆引擎，只是 driver 不同。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SpeakKind(str, Enum):
    """播放種類，決定 driver 怎麼取音。"""

    PRERECORDED = "prerecorded"  # 依 line_id 取預錄 wav（層 1/2/3/5 與所有 canonical）
    DYNAMIC = "dynamic"          # 即時生成的全文（層 4），走 speak_dynamic
    FILLER_THEN_DYNAMIC = "filler_then_dynamic"  # 先播 filler 掩飾延遲，再播動態全文（層 4）


@dataclass
class SpeakAction:
    """一個播放動作。

    kind:     播放種類。
    line_id:  預錄台詞 id（PRERECORDED 用）；動態句為 None。
    text:     動態全文（DYNAMIC／FILLER_THEN_DYNAMIC 用）；預錄句可為 None（由 id 取全文）。
    layer:    來自五層防禦的哪一層（0＝正常腳本流；1–5＝防禦層），供 metrics／debriefing 標記。
    filler_id: FILLER_THEN_DYNAMIC 時先播的 filler 台詞 id。
    """

    kind: SpeakKind
    line_id: Optional[str] = None
    text: Optional[str] = None
    layer: int = 0
    filler_id: Optional[str] = None
