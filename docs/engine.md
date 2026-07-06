# 開發者文件：引擎架構與文字模式用法

本文件說明 CPR 派遣員訓練工具對話引擎（`server/`）的架構設計、模組職責、FSM 狀態機、五層防禦機制、三 Provider 介面、設定項目，以及文字模式 harness 的實際用法。適合開發者在修改或擴充引擎前閱讀。

產品層決策（情境定位、台詞規則、演進路線）見 [`../SPEC.md`](../SPEC.md)；本文件聚焦「程式碼如何實現這些決策」。

---

## 一、架構總覽

整體分層：

```
┌─────────────────────────────────────────────────────────┐
│  providers/（STT／LLM／TTS 三個 Provider 的抽象與實作）   │
│  - 碰真時鐘、真音訊、真 LLM API、真子程序                 │
│  - 各自可獨立替換（切換實作＝改 config 一處）              │
└───────────────────────┬───────────────────────────────────┘
                        │ 由 driver 接起來
┌───────────────────────▼───────────────────────────────────┐
│  runtime.py（driver）                                     │
│  - IntentPipeline：分類編排（fastpath → LLM →（降級）keyword）│
│  - TextModeDriver／VoiceDriver：把 Provider 事件餵給引擎，  │
│    並把引擎回傳的 SpeakAction 交給 TTS 執行                │
└───────────────────────┬───────────────────────────────────┘
                        │ on_utterance(text, result, now) / tick(now)
┌───────────────────────▼───────────────────────────────────┐
│  engine/（DialogueEngine 及其依賴）                        │
│  - 純同步、決定性；不碰音訊、不碰真時鐘、不呼叫 LLM         │
│  - 時間由呼叫端以 now 參數注入；分類結果由呼叫端以          │
│    IntentResult 傳入                                       │
│  - 回傳 SpeakAction 清單（純資料，「決定要播什麼」）        │
└─────────────────────────────────────────────────────────────┘
```

**為何引擎不碰真時鐘／音訊／LLM**：`DialogueEngine`（`server/engine/fsm.py`）把「決策」與「副作用」徹底分離——它只消化文字輸入（`on_utterance`）與時間推進（`tick`），回傳 `SpeakAction` 清單交由 driver 執行實際播放。這帶來兩個直接好處：

1. **可完整單元測試**：測試可用假時鐘（`FakeClock`，見 `server/tests/conftest.py`）精確控制 timeout／S6 插播的觸發時機，並直接構造 `IntentResult` 餵進引擎，完全不需要真的呼叫 LLM 或跑真音訊。
2. **文字模式與語音模式共用同一顆引擎**：`TextModeDriver`（文字 harness、pytest 用）與未來的 `VoiceDriver`（真語音）都只是把不同來源的輸入轉成 `on_utterance`/`tick` 呼叫，引擎本身完全不知道自己是被文字餵還是被語音餵。

---

## 二、模組職責表

| 檔案 | 職責 |
|---|---|
| `server/config.py` | 集中設定：所有 Provider 選擇、模型 id、路徑、逾時／門檻等可調參數，支援環境變數覆蓋。 |
| `server/engine/actions.py` | 定義 `SpeakAction`／`SpeakKind`：引擎「決定要播什麼」的純資料輸出格式。 |
| `server/engine/intents.py` | 狀態（`State`）、slot（`Slot`／`SlotValue`）、意圖分類契約（`IntentResult`），以及 gating 對照表與 canonical 角色標記。 |
| `server/engine/fastpath.py` | `RegexFastPath`：不走 LLM 的即時規則偵測（S6 數數、結束訊號）；`KeywordFallbackClassifier`：LLM 不可用時的最小可用意圖分類（降級路徑）。 |
| `server/engine/script_store.py` | `ScriptStore`：載入台詞庫 YAML，提供依 id 取全文、canonical 依序取、variant／insert／meta 輪替取用。 |
| `server/engine/metrics.py` | `MetricsRecorder`：以 monotonic 時間戳記錄事件流，並計算 SPEC 第六節的衍生指標（辨識 OHCA 時間、開始按壓時間等）。 |
| `server/engine/fsm.py` | `DialogueEngine`：FSM 主體，S0–S7 狀態流轉與五層防禦的核心邏輯。 |
| `server/providers/base.py` | 三個 Provider 的抽象介面（`STTProvider`／`LLMProvider`／`TTSProvider`）與 STT 事件型別。 |
| `server/providers/stt_speechanalyzer.py` | `SpeechAnalyzerSTT`：以 subprocess 驅動 `spike/stt_spike` binary，消費其 stdout JSONL 事件流。 |
| `server/providers/llm_gemini.py` | `GeminiIntentClassifier`：Vertex AI 意圖分類（constrained JSON）；`GeminiLayer4Generator`：層 4 受約束即時生成。 |
| `server/providers/tts.py` | `PrerecordedTTS`（afplay 播預錄）、`SayTTS`（macOS say 後備）、`TextTTS`（文字模式假物）。 |
| `server/runtime.py` | `IntentPipeline`：分類編排（fastpath → LLM →（降級）keyword）；`TextModeDriver`：文字模式 driver；`execute_action`：把 `SpeakAction` 轉譯成實際 TTS 呼叫。 |
| `server/factory.py` | 依 `Config` 組裝引擎與三 Provider 的工廠函式；切換實作只改設定，組裝邏輯集中於此，避免散落各處。 |
| `server/app.py` | 最小 FastAPI 骨架：`/health` 存活探測、`/ws/session` 預留的對話 WebSocket（本階段僅 echo，未接引擎完整整合）。 |
| `server/cli_harness.py` | 文字模式測試 harness：不接真 STT/TTS，用文字完整驅動 FSM，跑通一整場對話並印出 metrics 摘要。 |

