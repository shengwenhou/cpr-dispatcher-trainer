"""FSM 對話引擎：S0–S7 狀態機主幹 + 意外輸入五層防禦。

設計原則
========
- **純同步、決定性**：引擎不碰音訊、不碰真時鐘（時間由呼叫端以 now 參數注入）、不呼叫 LLM
  （分類結果由呼叫端以 IntentResult 傳入）。所有副作用以 SpeakAction 清單回傳，交 driver 執行。
  → 引擎可完整單元測試；文字模式與語音模式共用同一顆引擎。
- **推進由 slot 決定**：狀態順序 S1→S6 各由一個 gating slot 把關；意圖一次可填多 slot →
  跳步到「第一個未填 slot」對應的狀態（SPEC 層 1）。
- **五層防禦**（SPEC 四）：
    層 1 跳步＝多 slot 一次填（正常能力）。
    層 2 元台詞：clarify（首次 unknown）／takeover（連續 2 次 unknown）／bridge（承接前綴＋重問當前問句）。
    層 3 FAQ：命中課堂 FAQ → 播答句 → 自動用 bridge 前綴重問當前狀態問句。
    層 4 受約束即時生成：unknown 但語句有實質內容 → 先播 filler 掩飾延遲，再播生成短句
         （≤40 字、僅安撫承接拉回、不新增醫療指示）；生成不可用時降級為層 2。
    層 5 分級 timeout：沉默 5s／10s 兩級 reprompt；技術故障 → tech_fault 句請講師介入。
- **S6 特殊模式**：非嚴格輪流。進入即啟動插播計時器（15–20s 隨機輪替 inserts）；
  首次偵測 counting＝起壓時間戳；聽到結束訊號→S7。

台詞一律以 id 經台詞庫取得（i18n 紀律）；引擎自身 log 為開發者字串，可繁中直寫。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import SpeakAction, SpeakKind
from .fastpath import RegexFastPath
from .intents import (
    CONDITIONAL_LINES,
    CONFIRM_IDS,
    ENTRY_QUESTION_IDS,
    GATING_SLOT,
    STATE_CONTEXT_FOR_GEN,
    STATE_ORDER,
    IntentResult,
    Slot,
    SlotValue,
    State,
    conditional_ids,
    layer4_text_violates,
    slot_satisfies,
)
from .metrics import EventType, MetricsRecorder
from .script_store import ScriptStore

# 型別：層 4 生成器。給定 (學員原句, 當前狀態問句全文, 狀態語境說明) 回傳 ≤max_chars 的
# 安撫承接句；不可用／失敗回傳 None（引擎據此降級為層 2）。由 driver 注入（真 LLM 或測試假物）。
# 第三參數為狀態語境（哪些動作尚未開始），讓生成器避免超前流程指示（SPEC 層 4 約束）。
Layer4Generator = Callable[[str, str, str], Optional[str]]

# S5 擺位逐步引導的四步 canonical id（依序，一次播一步）。variant 於重播當前步時輪替。
S5_STEP_IDS: list[str] = [
    "s5_position_kneel_c",
    "s5_position_handbase_c",
    "s5_position_stack_c",
    "s5_position_arms_c",
]


@dataclass
class EngineConfig:
    """引擎行為參數（由 config.Config 對應欄位轉入，集中預設便於測試覆寫）。"""

    confidence_threshold: float = 0.55
    s5_autoadvance_s: float = 4.0  # S5 每步沉默逾此秒數自動播下一步
    s6_insert_min_s: float = 15.0
    s6_insert_max_s: float = 20.0
    timeout_l1_s: float = 5.0
    timeout_l2_s: float = 10.0
    layer4_enabled: bool = True
    layer4_max_chars: int = 40


class DialogueEngine:
    """對話狀態機。持有當前狀態、已填 slot、防禦計數、S6 計時器狀態、metrics。

    對外主要方法：
      - start() → 產生 S0 開場動作。
      - on_utterance(text, result, now) → 消化一筆輸入，回傳 SpeakAction 清單。
      - tick(now) → 推進時間（S6 插播、沉默 timeout），回傳 SpeakAction 清單。
      - finished → 是否已達 S7 結束。
    """

    def __init__(
        self,
        script: ScriptStore,
        metrics: MetricsRecorder,
        config: Optional[EngineConfig] = None,
        rng: Optional[random.Random] = None,
        layer4_generator: Optional[Layer4Generator] = None,
    ) -> None:
        self.script = script
        self.metrics = metrics
        self.cfg = config or EngineConfig()
        self._rng = rng or random.Random()
        self._layer4 = layer4_generator
        self._fastpath = RegexFastPath()

        self.state: State = State.S0
        self.filled: dict[Slot, SlotValue] = {}
        self.finished: bool = False

        # 層 2 escalation：連續 unknown 次數（成功推進／FAQ 命中即歸零）
        self._unknown_streak: int = 0

        # 沉默 timeout：上次「有活動」的時間戳與已觸發的分級（避免重複播同級）
        self._last_activity_s: float = 0.0
        self._timeout_level_fired: int = 0

        # S6 插播計時器：下次插播的絕對時間（None＝未排程）
        self._next_insert_s: Optional[float] = None
        self._compression_started: bool = False

        # S5 逐步引導：目前播到第幾個 sub-step（-1＝尚未進 S5；0..3＝已播該步；4＝四步全完成）
        # 以及沉默 auto-advance 的下次觸發時間。
        self._s5_step: int = -1
        self._s5_next_auto_s: Optional[float] = None

        # 記錄每個狀態的進入時間，供 STATE_EXIT 算停留秒數
        self._state_enter_s: float = 0.0
        self._entered_once: bool = False  # 首次 enter 不記 STATE_EXIT（無前一狀態可離開）

    # ── 生命週期 ──────────────────────────────────────────────
    def start(self) -> list[SpeakAction]:
        """開場：記 SESSION_START，進入 S0 播固定開場白。"""
        self.metrics.record(EventType.SESSION_START)
        self._enter_state(State.S0, now=self.metrics.now(), trigger=None)
        actions = self._speak_state_canonicals(State.S0, layer=0)
        # S0 開場後即等待學員回應（救護車 vs 消防車），概念上等同進 S1 的問答前置；
        # 這裡狀態仍標 S0，待收到「要救護車」再推進。實務上 S0→S1 的橋接在 on_utterance 處理。
        self._reset_activity(self.metrics.now())
        return actions

    # ── 主輸入處理 ────────────────────────────────────────────
    def on_utterance(
        self,
        text: str,
        result: IntentResult,
        now: Optional[float] = None,
    ) -> list[SpeakAction]:
        """消化一筆學員輸入。

        text:   學員原句（觸發原句，進 metrics）。
        result: 分類結果。呼叫端已整合 fastpath＋LLM／keyword（見 driver）；
                但引擎仍會對 S6 再跑一次 fastpath 以保證數數關鍵路徑（雙重保險）。
        now:    monotonic 秒（預設取 metrics 當前時間）。
        """
        if now is None:
            now = self.metrics.now()
        if self.finished:
            return []

        self.metrics.record(EventType.UTTERANCE_IN, trigger_text=text, state=self.state.value)
        self._reset_activity(now)
        # S5 逐步引導中，學員一有互動（含 FAQ／澄清）就重置沉默 auto-advance 計時器，
        # 給學員一個完整的思考/操作窗口，不會剛答完 FAQ 就被計時器搶著往前推。
        if self.state == State.S5 and self._s5_next_auto_s is not None:
            self._s5_next_auto_s = now + self.cfg.s5_autoadvance_s

        # 引擎層再保險：S6 數數／結束訊號一律先過 fastpath（延遲關鍵路徑不依賴外部是否已跑）
        fp = self._fastpath.classify(text, self.state)
        result = self._merge_fastpath(result, fp, text)

        self.metrics.record(
            EventType.INTENT,
            trigger_text=text,
            state=self.state.value,
            source=result.source,
            confidence=result.confidence,
            slots={s.value: v.value for s, v in result.slots.items()},
            faq_id=result.faq_id,
            end_signal=result.end_signal,
            counting=result.counting,
        )

        # 結束訊號：任何狀態聽到「救護人員到了」→ 直達 S7（SPEC 結束條件）
        if result.end_signal:
            return self._go_s7(text, now)

        # 層 3：FAQ 命中優先於推進（答完再回到當前問句）
        if result.faq_id is not None:
            return self._handle_faq(result.faq_id, text, now)

        # S0/S1 分支：學員說要「消防車」（WANTS_AMBULANCE=NO）→ 播 fire_redirect 引導回，
        # 停留、不前進（SPEC S1：消防車→禮貌引導回）。
        if (
            self.state in (State.S0, State.S1)
            and result.slots.get(Slot.WANTS_AMBULANCE) == SlotValue.NO
            and result.confidence >= self.cfg.confidence_threshold
        ):
            return self._handle_fire_redirect(text, now)

        # 有可用 slot 且信心足夠 → 層 1 跳步推進
        usable = self._usable_slots(result)
        if usable and result.confidence >= self.cfg.confidence_threshold:
            self._unknown_streak = 0
            return self._advance_with_slots(usable, text, now)

        # S6 內即使沒明確 slot，只要 counting 就算壓胸進行中（起壓已在 merge 記錄）
        if self.state == State.S6 and result.counting:
            self._unknown_streak = 0
            return []  # 壓胸中，不打斷；插播由 tick 計時器負責

        # 「請求下一步」（再來呢／然後呢／接下來／完成了）：學員完成當前步驟、要指示。
        # 語意隨狀態不同（見 _handle_step_done），絕不走層 4。
        if result.step_done:
            self._unknown_streak = 0
            return self._handle_step_done(text, now)

        # 其餘：unknown／信心不足 → 層 4 或層 2
        return self._handle_unknown(text, now)

    # ── 時間推進（S6 插播、沉默 timeout）────────────────────────
    def tick(self, now: float) -> list[SpeakAction]:
        """由 driver 週期呼叫，推進時間相關行為。回傳需播放的動作。"""
        if self.finished:
            return []
        actions: list[SpeakAction] = []

        # S5 沉默 auto-advance：每步逾時未回應 → 視為學員正在做動作、自動播下一步。
        # 可能連鎖（若多個週期一次補上）或直接走完四步進 S6，故用 while。
        while (
            self.state == State.S5
            and self._s5_next_auto_s is not None
            and now >= self._s5_next_auto_s
        ):
            actions.extend(self._s5_advance(now, trigger=None, reason="auto"))
            # 進 S6 後 _s5_next_auto_s 會被清成 None，迴圈自然結束

        # S6 插播計時器
        if self.state == State.S6 and self._next_insert_s is not None and now >= self._next_insert_s:
            actions.extend(self._fire_insert(now))
            self._schedule_next_insert(now)

        # 沉默分級 timeout（S5 用 auto-advance、S6 用插播，皆不套用；見 _check_timeout）
        actions.extend(self._check_timeout(now))
        return actions

    # ================= 內部：狀態流轉 =================
    def _enter_state(self, state: State, now: float, trigger: Optional[str]) -> None:
        """切換狀態：記 STATE_EXIT（附停留）＋ STATE_ENTER；處理 S5/S6 特殊時間戳與計時器。"""
        # 離開舊狀態（首次進入無前一狀態，不記 EXIT）
        if self._entered_once and state != self.state:
            dwell = now - self._state_enter_s
            self.metrics.record(
                EventType.STATE_EXIT, trigger_text=trigger, state=self.state.value, dwell_s=round(dwell, 4)
            )
        self._entered_once = True
        self.state = state
        self._state_enter_s = now
        self.metrics.record(EventType.STATE_ENTER, trigger_text=trigger, state=state.value)

        # ★進入 S5 ＝ 辨識 OHCA 時間戳（SPEC 第六節）
        if state == State.S5:
            self.metrics.record(EventType.OHCA_RECOGNIZED, trigger_text=trigger)

        # 進入 S6：啟動插播計時器（非嚴格輪流）
        if state == State.S6:
            self._schedule_next_insert(now)

    def _advance_from_current(self, now: float, trigger: Optional[str]) -> list[SpeakAction]:
        """鏈式推進（chain-advance）。

        從當前狀態的下一步開始，逐狀態走：對每個狀態播其 canonical（跳步時抑制「已知答案的
        詢問句」，見 ENTRY_QUESTION_IDS），直到走到「第一個 gating slot 尚未滿足」的狀態
        （含）為止並停下等待學員回應。若一路 slot 都滿足則走到 S6（壓胸）。

        這解決了「填了某狀態 slot 就跳過該狀態、連帶漏掉其承接/判定/指令句」的問題：
        - S0→(要救護車) → 進 S1 播救護車確認 → S1 slot 已滿 → 續進 S2 播地址詢問 → 停。
        - S3 一句「沒反應也沒呼吸」→ S3 意識已滿續進 → S4 呼吸已滿：抑制呼吸詢問句、
          仍播瀕死判定「馬上開始壓胸」→ 續進 S5 播擺位 → S5 slot 未滿 → 停。
        """
        actions: list[SpeakAction] = []
        layer = 1 if trigger else 0

        # (0) 當前狀態的 gating slot 若「已填但不滿足」（如 S4 呼吸=UNCLEAR 描述模糊、
        #     S3 意識=YES 有反應但情境為無意識假人），不推進——有條件句就播（S4 probe），
        #     沒有就重問當前問句（S3「有反應」→ 重問確認），停在原狀態等更明確的回報。
        cur_slot = GATING_SLOT.get(self.state)
        if cur_slot is not None:
            cur_val = self.filled.get(cur_slot)
            if cur_val is not None and not slot_satisfies(cur_slot, cur_val):
                cond = self._conditional_actions(self.state, layer)
                return cond if cond else self._reask_current(layer=layer)

        # (1) 當前狀態的 gating slot 若「剛」被這句話填滿（且滿足），先播該狀態的確認/承接/條件句
        #     （如 S2 拿到地址後播 s2_addr_confirm_c；S4 呼吸判定後依值選 ruling_c／ruling_v01）。
        if cur_slot is not None and self._slot_ok(cur_slot):
            actions.extend(self._speak_confirm_only(self.state, layer=layer))

        # (2) 逐狀態往前走，直到「第一個 gating slot 尚未滿足」的狀態（含）為止。
        guard = 0
        while True:
            guard += 1
            if guard > 12:  # 防呆：狀態序列有限，正常不會超過
                break
            nxt = self._next_state_after(self.state)
            if nxt is None:
                break  # 已到 S6 之後，等結束訊號
            self._enter_state(nxt, now, trigger)
            actions.extend(self._speak_state_canonicals(nxt, layer=layer))
            slot = GATING_SLOT.get(nxt)
            if slot is None:
                break
            if not self._slot_ok(slot):
                break  # 此狀態尚缺答案 → 停下等學員回應
            if nxt == State.S6:
                break  # 到 S6 就停（壓胸階段，不再自動往前）
        return actions

    def _slot_ok(self, slot: Slot) -> bool:
        """某 slot 是否已滿足其 gating 條件。"""
        val = self.filled.get(slot)
        return val is not None and slot_satisfies(slot, val)

    def _next_state_after(self, state: State) -> Optional[State]:
        """回傳順序上的下一個狀態；S0→S1；S6 之後回 None（終點在 S7 由結束訊號觸發）。"""
        if state == State.S0:
            return State.S1
        try:
            idx = STATE_ORDER.index(state)
        except ValueError:
            return None
        if idx + 1 < len(STATE_ORDER):
            return STATE_ORDER[idx + 1]
        return None

    def _advance_with_slots(self, usable: dict[Slot, SlotValue], trigger: str, now: float) -> list[SpeakAction]:
        """層 1：填入 slot → 記錄 → 鏈式推進。呼吸構成 OHCA 的時間戳於進入 S5 時打（見 _enter_state）。"""
        for slot, val in usable.items():
            self.filled[slot] = val
            self.metrics.record(
                EventType.SLOT_FILL, trigger_text=trigger, slot=slot.value, value=val.value
            )
        # 已開始壓胸（含 S5 途中就數數起壓）→ 打起壓時間戳（idempotent，只記一次）。
        if usable.get(Slot.COMPRESSIONS_STARTED) == SlotValue.YES:
            self._mark_compression_start(trigger)

        # 蘊含關係：已開始壓胸 → 必然已就位（沒擺好位無法壓）。補填 POSITIONING_DONE，
        # 讓「他沒反應沒呼吸我在壓了」能一路跳到 S6，不被 S5 gating 卡住。
        if usable.get(Slot.COMPRESSIONS_STARTED) == SlotValue.YES and not self._slot_ok(Slot.POSITIONING_DONE):
            self.filled[Slot.POSITIONING_DONE] = SlotValue.YES
            self.metrics.record(
                EventType.SLOT_FILL, trigger_text=trigger, slot=Slot.POSITIONING_DONE.value,
                value=SlotValue.YES.value, implied=True,
            )

        # S5 逐步引導中收到「全局完成句」（都擺好了）→ 跳過剩餘步驟，直接完成並 chain 進 S6。
        if (
            self.state == State.S5
            and 0 <= self._s5_step < len(S5_STEP_IDS)
            and self._slot_ok(Slot.POSITIONING_DONE)
        ):
            return self._s5_complete(now, trigger, reason="skipped")

        return self._advance_from_current(now, trigger)

    def _handle_fire_redirect(self, trigger: str, now: float) -> list[SpeakAction]:
        """S1 消防車分支：進 S1（若在 S0）並播 fire_redirect 引導回救護車，停留不前進。"""
        self._unknown_streak = 0
        if self.state == State.S0:
            self._enter_state(State.S1, now, trigger)
        line = self.script.branch_line(State.S1.value, "fire_truck")
        if line is None:
            return []
        return [self._prerecorded(line.id, layer=0)]

    def _handle_step_done(self, trigger: str, now: float) -> list[SpeakAction]:
        """「請求下一步」的狀態相依處理（課堂高頻：再來呢／然後呢／完成了）。

        - S5：等同回報擺位完成 → 填 POSITIONING_DONE 推進 S6（S5 四步一次全播，故完成擺位
          即可進壓胸）。
        - S6：等同「壓胸持續中」→ 回一條 encourage insert（不觸發層 4），並重排下次插播。
        - S1–S4：學員在問流程，答案就是當前問題 → 重播當前狀態問句（bridge＋問句）。
        - S0：尚在開場，無問句可重播 → 不回應（等實質輸入）。
        """
        self.metrics.record(EventType.DEFENSE, trigger_text=trigger, layer=0, kind="step_done", state=self.state.value)

        if self.state == State.S5:
            # 逐步引導：口頭確認 → 播下一步（不是一次填完 POSITIONING_DONE）
            return self._s5_advance(now, trigger, reason="confirmed")

        if self.state == State.S6:
            # 壓胸持續中：回鼓勵插播、重排計時器（避免緊接著又插播）
            actions = self._fire_insert(now)
            self._schedule_next_insert(now)
            return actions

        if self.state in (State.S1, State.S2, State.S3, State.S4):
            return self._reask_current(layer=0)

        return []  # S0／S7：無對應行為

    def _go_s7(self, trigger: str, now: float) -> list[SpeakAction]:
        """轉 S7：記 EMS_ARRIVED → 播 handover → 記 SESSION_END → finished。"""
        self._enter_state(State.S7, now, trigger)
        self.metrics.record(EventType.EMS_ARRIVED, trigger_text=trigger)
        actions = self._speak_state_canonicals(State.S7, layer=0)
        # 結束
        dwell = now - self._state_enter_s
        self.metrics.record(EventType.STATE_EXIT, trigger_text=trigger, state=State.S7.value, dwell_s=round(dwell, 4))
        self.metrics.record(EventType.SESSION_END, trigger_text=trigger)
        self.finished = True
        return actions

    # ================= 內部：五層防禦 =================
    def _handle_faq(self, faq_id: str, trigger: str, now: float) -> list[SpeakAction]:
        """層 3：播 FAQ 答句，然後 bridge 前綴＋重問當前狀態問句。"""
        self._unknown_streak = 0
        self.metrics.record(EventType.DEFENSE, trigger_text=trigger, layer=3, faq_id=faq_id)
        actions: list[SpeakAction] = []
        ans = self.script.faq_answer(faq_id)
        if ans is not None:
            actions.append(self._prerecorded(ans.id, layer=3))
        # 答完自動接回當前問題（bridge 前綴 + 當前狀態 canonical 問句）
        actions.extend(self._reask_current(layer=3))
        return actions

    def _handle_unknown(self, text: str, now: float) -> list[SpeakAction]:
        """unknown／信心不足：優先層 4（有生成器且語句有實質內容），否則層 2 escalation。"""
        self._unknown_streak += 1

        # 層 4：受約束即時生成（先 filler 掩飾延遲）。沉默（空句）不走層 4。
        if (
            self.cfg.layer4_enabled
            and self._layer4 is not None
            and text
            and text.strip()
        ):
            question = self._current_question_text()
            state_context = STATE_CONTEXT_FOR_GEN.get(self.state, "")
            gen = None
            try:
                gen = self._layer4(text, question, state_context)
            except Exception:
                gen = None
            if gen:
                gen = gen.strip()[: self.cfg.layer4_max_chars]
                # 驗證層：生成含「當前狀態不該提及的流程動作」（如 S5 說「壓胸」）→ 丟棄降級層 2。
                violation = layer4_text_violates(gen, self.state)
                if violation is not None:
                    self.metrics.record(
                        EventType.DEFENSE, trigger_text=text, layer=4,
                        rejected=True, reason="procedure_leak", keyword=violation, generated=gen,
                    )
                    # 保守：降級層 2（bridge 前綴＋重播問句）
                    return self._handle_layer2(text)

                self.metrics.record(
                    EventType.DEFENSE, trigger_text=text, layer=4, generated=gen
                )
                filler = self.script.rotate_meta("filler")
                filler_id = filler.id if filler else None
                # 層 4 生成句之後仍回到當前問句，確保拉回腳本
                actions = [
                    SpeakAction(
                        kind=SpeakKind.FILLER_THEN_DYNAMIC,
                        text=gen,
                        layer=4,
                        filler_id=filler_id,
                    )
                ]
                actions.extend(self._reask_current(layer=4))
                return actions

        # 層 2：連續 2 次 unknown → takeover；否則 clarify。之後 bridge 重問。
        return self._handle_layer2(text)

    def _handle_layer2(self, trigger: str) -> list[SpeakAction]:
        """層 2 元台詞 escalation。"""
        actions: list[SpeakAction] = []
        if self._unknown_streak >= 2:
            self.metrics.record(EventType.DEFENSE, trigger_text=trigger, layer=2, kind="takeover")
            takeover = self.script.rotate_meta("takeover")
            if takeover:
                actions.append(self._prerecorded(takeover.id, layer=2))
            # takeover 後重問當前問句，並歸零 streak（已接管主導，重新開始）
            actions.extend(self._reask_current(layer=2))
            self._unknown_streak = 0
        else:
            self.metrics.record(EventType.DEFENSE, trigger_text=trigger, layer=2, kind="clarify")
            clarify = self.script.rotate_meta("clarify")
            if clarify:
                actions.append(self._prerecorded(clarify.id, layer=2))
        return actions

    def _check_timeout(self, now: float) -> list[SpeakAction]:
        """層 5 沉默分級 timeout：5s→l1，10s→l2，各級只播一次直到有活動 reset。

        S6 例外：壓胸階段非嚴格輪流，互動由插播計時器負責；學員沉默＝專心壓胸，
        不該用「喂？你還在嗎？」打斷。故 S6（與終態 S7、開場 S0）不套用沉默 timeout。"""
        # S5 例外：擺位改用沉默 auto-advance（持續往前帶），不問「你還在嗎」；S0/S6/S7 亦不套用。
        if self.state in (State.S0, State.S5, State.S6, State.S7):
            return []
        silence = now - self._last_activity_s
        actions: list[SpeakAction] = []
        if silence >= self.cfg.timeout_l2_s and self._timeout_level_fired < 2:
            self._timeout_level_fired = 2
            self.metrics.record(EventType.TIMEOUT, level=2, silence_s=round(silence, 2))
            self.metrics.record(EventType.DEFENSE, layer=5, kind="timeout_l2")
            line = self.script.rotate_meta("timeout_l2")
            if line:
                actions.append(self._prerecorded(line.id, layer=5))
        elif silence >= self.cfg.timeout_l1_s and self._timeout_level_fired < 1:
            self._timeout_level_fired = 1
            self.metrics.record(EventType.TIMEOUT, level=1, silence_s=round(silence, 2))
            self.metrics.record(EventType.DEFENSE, layer=5, kind="timeout_l1")
            line = self.script.rotate_meta("timeout_l1")
            if line:
                actions.append(self._prerecorded(line.id, layer=5))
        return actions

    def tech_fault(self) -> list[SpeakAction]:
        """層 5 安全網：技術故障 → 播 tech_fault 句請講師介入。由 driver 在異常時呼叫。"""
        self.metrics.record(EventType.DEFENSE, layer=5, kind="tech_fault")
        line = self.script.rotate_meta("tech_fault")
        return [self._prerecorded(line.id, layer=5)] if line else []

    # ================= 內部：S6 插播 =================
    def _schedule_next_insert(self, now: float) -> None:
        interval = self._rng.uniform(self.cfg.s6_insert_min_s, self.cfg.s6_insert_max_s)
        self._next_insert_s = now + interval

    def _fire_insert(self, now: float) -> list[SpeakAction]:
        line = self.script.rotate_insert()
        if line is None:
            return []
        self.metrics.record(EventType.SYSTEM_SPEAK, line_id=line.id, layer=0, s6_insert=True)
        return [SpeakAction(kind=SpeakKind.PRERECORDED, line_id=line.id, layer=0)]

    # ================= 內部：S5 逐步引導 =================
    def _s5_begin(self, layer: int) -> list[SpeakAction]:
        """進入 S5：播第一步（kneel），啟動 auto-advance 計時器。"""
        self._s5_step = 0
        self._s5_next_auto_s = self._state_enter_s + self.cfg.s5_autoadvance_s
        step_id = S5_STEP_IDS[0]
        self.metrics.record(
            EventType.S5_SUBSTEP, step=step_id, index=0, advance="enter"
        )
        return [self._prerecorded(step_id, layer=layer)]

    def _s5_advance(self, now: float, trigger: Optional[str], reason: str) -> list[SpeakAction]:
        """S5 往下一步推進。reason: confirmed（口頭確認）／auto（沉默自動）。

        若已在最後一步（arms）→ 四步完成，填 POSITIONING_DONE 並 chain 進 S6。
        否則播下一步 canonical（重播/續播用當前步變體輪替），重排 auto-advance 計時器。
        """
        if self._s5_step >= len(S5_STEP_IDS) - 1:
            # 最後一步已播 → 擺位完成，進 S6
            return self._s5_complete(now, trigger, reason="confirmed" if reason == "confirmed" else "auto")

        self._s5_step += 1
        # 重排下次 auto-advance：口頭確認從 now 起算；沉默自動從「上次到期時刻」起算，
        # 讓單次大幅 tick（如 /wait 20）也能連鎖補上多步。
        if reason == "auto" and self._s5_next_auto_s is not None:
            self._s5_next_auto_s = self._s5_next_auto_s + self.cfg.s5_autoadvance_s
        else:
            self._s5_next_auto_s = now + self.cfg.s5_autoadvance_s
        step_id = S5_STEP_IDS[self._s5_step]
        self.metrics.record(
            EventType.S5_SUBSTEP, trigger_text=trigger, step=step_id, index=self._s5_step, advance=reason
        )
        return [self._prerecorded(step_id, layer=1 if trigger else 0)]

    def _s5_complete(self, now: float, trigger: Optional[str], reason: str) -> list[SpeakAction]:
        """S5 四步完成（或全局完成句跳步）：填 POSITIONING_DONE、關計時器、chain 進 S6。

        reason: confirmed／auto（走完四步）／skipped（全局完成句跳過剩餘步）。
        """
        self._s5_next_auto_s = None
        self._s5_step = len(S5_STEP_IDS)
        self.metrics.record(
            EventType.S5_SUBSTEP, trigger_text=trigger, step="__complete__", advance=reason
        )
        self.filled[Slot.POSITIONING_DONE] = SlotValue.YES
        self.metrics.record(
            EventType.SLOT_FILL, trigger_text=trigger, slot=Slot.POSITIONING_DONE.value,
            value=SlotValue.YES.value, via=reason,
        )
        return self._advance_from_current(now, trigger)

    def _s5_current_step_id(self) -> str:
        """S5 當前 sub-step 的 canonical id（重播錨定用）。"""
        idx = self._s5_step if 0 <= self._s5_step < len(S5_STEP_IDS) else 0
        return S5_STEP_IDS[idx]

    # ================= 內部：台詞取用與播放 =================
    def _speak_state_canonicals(self, state: State, layer: int) -> list[SpeakAction]:
        """播某狀態的 canonical（依序），依四種角色決定是否播：
        - branch 句（如 s1 fire_redirect）：只在對應分支情境播，不走正常流 → 永遠跳過。
        - 詢問句（ENTRY_QUESTION_IDS）：slot 已滿足時抑制（不問已知答案）；未滿足時播。
        - 確認句（CONFIRM_IDS）：slot 已滿足時播（拿到答案的回應）；未滿足時抑制。
        - 條件句（CONDITIONAL_LINES）：不在此正常迭代播出（由 _conditional_actions 依 slot 值選），
          此處一律略過，最後再統一附加。
        - 其餘（always-voice）：一律播（S1 確認、S6 指令、S7 交接）。
        S5 特例：不一次連播四步，改進入逐步引導模式——只播第一步並啟動 auto-advance 計時器。
        """
        # S5：逐步引導。進入時只播第一步（kneel），其餘由確認／沉默 auto-advance／全局完成推進。
        if state == State.S5 and not self._slot_ok(Slot.POSITIONING_DONE):
            return self._s5_begin(layer)

        slot = GATING_SLOT.get(state)
        slot_ok = slot is not None and self._slot_ok(slot)
        entry_qs = ENTRY_QUESTION_IDS.get(state, set())
        confirms = CONFIRM_IDS.get(state, set())
        cond_ids = conditional_ids(state)
        actions: list[SpeakAction] = []
        for line in self.script.canonical(state.value):
            if line.branch:
                continue
            if line.id in cond_ids:
                continue  # 條件句改由 _conditional_actions 選播
            if line.id in entry_qs and slot_ok:
                continue  # 已知答案，抑制詢問句
            if line.id in confirms and not slot_ok:
                continue  # 還沒拿到答案，抑制確認句
            actions.append(self._prerecorded(line.id, layer=layer))
        # 條件句：依當前 gating slot 值選播（如 S4 依呼吸值選 probe / ruling_c / ruling_v01）
        actions.extend(self._conditional_actions(state, layer))
        return actions

    def _speak_confirm_only(self, state: State, layer: int) -> list[SpeakAction]:
        """播某狀態的「確認句」＋「條件句」。用於 slot 剛在當前狀態被填滿、即將往前推進時，
        先講完該狀態對這個答案的回應（如 S2 拿到地址後的 s2_addr_confirm_c；
        S4 拿到呼吸判定後依值選 ruling_c／ruling_v01）。"""
        actions: list[SpeakAction] = []
        confirms = CONFIRM_IDS.get(state, set())
        if confirms:
            for line in self.script.canonical(state.value):
                if line.id in confirms:
                    actions.append(self._prerecorded(line.id, layer=layer))
        actions.extend(self._conditional_actions(state, layer))
        return actions

    def _conditional_actions(self, state: State, layer: int) -> list[SpeakAction]:
        """依當前 gating slot 值選播條件句（CONDITIONAL_LINES）。

        逐項比對：slot 值 ∈ 該 line 的觸發集才播（依表定順序）。line 可為 canonical 或
        variant id，一律以 script 依 id 取用。slot 未填則不播任何條件句。"""
        rules = CONDITIONAL_LINES.get(state)
        if not rules:
            return []
        slot = GATING_SLOT.get(state)
        val = self.filled.get(slot) if slot else None
        if val is None:
            return []
        actions: list[SpeakAction] = []
        for line_id, triggers in rules:
            if val in triggers and self.script.has(line_id):
                actions.append(self._prerecorded(line_id, layer=layer))
        return actions

    def _current_question_id(self) -> Optional[str]:
        """當前狀態「主問句」的 canonical id（用於 bridge 重問）。

        S5 例外：擺位為逐步引導，重問要錨定「當前 sub-step」句（不回第一步 kneel）。
        其他狀態取該狀態第一個非分支 canonical。"""
        if self.state == State.S5 and 0 <= self._s5_step < len(S5_STEP_IDS):
            return self._s5_current_step_id()
        for line in self.script.canonical(self.state.value):
            if not line.branch:
                return line.id
        return None

    def _current_question_text(self) -> str:
        qid = self._current_question_id()
        return self.script.text_of(qid) if qid else ""

    def _reask_current(self, layer: int) -> list[SpeakAction]:
        """bridge 前綴 + 重問當前狀態主問句（輪替變體，同輪不重複）。

        S6 例外：壓胸階段沒有「問句」可重問，改為不重問（插播計時器負責維持互動）。
        S7／S0 也不重問。"""
        if self.state in (State.S6, State.S7, State.S0):
            return []
        qid = self._current_question_id()
        if qid is None:
            return []
        actions: list[SpeakAction] = []
        bridge = self.script.rotate_meta("bridge")
        if bridge:
            actions.append(self._prerecorded(bridge.id, layer=layer))
        # 重問時輪替 canonical／變體，避免每次都同一句
        variant = self.script.rotate_variant(qid)
        actions.append(self._prerecorded(variant.id, layer=layer))
        return actions

    def _prerecorded(self, line_id: str, layer: int) -> SpeakAction:
        self.metrics.record(EventType.SYSTEM_SPEAK, line_id=line_id, layer=layer)
        return SpeakAction(kind=SpeakKind.PRERECORDED, line_id=line_id, layer=layer)

    # ================= 內部：工具 =================
    def _merge_fastpath(self, result: IntentResult, fp: IntentResult, text: str) -> IntentResult:
        """把 fastpath 結果併入分類結果。fastpath 對 counting／end_signal 有最終話語權。"""
        if fp.end_signal:
            result.end_signal = True
        if fp.counting:
            result.counting = True
            # S6 內數數 → 記起壓（首次）並補 slot
            if self.state == State.S6:
                for s, v in fp.slots.items():
                    result.slots[s] = v
                self._mark_compression_start(text)
        if result.source == "none" and fp.source != "none":
            result.source = fp.source
        if fp.confidence > result.confidence and (fp.counting or fp.end_signal):
            result.confidence = fp.confidence
        return result

    def _mark_compression_start(self, trigger: str) -> None:
        """★首次偵測數數＝起壓時間戳（SPEC 第六節）。只記一次。"""
        if not self._compression_started:
            self._compression_started = True
            self.metrics.record(EventType.COMPRESSION_START, trigger_text=trigger)

    def _usable_slots(self, result: IntentResult) -> dict[Slot, SlotValue]:
        """挑出「有意義」的 slot（值非 UNKNOWN）。"""
        return {s: v for s, v in result.slots.items() if v != SlotValue.UNKNOWN}

    def _reset_activity(self, now: float) -> None:
        self._last_activity_s = now
        self._timeout_level_fired = 0
