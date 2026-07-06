"""RegexFastPath：不走 LLM 的即時偵測，用於延遲關鍵路徑與 LLM 不可用時的降級。

兩個職責：
1. S6 數數快篩（SPEC 明列「S6 數數偵測不走 LLM」）：文字含數字或「下」即判定 counting。
   與 LLM 分類並存——S6 進來的每一句都先過快篩，命中即當「壓胸進行中／起壓」證據，
   不必等 LLM 往返（省 150–400ms）。
2. 結束訊號偵測：「救護人員到了／來了／到場」類 → end_signal（→ 轉 S7）。
   spike 實測諧音風險高（「救護人員」→「客戶人員」），故用寬鬆多關鍵字聯集。

另提供 KeywordFallbackClassifier：LLM 不可用時的最小可用意圖分類，讓文字模式仍能跑完
happy path（降級路徑）。它不追求準確，只覆蓋各狀態最典型的表述，信心固定給中等值。

本檔規則字串為「辨識用關鍵字」，非派遣員台詞（不經台詞庫）；屬引擎內部語料，
繁中直寫符合紀律（使用者可見的是台詞，不是這些 pattern）。
"""
from __future__ import annotations

import re

from .intents import IntentResult, Slot, SlotValue, State

# 阿拉伯數字或中文數字（含「兩」）
_DIGIT = r"[0-9０-９一二三四五六七八九十兩]"
# 數數：連續出現數字，或出現「下」（壓胸口令「一下兩下」），或「壓」重複節奏詞
_COUNTING_RE = re.compile(
    rf"({_DIGIT}\s*下)|({_DIGIT}[\s、,，]*{_DIGIT})|(下[\s、,，]*{_DIGIT})"
)
# 單一「下」或單一數字也算（大聲數數 STT 可能只斷出一個 token）
_SINGLE_COUNT_RE = re.compile(rf"({_DIGIT})|(下)")

# 結束訊號關鍵字（寬鬆聯集，容諧音）：救護／消防／救援人員 + 到了／來了／到場／抵達／接手
_ARRIVAL_SUBJECTS = ["救護", "消防", "救援", "人員", "救護車", "醫護", "客戶人員"]
_ARRIVAL_VERBS = ["到了", "來了", "到場", "抵達", "接手", "已到", "到達", "來到", "到"]
_ARRIVAL_RE = re.compile(
    "(" + "|".join(map(re.escape, _ARRIVAL_SUBJECTS)) + ").{0,6}("
    + "|".join(map(re.escape, _ARRIVAL_VERBS)) + ")"
)


def detect_counting(text: str, strict: bool = False) -> bool:
    """偵測壓胸數數。

    strict=False（S6 內用）：寬鬆——出現任何數字或「下」即算數數，符合 spike
      「偵測規則用『含數字或下』即可」的實測結論。
    strict=True（S6 外用）：需雙 token 或「數字+下」，避免地址門牌號等誤判為數數。
    """
    if not text:
        return False
    if strict:
        return bool(_COUNTING_RE.search(text))
    return bool(_SINGLE_COUNT_RE.search(text))


def detect_arrival(text: str) -> bool:
    """偵測結束訊號（救護人員到了類）。"""
    if not text:
        return False
    return bool(_ARRIVAL_RE.search(text))


class RegexFastPath:
    """S6 數數與結束訊號的即時偵測器。無狀態，純函式包裝。"""

    def classify(self, text: str, state: State) -> IntentResult:
        """回傳 fastpath 判定。僅在偵測到 counting／arrival 時給出非空結果，否則交由 LLM。"""
        res = IntentResult(source="regex_fastpath", raw=text)

        # 結束訊號優先（任何狀態都可能出現，但主要在 S6）
        if detect_arrival(text):
            res.end_signal = True
            res.confidence = 0.9
            return res

        # 數數偵測只在 S6 有意義（起壓判定＋壓胸中不打斷）。S6 外即使句子含數字（年齡、
        # 樓層、門牌、分鐘數）也不報 counting——一來 S6 外 counting 對 FSM 是惰性的（不填
        # slot、不打戳），二來「五十歲」這類會被寬鬆規則誤判，乾脆在 S6 外一律不報，最乾淨。
        if state == State.S6 and detect_counting(text, strict=False):
            res.counting = True
            res.confidence = 0.85
            res.slots[Slot.COMPRESSIONS_STARTED] = SlotValue.YES  # S6 內數數＝已開始壓胸
            return res

        return res  # 空結果（is_unknown=True），讓上層續問 LLM