---

## 三、FSM 狀態機

### 3.1 狀態總覽（S0–S7）

狀態定義於 `server/engine/intents.py` 的 `State` enum；狀態順序（跳步計算用）為 `STATE_ORDER = [S1, S2, S3, S4, S5, S6]`（S0 固定開場、S7 為終態，皆不列入 gating 序列）。對照 SPEC.md 第四節：

| 狀態 | 意義 | Gating Slot |
|---|---|---|
| S0 | 開場（固定語音：「你好，這裡是天才消防局，請問你要消防車還是救護車？」） | 無 |
| S1 | 確認救護車需求（說消防車 → 禮貌引導回，停留此狀態） | `WANTS_AMBULANCE` |
| S2 | 確認地點（確認句：「好的，地址我記下了，預估5分鐘之後會抵達。」，不複誦地址） | `LOCATION` |
| S3 | 確認意識（叫得醒嗎？有反應嗎？） | `CONSCIOUSNESS` |
| S4 | 確認呼吸（正常呼吸？聽起來怎樣？→ 瀕死喘息判斷）＝★辨識 OHCA 時間戳（進入本狀態即打點，見下） | `BREATHING` |
| S5 | 指導擺位（平躺、跪旁邊、掌根置兩乳頭連線中點、雙手交疊） | `POSITIONING_DONE` |
| S6 | 指導壓胸（★首次按壓時間戳＝首次偵測到學員數數）；非嚴格輪流，計時器每 15–20 秒插播鼓勵／糾正 | `COMPRESSIONS_STARTED` |
| S7 | 學員說「救護人員到了」類結束訊號 → 稱讚＋宣布接手＋結束 | 無（終態） |

值得留意：SPEC 表格把「辨識 OHCA」標在 S4，但實作上是**進入 S5 那一刻**才記 `OHCA_RECOGNIZED` 事件（`fsm.py` 的 `_enter_state`：`if state == State.S5: self.metrics.record(EventType.OHCA_RECOGNIZED, ...)`）。這是因為「辨識 OHCA」在語義上等於「S4 的呼吸判定已完成且判定為無正常呼吸」，而完成判定的時間點就是狀態機推進離開 S4、進入 S5 的那一刻——兩者在實作上等價，只是打點的程式位置選在狀態轉移處。

### 3.2 Slot 模型與跳步

每個推進狀態由一個 gating slot 把關（`GATING_SLOT: dict[State, Slot]`），該 slot 的值須滿足 `slot_satisfies(slot, value)` 才算「通過」該狀態：

