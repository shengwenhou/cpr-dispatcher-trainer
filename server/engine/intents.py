"""意圖與 slot 契約：FSM 與各分類器（LLM／RegexFastPath／關鍵字後備）之間的資料介面。

設計核心（SPEC 四）：
- 狀態 S0–S7 為順序主幹，但意圖分類「一次回傳多個 slot」→ FSM 跳步到第一個未填 slot。
- 因此 FSM 的推進由「哪些 slot 已填」決定：state → gating slot 對照見 GATING_SLOT。
- 分類器回傳 IntentResult：多個 slot 值＋整體信心＋（可選）FAQ 命中＋（可選）結束訊號。

本檔只定義契約與 slot 模型，不含任何派遣員台詞字串（i18n 紀律）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class State(str, Enum):
    """FSM 狀態。值為穩定字串（進 metrics／log），對應台詞庫 states.<value>。"""

    S0 = "s0"  # 開場
    S1 = "s1"  # 確認救護車需求
    S2 = "s2"  # 確認地點
    S3 = "s3"  # 確認意識
    S4 = "s4"  # 確認呼吸（→ 辨識 OHCA）
    S5 = "s5"  # 指導擺位（進入 = 辨識 OHCA 時間戳）
    S6 = "s6"  # 指導壓胸（首次數數 = 起壓時間戳）
    S7 = "s7"  # 結束接手


# 狀態順序（跳步計算用；S0 為固定開場、S7 為終態，皆不列入 slot gating 序列）
STATE_ORDER: list[State] = [State.S1, State.S2, State.S3, State.S4, State.S5, State.S6]


class Slot(str, Enum):
    """可被意圖分類填入的 slot。每個推進狀態由一個 gating slot 把關。"""

    WANTS_AMBULANCE = "wants_ambulance"      # S1：確認要救護車
    LOCATION = "location"                     # S2：提供地點
    CONSCIOUSNESS = "consciousness"           # S3：意識狀態（有／無反應）
    BREATHING = "breathing"                   # S4：呼吸狀態（正常／無／瀕死喘息）
    POSITIONING_DONE = "positioning_done"     # S5：擺位完成（學員回報就位）
    COMPRESSIONS_STARTED = "compressions_started"  # S6：已開始壓胸（數數）


# state → 把關該狀態推進的 slot。填了此 slot 才算「通過」該狀態。
GATING_SLOT: dict[State, Slot] = {
    State.S1: Slot.WANTS_AMBULANCE,
    State.S2: Slot.LOCATION,
    State.S3: Slot.CONSCIOUSNESS,
    State.S4: Slot.BREATHING,
    State.S5: Slot.POSITIONING_DONE,
    State.S6: Slot.COMPRESSIONS_STARTED,
}


class SlotValue(str, Enum):
    """slot 的規範化值。分類器須回傳這些列舉之一（或省略該 slot）。

    對意識／呼吸做語義分級，讓 FSM 能判斷是否構成 OHCA（無意識＋無正常呼吸）。
    """

    # 通用
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    # 呼吸專用細分
    NORMAL = "normal"        # 正常呼吸
    ABSENT = "absent"        # 完全沒有呼吸（沒起伏、沒在呼吸）→ 明確無呼吸
    AGONAL = "agonal"        # 瀕死喘息（有喘但怪、很久才喘一下、像打呼／喉音）→ 視同無正常呼吸
    UNCLEAR = "unclear"      # 呼吸描述模糊（「好像有喘」「不太確定」）→ 需 probe 釐清，不足以判定
    # 地點
    PROVIDED = "provided"    # 已提供地點（內容容錯，不複誦）


@dataclass
class IntentResult:
    """一次分類的結果。

    slots: {Slot: SlotValue} — 本句填了哪些 slot（可多個，支援跳步）。
    confidence: 整體信心 0–1；低於門檻 → FSM 不前進、播澄清句。
    faq_id: 若命中課堂 FAQ，這裡帶 faq 的 id（層 3）。
    end_signal: 是否偵測到「救護人員到了」類結束訊號（→ 轉 S7）。
    counting: 是否偵測到壓胸數數（S6 快篩；RegexFastPath 主要輸出）。
    source: 產生此結果的來源（"llm"／"regex_fastpath"／"keyword_fallback"／"none"）。
    raw: 分類器原始回應（除錯用，可為 None）。
    """

    slots: dict[Slot, SlotValue] = field(default_factory=dict)
    confidence: float = 0.0
    faq_id: Optional[str] = None
    end_signal: bool = False
    counting: bool = False
    source: str = "none"
    raw: Optional[Any] = None

    @property
    def is_unknown(self) -> bool:
        """無任何可用資訊：沒填 slot、沒 FAQ、沒結束訊號、沒數數。"""
        return (
            not self.slots
            and self.faq_id is None
            and not self.end_signal
            and not self.counting
        )


# ── slot 值是否「滿足」某狀態的推進條件 ──────────────────────────
def slot_satisfies(slot: Slot, value: SlotValue) -> bool:
    """判斷某 slot 的值是否足以通過其 gating 狀態。

    - WANTS_AMBULANCE：YES 才過（NO＝要消防車，停留 S1 引導）。
    - LOCATION：PROVIDED／YES 視為已提供。
    - CONSCIOUSNESS：任何明確判定（YES／NO）皆算「已評估」而通過；臨床上無意識才續走，
      但「有意識」在本課堂固定情境（無意識假人）中不會發生，FSM 仍以「已評估」推進，
      避免卡死；真有意識的處置屬進階情境（v1 不涵蓋）。
    - BREATHING：NORMAL 不算通過（需回到正常呼吸分支，但本情境不會發生）；
      ABSENT／AGONAL 視為「無正常呼吸」→ 通過並觸發 OHCA；
      UNCLEAR（描述模糊）不算通過——播 probe 釐清後停在 S4 等更明確的回報。
    - POSITIONING_DONE：YES。
    - COMPRESSIONS_STARTED：YES。
    """
    if slot == Slot.WANTS_AMBULANCE:
        return value == SlotValue.YES
    if slot == Slot.LOCATION:
        return value in (SlotValue.PROVIDED, SlotValue.YES)
    if slot == Slot.CONSCIOUSNESS:
        return value in (SlotValue.YES, SlotValue.NO)
    if slot == Slot.BREATHING:
        return value in (SlotValue.ABSENT, SlotValue.AGONAL)  # UNCLEAR/NORMAL 不通過
    if slot == Slot.POSITIONING_DONE:
        return value == SlotValue.YES
    if slot == Slot.COMPRESSIONS_STARTED:
        return value == SlotValue.YES
    return False


def breathing_implies_ohca(value: SlotValue) -> bool:
    """呼吸值是否構成 OHCA 判定（無正常呼吸）。"""
    return value in (SlotValue.ABSENT, SlotValue.AGONAL)


# ── canonical 角色標記（引擎內部，非使用者可見）──────────────────
# 狀態的 canonical 分四種角色，決定「什麼情境播哪句」：
#   (a) 詢問句 ENTRY_QUESTION_IDS：進入狀態時問學員以取得 slot；slot 於跳步已滿足時抑制
#       （不問已知答案）。
#   (b) 確認句 CONFIRM_IDS：gating slot 已滿足時播（拿到答案的回應，如「地址記下了」）；
#       slot 尚空時抑制。
#   (c) 條件句 CONDITIONAL_LINES：只在 gating slot 取「特定值」時播（比 confirm 更細，
#       依 slot 的哪個值決定播哪一句）。用於同一狀態下不同回報走不同台詞的臨床分歧。
#   (d) 未列入上述任何表的 canonical＝always-voice（一律播，如 S5/S6 指令、S7 交接）。
# 註：條件句可指向 variant id（非 canonical 序列內），播放時以 id 直接取用。

ENTRY_QUESTION_IDS: dict[State, set[str]] = {
    State.S2: {"s2_addr_ask_c"},
    State.S3: {"s3_consciousness_c"},
    State.S4: {"s4_breathing_c"},  # 只問有無正常呼吸；瀕死喘息追問改為條件句（見下）
}

CONFIRM_IDS: dict[State, set[str]] = {
    State.S2: {"s2_addr_confirm_c"},
    # S4 的判定句改由 CONDITIONAL_LINES 依呼吸值選句（v01 vs c），不再放這裡
}

# 條件 utterance 選擇（最小而通用的機制）：
# state → 有序清單 [(line_id, {觸發的 gating slot 值})]。
# 播放時逐項比對當前 gating slot 值：值 ∈ 觸發集才播該 line（依清單順序）。
# line 可為 canonical 或 variant id（皆以 script 依 id 取用）。
# 下一個情境（小兒）若有「同狀態依回報值分歧台詞」需求，於此表加一條即可，不動 fsm.py。
#
# S4 呼吸判定的臨床分歧（依台詞庫審定備註）：
#   - UNCLEAR（有呼吸但描述模糊/怪）→ s4_agonal_probe_c 追問釐清是否瀕死喘息（停 S4 等回報）。
#   - AGONAL（確認瀕死喘息：很久喘一下、打呼、喉音）→ s4_agonal_ruling_c「這種喘不算正常呼吸…」。
#   - ABSENT（明確沒呼吸/沒起伏）→ s4_agonal_ruling_v01「他沒有在正常呼吸，不要再等了…」
#     （避免用「這種喘」指涉不存在的描述）。
CONDITIONAL_LINES: dict[State, list[tuple[str, set["SlotValue"]]]] = {
    State.S4: [
        ("s4_agonal_probe_c", {SlotValue.UNCLEAR}),
        ("s4_agonal_ruling_c", {SlotValue.AGONAL}),
        ("s4_agonal_ruling_v01", {SlotValue.ABSENT}),
    ],
}


def conditional_ids(state: State) -> set[str]:
    """某狀態所有「條件句」的 id 集合（供 canonical 正常迭代時略過這些 id）。"""
    return {lid for lid, _ in CONDITIONAL_LINES.get(state, [])}