# ── 降級用關鍵字後備分類器 ──────────────────────────────────────
class KeywordFallbackClassifier:
    """LLM 不可用時的最小意圖分類。覆蓋各狀態最典型表述，讓文字模式跑得完 happy path。

    刻意保守：只在關鍵字明確命中時填 slot，信心給 0.6（過門檻但標示為後備來源）。
    未命中回傳空結果 → 上層走澄清／防禦流程。
    """

    # 各 slot 的觸發關鍵字（辨識語料，非台詞）
    _AMBULANCE_YES = ["救護車", "救護", "叫救護", "要救護", "派救護"]
    _FIRE = ["消防車", "消防"]
    _LOCATION_HINT = ["路", "街", "號", "巷", "弄", "樓", "區", "市", "縣", "鄉", "鎮", "在", "地址", "這裡", "家裡"]
    _NO_RESPONSE = ["沒反應", "沒有反應", "叫不醒", "沒回應", "沒有回應", "昏迷", "沒意識", "不省人事", "沒動"]
    _HAS_RESPONSE = ["有反應", "會動", "睜眼", "清醒", "有回應"]
    _NO_BREATH = ["沒呼吸", "沒有呼吸", "沒在呼吸", "沒有在呼吸", "停止呼吸", "沒起伏", "沒有起伏", "胸口沒"]
    # 明確瀕死喘息描述（有呼吸動作但異常）
    _AGONAL = ["喘", "喘息", "打呼", "很久才", "偶爾", "怪", "痰", "喉", "用力吸", "吸一大口", "吸一口"]
    # 呼吸描述模糊、無法判定 → 追問（probe）。含不確定詞或「有呼吸」但沒說正常
    _UNCLEAR_BREATH = ["不確定", "不太確定", "不知道", "看不出來", "好像有呼吸", "應該有呼吸", "有一點", "微弱"]
    _NORMAL_BREATH = ["正常呼吸", "呼吸正常", "很正常", "正常的"]
    _POSITION_DONE = ["跪好", "就位", "手放好", "準備好", "好了", "擺好", "趴好", "弄好", "ok", "OK"]

    def classify(self, text: str, state: State) -> IntentResult:
        res = IntentResult(source="keyword_fallback", raw=text)
        if not text:
            return res
        t = text

        def has(words: list[str]) -> bool:
            return any(w in t for w in words)

        # 依當前狀態優先判斷該狀態的 slot，但也允許跳步（多 slot 一次填）
        # S1：救護車 vs 消防車
        if has(self._AMBULANCE_YES):
            res.slots[Slot.WANTS_AMBULANCE] = SlotValue.YES
        elif has(self._FIRE):
            res.slots[Slot.WANTS_AMBULANCE] = SlotValue.NO

        # S2：地點（狀態在 S2 且出現地點線索）
        if state == State.S2 and has(self._LOCATION_HINT):
            res.slots[Slot.LOCATION] = SlotValue.PROVIDED

        # S3：意識
        if has(self._NO_RESPONSE):
            res.slots[Slot.CONSCIOUSNESS] = SlotValue.NO
        elif has(self._HAS_RESPONSE):
            res.slots[Slot.CONSCIOUSNESS] = SlotValue.YES

        # S4：呼吸（順序：明確無 > 瀕死喘息 > 明確正常 > 模糊追問）
        if has(self._NO_BREATH):
            res.slots[Slot.BREATHING] = SlotValue.ABSENT
        elif has(self._AGONAL):
            res.slots[Slot.BREATHING] = SlotValue.AGONAL
        elif has(self._NORMAL_BREATH):
            res.slots[Slot.BREATHING] = SlotValue.NORMAL
        elif has(self._UNCLEAR_BREATH):
            res.slots[Slot.BREATHING] = SlotValue.UNCLEAR

        # S5：擺位完成
        if state == State.S5 and has(self._POSITION_DONE):
            res.slots[Slot.POSITIONING_DONE] = SlotValue.YES

        if res.slots:
            res.confidence = 0.6
        return res