- `WANTS_AMBULANCE`：`YES` 才過（`NO` 表示要消防車，停留 S1 引導）。
- `LOCATION`：`PROVIDED` 或 `YES` 皆視為已提供。
- `CONSCIOUSNESS`：`YES` 或 `NO` 皆算「已評估」而通過（本課堂固定情境為無意識假人，不會真的出現「有意識」分支，但實作上仍以「已評估」推進，避免卡死）。
- `BREATHING`：`ABSENT` 或 `AGONAL`（瀕死喘息）視為「無正常呼吸」→ 通過並觸發 OHCA；`NORMAL` 不算通過；`UNCLEAR`（描述模糊）不算通過——播 probe 追問後停在 S4。
- `POSITIONING_DONE` / `COMPRESSIONS_STARTED`：皆須 `YES`。

**跳步（chain-advance）**：意圖分類一次可回傳多個 slot（如學員一句話講完「他沒反應也沒呼吸」同時填了 `CONSCIOUSNESS=NO` 與 `BREATHING=ABSENT`）。引擎收到多 slot 填入後，會呼叫 `_advance_from_current`，從當前狀態逐狀態往前走：

0. 若當前狀態的 gating slot「已填但不滿足」（如 S4 呼吸=`UNCLEAR` 描述模糊），**不推進**——播該狀態的條件釐清句（S4 的 `s4_agonal_probe_c` 追問），停在原狀態等更明確回報。
1. 否則，若當前狀態的 gating slot「剛」被這句話填滿（且滿足），先播該狀態的確認句／條件句（如 S2 拿到地址後的 `s2_addr_confirm_c`；S4 依呼吸值選 `s4_agonal_ruling_c`／`s4_agonal_ruling_v01`）。
2. 逐狀態往前，每進一個新狀態就播該狀態的 canonical，直到走到「第一個 gating slot 尚未滿足」的狀態（含）為止並停下等待回應；若一路都滿足則走到 S6 為止（S6 是壓胸階段，不再自動往前）。

此外有一條蘊含規則：若一句話直接填了 `COMPRESSIONS_STARTED=YES`（如「他沒反應沒呼吸我在壓了」），引擎會自動補填 `POSITIONING_DONE=YES`（`_advance_with_slots` 中的隱含邏輯），因為「已開始壓胸」在臨床上蘊含「已就位」，讓這種一句話跳到底的表述不會被 S5 的 gating 卡住。

### 3.3 Canonical 四種角色（含條件選句）

每個狀態在台詞庫中的 canonical 句依語意分成四種角色，決定「什麼情境播哪句」，定義於 `intents.py`：

1. **詢問句**（`ENTRY_QUESTION_IDS: dict[State, set[str]]`）：用來問學員以取得 slot 的問句。若跳步進入該狀態時 slot 已被同一句話填滿，就**抑制**這句（不問已知答案）。目前定義：
   - S2：`{"s2_addr_ask_c"}`
   - S3：`{"s3_consciousness_c"}`
   - S4：`{"s4_breathing_c"}`（只問有無正常呼吸；瀕死喘息追問改為條件句，見下）

2. **確認句**（`CONFIRM_IDS: dict[State, set[str]]`）：只在該狀態 gating slot **已滿足**時才播（拿到答案後的回應）；slot 尚空時抑制，因為還沒拿到答案不能先講「記下了」。目前定義：
   - S2：`{"s2_addr_confirm_c"}`（地址確認）

3. **條件句**（`CONDITIONAL_LINES: dict[State, list[tuple[str, set[SlotValue]]]]`）：只在該狀態 gating slot 取**特定值**時才播（比確認句更細——依 slot 的哪個值決定播哪一句）。用於同一狀態下不同回報走不同台詞的臨床分歧。條件句可指向 variant id（不在 canonical 序列內），播放時以 id 直接取用。目前定義（S4 呼吸判定，依台詞庫審定備註）：
   - `BREATHING=UNCLEAR`（描述模糊）→ `s4_agonal_probe_c`（追問「是不是很久才喘一下」以釐清是否瀕死喘息；播完停在 S4 等更明確回報）。
   - `BREATHING=AGONAL`（瀕死喘息）→ `s4_agonal_ruling_c`（「這種喘不算正常呼吸…」）。
   - `BREATHING=ABSENT`（明確沒呼吸）→ `s4_agonal_ruling_v01`（「他沒有在正常呼吸，不要再等了…」；避免用「這種喘」指涉不存在的描述）。

4. **always-voice**（其餘，未列入上述任何表者）：一律播，不受 slot 狀態影響。例如 S1 的救護車確認、S5／S6 的指令句、S7 的交接句。

