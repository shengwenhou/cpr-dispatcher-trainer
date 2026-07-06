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


# 「當前這一步完成、請求下一步」語型（課堂高頻超短句，直接走 fastpath 不勞 LLM）：
# 再來呢／然後呢／接下來／下一步／好了／跪好了／做好了／完成了／弄好了…
# 注意：這是「單一步驟」確認（S5 逐步引導播下一步），不是「全部擺好」（那走 _ALL_POSITIONED）。
_STEP_DONE_PHRASES = [
    "再來呢", "再來要", "再來咧", "再來是", "然後呢", "然後咧", "然後要", "然後是",
    "接下來", "下一步", "下一個步驟", "做好了", "做完了", "完成了", "弄好了", "好了然後",
    "然後勒", "再來勒", "接著呢", "接著要", "好了", "好啦", "跪好了", "跪好", "OK", "ok", "可以了",
]

# 「全部擺好、可以開始」的明確全局完成語型（→ POSITIONING_DONE，S5 跳過剩餘步驟直接進 S6）。
# 需比 step_done 更明確（含「都／全部／位置／手也」等整體語氣），且優先於 step_done 判定。
_ALL_POSITIONED_PHRASES = [
    "都擺好", "都弄好", "都準備好", "都好了", "全部好", "全部弄好", "全部擺好", "都就位",
    "位置都", "手也擺好", "手也放好", "位置擺好", "姿勢擺好", "都用力壓好",
]


def detect_all_positioned(text: str) -> bool:
    """偵測「全部擺好、可以開始壓」的明確全局完成訊號（→ 跳過剩餘 S5 步驟）。"""
    if not text:
        return False
    return any(p in text for p in _ALL_POSITIONED_PHRASES)


def detect_step_done(text: str) -> bool:
    """偵測「當前這一步完成、請求下一步」。用寬鬆片語比對，容短句與諧音。

    呼叫端若同時要判全局完成，應先判 detect_all_positioned（較明確，優先）。"""
    if not text:
        return False
    return any(p in text for p in _STEP_DONE_PHRASES)


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

        # 數數偵測在 S5／S6 有意義。學員在 S5 擺位途中就開始數數＝已就位且已起壓 →
        # 填 COMPRESSIONS_STARTED（引擎蘊含補 POSITIONING_DONE），直接完成擺位跳進 S6 並打
        # 起壓時間戳，不會卡在 S5 把數數當 unknown。S6 內數數＝壓胸進行中。
        # （S5／S6 之外不報 counting：年齡/門牌/分鐘數等含數字句對 FSM 惰性，且寬鬆規則會誤判。）
        if state in (State.S5, State.S6) and detect_counting(text, strict=False):
            res.counting = True
            res.confidence = 0.85
            res.slots[Slot.COMPRESSIONS_STARTED] = SlotValue.YES
            return res

        # S5「全部擺好」明確全局完成（都擺好了／手也放好了）→ 填 POSITIONING_DONE，
        # 引擎據此跳過剩餘 S5 步驟直接進 S6。優先於 step_done（較明確）。
        if state == State.S5 and detect_all_positioned(text):
            res.slots[Slot.POSITIONING_DONE] = SlotValue.YES
            res.confidence = 0.85
            return res

        # 「當前這一步完成、請求下一步」（好了／再來呢／然後呢…）：課堂高頻超短句，
        # 直接走 fastpath 省 LLM 往返（S5 auto-advance 只有數秒，LLM 往返太慢）。
        if detect_step_done(text):
            res.step_done = True
            res.confidence = 0.85
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
    # 注意：S5 改逐步引導後，「好了／跪好了」屬單步確認（step_done），不再一次填 POSITIONING_DONE。
    # 全局完成改由 detect_all_positioned 判斷（都擺好了／手也放好了…）。此清單保留給非 S5 情境
    # （目前無用，POSITIONING_DONE 於 S5 由 step_done 逐步累積或全局完成句填）。

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

        # S5：全局完成句（都擺好了／手也放好了）→ POSITIONING_DONE（跳過剩餘步驟）。優先於 step_done。
        if state == State.S5 and detect_all_positioned(text):
            res.slots[Slot.POSITIONING_DONE] = SlotValue.YES

        # 「請求下一步」（好了／再來呢／完成了…）：沒有更強的 slot 訊號時標記 step_done，
        # 讓降級路徑也能正確處理（S1–S4 重問、S5 播下一步、S6 鼓勵）。
        if not res.slots and detect_step_done(text):
            res.step_done = True

        if res.slots or res.step_done:
            res.confidence = 0.6
        return res
