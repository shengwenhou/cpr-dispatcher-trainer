"""GeminiIntentClassifier：Vertex AI（google-genai）意圖分類，constrained JSON 輸出。

- 認證：GOOGLE_APPLICATION_CREDENTIALS 環境變數（ADC，與 TTS 批次共用同一 service account）。
  本檔絕不引用金鑰內容或私有路徑。
- 模型：model_id 為設定值（預設 flash-lite 級）；可用性由 available() 探測（列模型／試呼叫）。
- constrained decoding：response_mime_type=application/json + response_schema，一次回傳多 slot
  ＋信心＋（可選）FAQ 命中＋結束訊號（對齊 IntentResult）。
- 系統 prompt 明確要求：僅做分類、不生成台詞、諧音容錯（電話報案語境，spike 實測諧音為常態）。

另含 GeminiLayer4Generator：層 4 受約束即時生成（≤N 字、僅安撫承接拉回、禁止新醫療指示），
每次生成寫入 logs/layer4/ 待課後審核（SPEC 層 4）。

同步介面：內部 SDK 呼叫為同步；driver 需非阻塞時以 thread offload（見 runtime）。
逾時／認證失敗／內容過濾一律回傳空／低信心結果或 None，不拋例外中斷對話（降級友善）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..engine.intents import IntentResult, Slot, SlotValue, State
from .base import LLMProvider

# 意圖分類的 constrained JSON schema（用 google.genai 的 dict schema 形式，避免硬綁 types 版本差異）
_SLOT_ENUM_GENERIC = ["yes", "no", "normal", "absent", "agonal", "provided", "unknown"]

_INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "wants_ambulance": {"type": "STRING", "enum": ["yes", "no", "unknown"]},
        "location": {"type": "STRING", "enum": ["provided", "unknown"]},
        "consciousness": {"type": "STRING", "enum": ["yes", "no", "unknown"]},
        "breathing": {"type": "STRING", "enum": ["normal", "absent", "agonal", "unclear", "unknown"]},
        "positioning_done": {"type": "STRING", "enum": ["yes", "no", "unknown"]},
        "compressions_started": {"type": "STRING", "enum": ["yes", "no", "unknown"]},
        "faq_id": {"type": "STRING"},
        "end_signal": {"type": "BOOLEAN"},
        "step_done": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["confidence", "end_signal"],
}

_SYSTEM_PROMPT = """你是 CPR 派遣訓練系統的「意圖分類器」，不是對話者。你的唯一工作是把報案民眾的一句話
分類成結構化 JSON，供有限狀態機決定下一步。嚴格遵守：
1. 只輸出 JSON，符合給定 schema；不要生成任何對民眾說的話。
2. 這是電話報案的語音轉文字，常有諧音錯誤（例：「救護人員」可能被辨識成「客戶人員」、
   「倒」成「早」、地址街名易錯）。請以語意與情境容錯判斷，不要因逐字不完美就判 unknown。
3. 各欄位語意：
   - wants_ambulance：要救護車=yes；說要消防車=no；無關=unknown。
   - location：有講出任何地點/地址（不論正確與否）=provided；否則 unknown。
   - consciousness：叫得醒/有反應=yes；叫不醒/沒反應/昏迷=no；未提=unknown。
   - breathing：正常呼吸=normal；完全沒呼吸/胸口沒起伏=absent；很久才喘一下/瀕死喘息/像打呼/喉音/偶爾用力吸一口=agonal；
       民眾有提到呼吸但描述模糊、無法判定是否正常（如「好像有喘」「不太確定」「怪怪的」）=unclear；完全未提=unknown。
       （absent 與 agonal 的差別很重要：absent 是「完全沒有」，agonal 是「有動作但屬瀕死喘息」；拿不準時用 unclear 讓系統追問。）
   - positioning_done：民眾表示已就位/手已放好/準備好=yes；否則 unknown。
   - compressions_started：民眾在數數或說已在壓=yes；否則 unknown。
   - end_signal：民眾表示救護人員已到/接手=true；否則 false。
   - step_done：民眾表示「當前步驟做完了、要下一步指示」（如「再來呢」「然後呢」「接下來」
       「做好了」「完成了」「下一步」）=true；否則 false。這是課堂高頻語，代表完成當前動作、
       等你指示，不是聽不懂。
   - faq_id：若這句話對應下列課堂常見問題之一，填該 id；否則留空字串。