判斷邏輯集中在 `fsm.py`：`_speak_state_canonicals` 對每一句 canonical 先跳過 `branch` 分支句與條件句（條件句改由 `_conditional_actions` 依 slot 值選播），再依詢問句／確認句表決定是否抑制；`_conditional_actions` 則比對當前 gating slot 值選播對應條件句。

**BREATHING slot 值空間**：`ABSENT`（完全沒有）／`AGONAL`（有動作但瀕死喘息）／`UNCLEAR`（描述模糊，需追問）／`NORMAL`。`slot_satisfies` 只認 `ABSENT`／`AGONAL` 為通過 S4；`UNCLEAR` 不通過——填了但不滿足，引擎播 probe 後停在 S4（見 `_advance_from_current` 的步驟 0）。分類器（Gemini schema 與 `KeywordFallbackClassifier`）皆能產出這四個值。

**chain-advance 的抑制與選句規則**：靠這四種角色，跳步時才能做到「不重複問已知答案，但仍講完承接／判定／指令句，且依回報值選對判定句」。例如：學員一句「他沒反應也沒呼吸」同時填意識與呼吸——S3 意識已滿→抑制 `s3_consciousness_c` 並續進 S4；S4 呼吸為 `ABSENT`→抑制詢問句與 probe、依條件選 `s4_agonal_ruling_v01`→續進 S5 播擺位→S5 slot 未滿→停。

---

## 四、五層防禦

當學員的話無法直接推進狀態（unknown、信心不足、或命中特殊情境）時，引擎依序啟用以下防禦層。各層邏輯集中於 `fsm.py`，觸發判斷順序見 `on_utterance` 主流程。

| 層 | 觸發條件 | 台詞來源 | 對應程式 |
|---|---|---|---|
| 1 | 多 slot 一次填（跳步），為正常能力而非「防禦」 | 預錄 canonical | `_advance_with_slots` / `_advance_from_current` |
| 2 | unknown 且無層 4 生成結果：首次 unknown → clarify；連續 2 次 unknown → takeover | 台詞庫 `meta_phrases`（`clarify`／`takeover`／`bridge`），輪替取用 | `_handle_layer2` |
| 3 | 命中課堂 FAQ（`result.faq_id` 非空） | 台詞庫 `faq` 答句＋答完自動 bridge 重問當前狀態問句 | `_handle_faq` |
| 4 | unknown 但語句非空、`layer4_enabled` 且已注入生成器：先播 filler 掩飾延遲，再播 ≤`layer4_max_chars` 字的即時生成安撫承接句；生成失敗或不可用則降級為層 2 | 台詞庫 `filler` ＋ LLM 即時生成（`Layer4Generator`） | `_handle_unknown` |
| 5 | 沉默分級 timeout（5s→l1，10s→l2，各級只播一次直到有活動 reset）；技術故障（driver 主動呼叫 `tech_fault()`） | 台詞庫 `meta_phrases`（`timeout_l1`／`timeout_l2`／`tech_fault`） | `_check_timeout` / `tech_fault` |

補充規則：

- **優先順序**：`on_utterance` 內的判斷順序是——結束訊號（任何狀態最優先，直達 S7）→ 層 3 FAQ → S0/S1 消防車分支（`fire_redirect`，停留不前進）→ 層 1 跳步 → S6 特例（`counting` 即視為壓胸進行中，不打斷）→ 層 4／層 2（`_handle_unknown`）。
- **層 5 的例外狀態**：S0（開場）、S6（壓胸中，非嚴格輪流，互動由插播計時器負責）、S7（終態）不套用沉默 timeout（`_check_timeout` 開頭即檢查並跳過）。
- **層 2／層 3 之後的重問**：`_reask_current` 會先播 bridge 前綴，再輪替播當前狀態的主問句（canonical 或其 variant，同輪不重複）；但 S0／S6／S7 這三個狀態沒有「問句」可重問，故不重問。
- **層 4 的安全限制**：生成句一律先經 `.strip()[:max_chars]` 截斷，且系統 prompt（見 `llm_gemini.py`）明文禁止生成任何新醫療指示，只能安撫、承接、拉回話題；每次生成會寫入 `logs/layer4/`（見第六節設定）供課後審核，好的轉正為 FAQ 台詞。
- **S6 的雙重保險**：引擎在 `on_utterance` 開頭會對每句輸入**再跑一次** `RegexFastPath`（`_merge_fastpath`），不論 driver 端的 `IntentPipeline` 是否已經處理過，確保 S6 數數／結束訊號這條延遲關鍵路徑不依賴外部分類是否正確跑過。

---

## 五、三 Provider 介面與實作

### 5.1 抽象介面（`server/providers/base.py`）

```python
class STTProvider(abc.ABC):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def events(self) -> AsyncIterator[STTEvent]: ...

class LLMProvider(abc.ABC):
    def classify_intent(self, text: str, state: State, context: Optional[dict] = None) -> IntentResult: ...
    def available(self) -> bool: ...

class TTSProvider(abc.ABC):
    def speak(self, utterance_id: str) -> None: ...
    def speak_dynamic(self, text: str) -> None: ...
```

`STTEvent` 型別對齊 `spike/` 的 JSONL 契約，`STTEventType` 包含 `VOLATILE`（未定稿即時片段）、`FINAL`（定稿片段，送 FSM 的主要輸入）、`ENDPOINT`（語義斷句）、`STATUS`（診斷）、`ERROR`（進程異常）。

### 5.2 各實作重點

**`SpeechAnalyzerSTT`**（`stt_speechanalyzer.py`）：以 `asyncio.create_subprocess_exec` 拉起 `spike/stt_spike` binary（帶 `--locale`／`--silence-ms`／`--flush-ms`），開兩個背景 task 分別消費 stdout（逐行解析 JSONL 轉 `STTEvent`）與 stderr（吞掉診斷輸出，避免管線塞滿）。收尾時送 `SIGINT` 讓 helper 走其內建的優雅收尾（helper 保證 3 秒內硬退出），本 Provider 再給 `shutdown_grace_s`（預設 5 秒）裕度等待，逾時才 `SIGKILL`，且一律 `wait()` 回收避免殭屍進程。設計上明確**不重新發明** spike 已處理過的工程問題（週期 flush、VAD 判斷、音訊格式轉換保證），只負責「正確消費事件流」與「管理進程生命週期」。

**`GeminiIntentClassifier`**（`llm_gemini.py`）：走 Vertex AI（`google-genai`，`vertexai=True`），認證透過標準環境變數 `GOOGLE_APPLICATION_CREDENTIALS`（ADC），程式碼本身不引用金鑰內容或路徑。呼叫 `generate_content` 時帶 `response_mime_type="application/json"` 與明確的 `response_schema`（`_INTENT_SCHEMA`），做 constrained decoding，一次回傳多個 slot 欄位＋`confidence`＋可選的 `faq_id`／`end_signal`。系統 prompt 明確要求「只做分類、不生成台詞」，並特別提示諧音容錯（電話報案語境常見「救護人員」被辨識成「客戶人員」等）。`available()` 會檢查環境變數是否存在並嘗試建立 client，結果快取避免重複探測；`classify_intent` 任何例外（逾時／認證失敗／內容過濾）皆捕捉並回傳空的低信心結果，不拋例外中斷對話。同檔另有 `GeminiLayer4Generator`，是層 4 即時生成的實作，重用同一個 `GeminiIntentClassifier` 的 client／認證，每次生成寫入 `log_dir` 一個 JSON 檔待審核。

**`PrerecordedTTS` / `SayTTS`**（`tts.py`）：`PrerecordedTTS.speak(id)` 走 `subprocess.run(["afplay", ...])` 播放 `<audio_dir>/<id>.wav`；若音檔缺失則退回 `fallback.speak_dynamic(text)`（保證不啞火）。`speak_dynamic`（層 4 動態生成句）一律委派給 fallback（預設 `SayTTS`，即 `subprocess.run(["say", "-v", voice, text])`）。另有 `TextTTS`：文字模式假物，不出聲，只把播放請求收進 `spoken` 清單並可選擇性呼叫 `on_speak` callback，供 harness 即時列印。

### 5.3 切換實作＝改 config 一處

三個 Provider 的選擇皆由 `config.py` 對應的 `provider` 欄位決定（環境變數可覆蓋），`factory.py` 的 `build_llm` / `build_tts` 依這個欄位分派建構哪個實作：