4. 一句話可同時填多個欄位（如「他沒反應也沒呼吸」→ consciousness=no, breathing=absent）。
5. confidence：你對本次分類整體的信心 0.0–1.0。模糊/聽不懂時給低分（<0.5）。
6. 【單字/極簡回答依當前問句解讀】民眾常只回一個字或極短句，其意義由「當前問句」決定，
   你必須結合下方標示的「目前狀態/當前問句」來判斷，不要因為太短就判 unknown：
   - 對 yes-no 問句：「不會/沒有/沒了/叫不醒」＝否定；「有/會/對/是」＝肯定。
     例：S3 問意識「叫得醒嗎」→「不會」＝consciousness=no；「有」＝consciousness=yes。
     例：S4 問呼吸→「沒有」＝breathing=absent；「有」＝breathing=unclear（有呼吸但沒描述，要追問）。
   - 對指令句（S5 擺位、S6 壓胸的每一步）：「好/好了/有/嗯/OK/可以」或完成式「我早就…了/
     已經…好了」＝step_done=true（做好這一步、等下一步）。
   few-shot：
     狀態 s3、句子「不會」→ {"consciousness":"no", ...}
     狀態 s3、句子「有」→ {"consciousness":"yes", ...}
     狀態 s4、句子「有」→ {"breathing":"unclear", ...}
     狀態 s4、句子「沒有」→ {"breathing":"absent", ...}
     狀態 s5、句子「好」→ {"step_done":true, ...}
     狀態 s5、句子「我早就扣起來了」→ {"step_done":true, ...}
"""

# 各狀態「當前問句在問什麼」的簡述，注入 prompt 讓 LLM 能解讀極簡回答。
_STATE_QUESTION_HINT: dict[State, str] = {
    State.S1: "你正在確認對方是否要救護車（yes-no）。",
    State.S2: "你正在問地址。",
    State.S3: "你正在問傷患有無意識/反應（yes-no）：叫得醒嗎、有沒有反應。",
    State.S4: "你正在問傷患有無正常呼吸（yes-no）：胸口有沒有起伏、有沒有在呼吸。",
    State.S5: "你正在逐步下擺位指令，等民眾做完這一步回覆（好/做好了＝step_done）。",
    State.S6: "你正在指導壓胸，民眾多半在數數或回應鼓勵。",
}


def _build_prompt(text: str, state: State, faq_intents: dict[str, str]) -> str:
    faq_lines = "\n".join(f"   - {fid}: {desc}" for fid, desc in faq_intents.items())
    hint = _STATE_QUESTION_HINT.get(state, "")
    return (
        _SYSTEM_PROMPT
        + f"\n目前對話狀態：{state.value}。當前問句在問：{hint}\n"
        + "（請特別依此解讀單字/極簡回答。）\n"
        + "課堂常見問題清單（faq_id: 說明）：\n"
        + faq_lines
        + f"\n\n請分類這句話：「{text}」"
    )


def _to_slot_value(raw: Optional[str]) -> Optional[SlotValue]:
    if not raw or raw == "unknown":
        return None
    try:
        return SlotValue(raw)
    except ValueError:
        return None


class GeminiIntentClassifier(LLMProvider):
    def __init__(
        self,
        model_id: str,
        project: str,
        location: str,
        faq_intents: dict[str, str],
        timeout_s: float = 6.0,
    ) -> None:
        self.model_id = model_id
        self.project = project
        self.location = location
        self.faq_intents = faq_intents
        self.timeout_s = timeout_s
        self._client = None
        self._available: Optional[bool] = None
        self._valid_faq_ids = set(faq_intents.keys())

    def _ensure_client(self):
        if self._client is None:
            from google import genai  # 延遲匯入：LLM 停用時不需要此依賴

            # vertexai=True + project/location；認證走 ADC（GOOGLE_APPLICATION_CREDENTIALS）
            self._client = genai.Client(vertexai=True, project=self.project, location=self.location)
        return self._client

    def available(self) -> bool:
        """探測可用性：需有認證環境變數，且能建立 client。快取結果避免重複探測。"""
        if self._available is not None:
            return self._available
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            self._available = False
            return False
        try:
            self._ensure_client()
            self._available = True
        except Exception:
            self._available = False
        return self._available

    def list_models(self) -> list[str]:
        """列出可用模型（執行時確認 model_id 有效／自動選 flash-lite 級）。失敗回空。"""
        try:
            client = self._ensure_client()
            names = []
            for m in client.models.list():
                name = getattr(m, "name", None) or getattr(m, "display_name", None)
                if name:
                    names.append(str(name))
            return names
        except Exception:
            return []

    def classify_intent(self, text: str, state: State, context: Optional[dict] = None) -> IntentResult:
        """呼叫 Gemini 做 constrained JSON 分類。任何失敗回空低信心結果（降級友善，不拋）。"""
        res = IntentResult(source="llm")
        if not text or not text.strip():
            return res
        try:
            from google.genai import types

            client = self._ensure_client()
            prompt = _build_prompt(text, state, self.faq_intents)
            resp = client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_INTENT_SCHEMA,
                    temperature=0.0,
                ),
            )
            payload = self._extract_json(resp)
            if payload is None:
                return res
            return self._parse_payload(payload, text)
        except Exception as e:  # 逾時／認證／過濾／網路 → 降級
            res.raw = {"error": type(e).__name__, "msg": str(e)[:200]}
            return res

    def _extract_json(self, resp) -> Optional[dict]:
        # google-genai 的 resp.text 在 JSON 模式下即為 JSON 字串
        txt = getattr(resp, "text", None)
        if not txt:
            try:
                txt = resp.candidates[0].content.parts[0].text
            except Exception:
                return None
        try:
            return json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_payload(self, d: dict, text: str) -> IntentResult:
        res = IntentResult(source="llm", raw=d)
        mapping = {
            "wants_ambulance": Slot.WANTS_AMBULANCE,
            "location": Slot.LOCATION,
            "consciousness": Slot.CONSCIOUSNESS,
            "breathing": Slot.BREATHING,
            "positioning_done": Slot.POSITIONING_DONE,
            "compressions_started": Slot.COMPRESSIONS_STARTED,
        }
        for key, slot in mapping.items():
            sv = _to_slot_value(d.get(key))
            if sv is not None:
                res.slots[slot] = sv
        faq_id = d.get("faq_id")
        if faq_id and faq_id in self._valid_faq_ids:
            res.faq_id = faq_id
        res.end_signal = bool(d.get("end_signal", False))
        res.step_done = bool(d.get("step_done", False))
        try:
            res.confidence = float(d.get("confidence", 0.0))
        except (TypeError, ValueError):
            res.confidence = 0.0
        return res


# ── 層 4：受約束即時生成 ─────────────────────────────────────────
_LAYER4_SYSTEM = """你是台灣 119 派遣員，正在電話中引導民眾對倒下的人做 CPR。民眾剛講了一句你無法歸類的話。
請只回一句「安撫並把話題拉回」的短句，嚴格遵守下列規則（違反任何一條都不行）：
- 不超過 {max_chars} 個字。
- 【禁止超前流程】只能講「現在這一步」的事。以下狀態語境明確指出哪些動作尚未開始，
  你絕對不可提到任何「尚未開始的操作」（例如學員還沒開始壓胸時，不可講壓胸、按壓、數數）：
    {state_context}