- `LLMConfig.provider`：`"gemini"` 走 `GeminiIntentClassifier`；`"none"` 回傳 `None`（純降級路徑）。`build_llm` 在建構失敗時也會靜默回傳 `None`，讓系統自動走降級而非拋錯中斷。
- `TTSConfig.provider`：`"prerecorded"` 走 `PrerecordedTTS`（另組 `SayTTS` 當 fallback）；文字模式（`text_mode=True`）或 `provider="text"` 一律回傳 `TextTTS`；未知 provider 值也安全退回 `TextTTS`。
- `STTConfig.provider`：目前 `"speechanalyzer"` 對應 `SpeechAnalyzerSTT`（`cli_harness.py` 的 `--voice` 模式中直接依此欄位建構）。

也就是說，新增或替換一個 Provider 實作時，只需要：(1) 實作對應抽象介面；(2) 在 `factory.py` 的對應 `build_*` 函式加一個分支；(3) 在 `config.py` 的 `provider` 欄位文件補充新選項字串。呼叫端（`cli_harness.py`、未來的 `app.py`）完全不需要改動，因為它們只依賴 `factory.py` 組裝出來的抽象介面。

---

## 六、設定

所有可調欄位集中於 `server/config.py`，皆遵循「環境變數覆蓋預設值」的模式（`_env` / `_env_int` helper）。**金鑰一律不寫入本檔或任何程式碼**，LLM 認證只透過標準環境變數 `GOOGLE_APPLICATION_CREDENTIALS` 讀取，本檔完全不引用其值。

| 分組 | 欄位 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|---|
| `Config` | `locale` | `CPR_LOCALE` | `zh-TW` | 資產／台詞庫 locale（連字號格式），對應 `content/<locale>/` 與 `assets/audio/<locale>/`。 |
| | `scenario` | `CPR_SCENARIO` | `adult` | 情境（v1 僅成人）；決定 `content/<locale>/<scenario>_script.yaml` 路徑。 |
| `STTConfig` | `provider` | `CPR_STT_PROVIDER` | `speechanalyzer` | STT 實作選擇；`"text"` 為文字模式假 STT。 |
| | `helper_path` | `CPR_STT_HELPER` | `<repo>/spike/stt_spike` | spike binary 路徑（可執行檔，非原始碼）。 |
| | `stt_locale` | `CPR_STT_LOCALE` | `zh_TW` | 傳給 helper 的 locale（SDK 用底線格式，與資產 locale 的連字號格式分開，避免混用）。 |
| | `silence_ms` | `CPR_STT_SILENCE_MS` | `600` | VAD 靜音門檻（毫秒）。 |
| | `flush_ms` | `CPR_STT_FLUSH_MS` | `700` | 週期 flush 間隔（毫秒）。 |
| | `shutdown_grace_s` | （無，程式常數） | `5.0` | 收到 SIGINT 後等 helper 自行收尾的秒數，逾時才 SIGKILL。 |
| `LLMConfig` | `provider` | `CPR_LLM_PROVIDER` | `gemini` | LLM 實作選擇；`"none"` 為純降級路徑。 |
| | `model_id` | `CPR_LLM_MODEL` | `gemini-2.5-flash-lite` | 意圖分類模型 id。 |
| | `project` | `CPR_GCP_PROJECT` | `atls-tts` | GCP 專案 id（與 TTS 批次共用同一 service account）。 |
| | `location` | `CPR_GCP_LOCATION` | `us-central1` | Vertex AI 區域。 |
| | `request_timeout_s` | `CPR_LLM_TIMEOUT_S` | `6` | 意圖分類逾時（秒），超過視為 LLM 不可用，走該狀態澄清句。 |
| | `confidence_threshold` | `CPR_LLM_CONF_THRESHOLD` | `0.55` | 信心門檻，低於此值 FSM 不前進、播澄清句。 |
| `TTSConfig` | `provider` | `CPR_TTS_PROVIDER` | `prerecorded` | TTS 實作選擇；`"text"` 為文字模式（不出聲，印 id＋全文）。 |
| | `audio_root` | `CPR_AUDIO_ROOT` | `<repo>/assets/audio` | 預錄音檔根目錄，實際檔案在 `<audio_root>/<locale>/<id>.wav`。 |
| | `fallback_provider` | `CPR_TTS_FALLBACK` | `say` | 層 4 即時生成的後備 TTS。 |
| | `say_voice` | `CPR_SAY_VOICE` | `Meijia` | macOS `say -v` 使用的語音。 |
| `Layer4Config` | `enabled` | `CPR_LAYER4_ENABLED` | `1`（真） | 層 4 受約束即時生成是否啟用；設為 `0`／`false`／`False` 停用（降級為層 2）。 |
| | `max_chars` | （無，程式常數） | `40` | 生成句上限字數。 |
| | `log_dir` | （無，程式常數） | `<repo>/logs/layer4` | 每次生成留存待課後審核的目錄（開發者產物，不入 repo；見 `.gitignore`）。 |
| `TimeoutConfig` | `level1_s` | （無，程式常數） | `5.0` | 第一級沉默 timeout（秒）。 |
| | `level2_s` | （無，程式常數） | `10.0` | 第二級沉默 timeout（秒）。 |
| `S6Config` | `insert_min_s` | （無，程式常數） | `15.0` | S6 插播計時器下限（秒）。 |
| | `insert_max_s` | （無，程式常數） | `20.0` | S6 插播計時器上限（秒）。 |

---

## 七、文字模式 harness 用法

`server/cli_harness.py` 不接真 STT/TTS，用文字完整驅動 FSM，可跑通一整場對話並印出 metrics 摘要。以下範例假設已 `cd` 到 repo 根目錄，並使用開發機上已安裝依賴的 Python 3.12 環境（`~/presentation-env/bin/python3`；亦可用任何已 `pip install -r requirements.txt` 的 Python 3.12 環境）。

### 7.1 旗標

| 旗標 | 說明 |
|---|---|
| `--voice` | 接真 STT/TTS Provider（本階段僅驗證「能啟動」，真語音整測留待 UI 階段）。 |
| `--no-llm` | 停用 LLM，強制走降級路徑（`RegexFastPath`＋`keyword` 後備）。 |
| `--seed <int>` | 固定亂數種子，讓變體／插播輪替可重現（決定性測試用）。 |
| `--script-file <path>` | 從檔案讀輸入（每行一句或一個 `/` 指令），取代 stdin；檔案內以 `#` 開頭的行視為註解。 |
| `--dump <path>` | 結束時把 metrics 事件流寫成 JSONL 到此路徑。 |
| `--step <float>` | 每句 final 之間推進的虛擬秒數（預設 `0.5`）。 |

互動指令（輸入以 `/` 開頭的行）：

| 指令 | 說明 |
|---|---|
| `/wait <秒>` | 推進虛擬時鐘 N 秒並週期 pump `tick`（模擬沉默觸發分級 timeout；S6 插播計時），預設 5 秒，每 0.5 秒 pump 一次。 |
| `/fault` | 模擬技術故障，觸發層 5 `tech_fault` 台詞。 |
| `/state` | 印出目前狀態、已填 slot、是否結束（除錯用）。 |
| `/dump <路徑>` | 把 metrics 事件流寫成 JSONL（預設 `sessions/harness_dump.jsonl`）。 |
| `/quit` | 結束並印出 metrics 摘要。 |

### 7.2 執行範例

**範例一：互動模式（stdin 逐行輸入），停用 LLM 走降級路徑**

```bash
cd ~/Projects/cpr-dispatcher-trainer
~/presentation-env/bin/python3 server/cli_harness.py --no-llm --seed 42
```

啟動後會先印出開場白，接著逐行輸入學員的話（例如先輸入「要救護車」，再輸入「他沒反應也沒呼吸」），每行 Enter 送出視為一句 final。輸入 `/quit` 結束並看 metrics 摘要。

**範例二：用腳本檔餵一整場對話（決定性、可重跑比對）**

先準備一個腳本檔（每行一句學員的話，或一個指令；`#` 開頭為註解），例如專案內的 `sessions/demo_script.txt`（`sessions/` 已被 `.gitignore` 排除，屬本機執行期產物）：

```
# 示範腳本：完整走一輪 happy path
要救護車
台北市中山區中山北路一段1號
他沒反應也沒呼吸
我跪好了手也放好了
1 2 3 4 5
救護人員到了
/quit
```

執行：

```bash
cd ~/Projects/cpr-dispatcher-trainer
~/presentation-env/bin/python3 server/cli_harness.py --script-file /path/to/demo_script.txt --seed 42 --dump sessions/demo_dump.jsonl
```

結束時會印出 metrics 摘要（辨識 OHCA 時間、開始按壓時間、EMS 抵達時間、各狀態停留時間、五層防禦觸發統計），並把事件流寫入 `--dump` 指定的路徑。