- 【禁止新醫療指示】不談藥物、不改處置、不做診斷、不提 AED／人工呼吸／電擊／翻身等。
- 【不要複誦問句】系統會在你這句話之後自動重問當前問題，所以你不要自己把問題再講一遍，
  只要安撫情緒＋承接對方的話＋一句「我們先照著做」這類拉回即可。
- 台灣口語，冷靜堅定，不要客套廢話。
民眾剛說：{utterance}
只輸出那一句話本身，不要引號、不要解釋。"""


class GeminiLayer4Generator:
    """層 4 生成器。可呼叫物件：generate(utterance, question, state_context) → str|None。

    每次生成（原句＋當前步驟＋狀態語境＋生成句）寫入 log_dir 一個 JSON 檔，待維護者課後
    審核（SPEC 層 4：好的轉正為 FAQ 台詞）。失敗一律回 None，讓引擎降級為層 2。
    注意：生成後的「超前流程動作」把關由引擎 layer4_text_violates 負責（引擎才知道狀態），
    本生成器只負責在 prompt 給足約束並產文。
    """

    def __init__(
        self,
        classifier: GeminiIntentClassifier,
        max_chars: int = 40,
        log_dir: Optional[Path] = None,
    ) -> None:
        self._clf = classifier  # 重用同一 client／認證
        self.max_chars = max_chars
        self.log_dir = Path(log_dir) if log_dir else None

    def __call__(self, utterance: str, question: str, state_context: str = "") -> Optional[str]:
        return self.generate(utterance, question, state_context)

    def generate(self, utterance: str, question: str, state_context: str = "") -> Optional[str]:
        try:
            from google.genai import types

            client = self._clf._ensure_client()
            prompt = _LAYER4_SYSTEM.format(
                max_chars=self.max_chars,
                state_context=state_context or "（急救進行中，只安撫、不新增任何操作指示）",
                utterance=utterance,
            )
            resp = client.models.generate_content(
                model=self._clf.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.4, max_output_tokens=120),
            )
            text = getattr(resp, "text", None)
            if not text:
                return None
            text = text.strip().strip("「」\"'").strip()[: self.max_chars]
            if text:
                self._log(utterance, question, state_context, text)
            return text or None
        except Exception:
            return None

    def _log(self, utterance: str, question: str, state_context: str, generated: str) -> None:
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            rec = {
                "ts_utc": ts,
                "utterance": utterance,
                "question": question,
                "state_context": state_context,
                "generated": generated,
                "model": self._clf.model_id,
            }
            (self.log_dir / f"layer4_{ts}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # 記錄失敗不影響對話