**範例三：用真的 LLM（需已設定 `GOOGLE_APPLICATION_CREDENTIALS`），並手動測試沉默 timeout**

```bash
cd ~/Projects/cpr-dispatcher-trainer
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-service-account.json
~/presentation-env/bin/python3 server/cli_harness.py --seed 1
```

互動中可輸入 `/wait 12` 觀察分級 timeout（5 秒觸發 l1、10 秒觸發 l2）；輸入 `/state` 隨時檢視目前狀態與已填 slot；輸入 `/fault` 模擬技術故障看 `tech_fault` 台詞。

---

## 八、測試

於 repo 根目錄執行：

```bash
cd ~/Projects/cpr-dispatcher-trainer
~/presentation-env/bin/python3 -m pytest
```

`pytest.ini` 已設定 `testpaths = server/tests`，測試檔涵蓋範圍（檔名即可看出）：

- `test_transitions.py`：S0–S7 狀態流轉與跳步。
- `test_defenses.py`：五層防禦各層觸發條件。
- `test_s6_and_metrics.py`：S6 壓胸階段（插播計時、數數偵測）與 metrics 衍生指標計算。
- `test_script_and_fastpath.py`：`ScriptStore` 台詞取用／輪替，以及 `RegexFastPath` 規則。
- `test_acceptance_flow.py`：端到端驗收流程（完整一場對話）。
- `test_runtime_and_providers.py`：`IntentPipeline`／driver 編排邏輯與 Provider 假物整合。
- `test_app.py`：FastAPI 骨架（`/health` 等）。

所有測試以真實台詞庫（`content/zh-TW/adult_script.yaml`）為資料來源，但完全不接真 STT/LLM/TTS：時鐘用可控假時鐘（`conftest.py` 的 `FakeClock`）讓 metrics 時間戳與 timeout／S6 計時可精確斷言；分類器可直接構造 `IntentResult` 餵進引擎（繞過 LLM），或用 `KeywordFallbackClassifier`；rng 固定種子讓變體／插播輪替可重現。

---

## 九、降級路徑

當 LLM 不可用時（未設定 `GOOGLE_APPLICATION_CREDENTIALS`、配額用盡、逾時、或建構失敗），系統仍能完整跑完文字模式的 happy path，機制如下（見 `runtime.py` 的 `IntentPipeline.classify`）：

1. **先跑 `RegexFastPath`**：不論 LLM 是否可用，每句輸入一律先過 `RegexFastPath`。若命中結束訊號，或在 S6 狀態命中數數，直接短路回傳，完全不呼叫 LLM（延遲關鍵路徑不依賴 LLM）。
2. **嘗試 LLM（若已設定且可用）**：`self.llm.available()` 為真時才呼叫 `classify_intent`；若 LLM 回傳的結果非 unknown 或信心大於 0，就採用它（並把 fastpath 偵測到的 `counting` 弱訊號合併進去）。
3. **降級：`KeywordFallbackClassifier`**：當 LLM 為 `None`（`config.py` 設 `CPR_LLM_PROVIDER=none`，或 `factory.build_llm` 因缺依賴／建構失敗回傳 `None`）、或 LLM 不可用、或 LLM 回傳了空結果，一律退回關鍵字比對分類器。這個分類器刻意保守——只在關鍵字明確命中時填 slot（信心固定給 `0.6`，剛好過門檻但標示來源為 `keyword_fallback`），涵蓋各狀態最典型的表述（如 S1「救護車」/「消防車」、S3「沒反應」/「有反應」、S4「沒呼吸」/「喘」/「正常呼吸」等）。未命中則回傳空結果，交由引擎走層 2 澄清／接管防禦流程。

`--no-llm` 旗標（`cli_harness.py`）等同於強制模擬「LLM 完全不可用」的情境，方便開發時驗證純降級路徑（`RegexFastPath`＋`KeywordFallbackClassifier`）是否仍能推進完整流程，不需要真的斷網或撤除認證。

同樣地，層 4（受約束即時生成）在 LLM 不可用時也會自動降級：`factory.build_layer4` 只在 `cfg.layer4.enabled` 為真且 LLM 存在時才建立生成器，否則回傳 `None`；引擎的 `_handle_unknown` 在生成器為 `None`、生成失敗（例外）、或生成結果為空時，一律無縫降級為層 2 元台詞（clarify／takeover），不會讓對話卡住或拋錯。
