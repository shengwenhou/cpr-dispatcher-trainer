# 進度與交接記錄

## 2026-07-03 — 規劃定案、repo 建立

### 已完成
1. 教材四份 PDF → Markdown（pymupdf4llm，品質驗證通過）；私有文件區的原 PDF 已刪。
2. 完整架構分析：Claude Opus（deep-reasoner）與 OpenAI Codex 平行獨立分析後整合，結論收斂——本機網頁 app + FSM/LLM hybrid + 全預錄 TTS + on-device STT。報告：`docs/架構分析_20260703.md`。
3. 使用者規格定案（六點決定 + 意外輸入五層防禦設計確認），全部整理進 `SPEC.md`。
4. 基礎設施：
   - 修復並重裝 codex CLI（0.142.5）
   - 安裝 GitHub CLI（gh 2.96.0）
   - 確認遠端工作鏈就緒（Tailscale 組網、sshd、tmux；細節見維護者私人筆記）
   - 建立本 repo：`~/Projects/cpr-dispatcher-trainer/`
   - GitHub 授權完成 → repo 已建立並推送：https://github.com/shengwenhou/cpr-dispatcher-trainer

### 補記（同日稍晚）
- 建立專案 wiki（繁中六頁：Home／Introduction／Maintenance／Roadmap／Plan／Tech-Debt）。因 GitHub Free 方案的 private repo 不支援 Wiki 功能，內容暫居主 repo `wiki/` 目錄（見 `wiki/README.md` 說明）；本地 `~/Projects/cpr-dispatcher-trainer.wiki/` git repo 已備妥，待 repo 公開後一個 push 遷移。
- 準備公開（make public）：加入 MIT License（署名 Sheng-Wen Hou）+ README 授權區塊與醫療教學免責聲明；依使用者決定移除兩份 In-House 第三方內部文件（Dispatch Protocols、QI Form；正本保留於私有文件區）並重寫 git 歷史徹底清除；全 repo 淡化機器名稱、內網 IP、私有路徑。歷史已 squash 為單一乾淨 commit 並 force push。

### 補記（repo 公開後）
- 使用者將 repo 轉為 public → 正式 GitHub Wiki 上線（六頁 + 側邊欄），主 repo `wiki/` 暫居目錄移除；wiki 編輯正本自此為 `~/Projects/cpr-dispatcher-trainer.wiki/`（push 至 `.wiki.git`）。
- 多語言需求評估完成（日／泰／印尼／馬來／英，目標 v3.0）：評估表入 wiki Roadmap；i18n 設計紀律入 SPEC 第八之一節（v1.0 起字串外部化、locale 參數化）。

### 待辦（下次 session 從這裡接手）
1. **Spike：SpeechTranscriber(zh_TW) 實測**（壓胸數數階段為重點）——SPEC 第九節第 1 項。
3. 台詞庫草稿（成人情境全套 + 元台詞 + FAQ 初稿）→ 交使用者審定。
4. FSM 引擎骨架 + Provider 介面。

### 環境備忘
- 開發機：Apple Silicon Mac（macOS 26）；主機與網路細節見維護者私人筆記。
- 遠端工作：筆電經 Tailscale 以 ssh + tmux 連入開發機。
- ollama 尚未安裝（v2.0 地端化時才需要，屆時裝完記得跑環境快照）。

## 2026-07-05 — Phase 0 Spike 完成：SpeechTranscriber(zh_TW) ✅ 通過

### 結論

**macOS 26 SpeechAnalyzer/SpeechTranscriber（zh_TW，on-device）通過全部三項實測，定案為 v1.0 STT，不啟動 whisper.cpp 備案。**維護者真人實測（藍牙無線麥克風、16kHz）。

| 測項 | 結果 | 數據 |
|---|---|---|
| 1. 報案短句正確率 | ✅ 通過 | 10 句關鍵意圖詞 9.5/10 可辨。錯誤型態為諧音（「救護人員」→「客戶人員」、「倒」→「早」），意圖 pattern 仍可匹配；地址句最弱（「忠孝東路」→「校東路」），但設計上不複誦地址、交 LLM slot-filling 容錯 |
| 2. 連續大聲數數（壓胸模擬，最大風險） | ✅ 通過 | 40+ 秒三輪「一下兩下…三十」STT 全程存活；週期 flush 每 ~0.8s 穩定切段；數數開始 1–2s 內即有辨識活動（滿足起壓時間戳需求）。逐字正確率低（「八下」→「下巴」等諧音）但無妨：偵測規則用「含數字或『下』」即可 |
| 3. 端點延遲與可調性 | ✅ 通過 | 體感延遲＝靜音門檻＋處理 ~30–50ms：門檻 500ms → ~530ms（SPEC 300–600ms 預算內）；800ms → ~830ms（更穩）。端點與句子一一對應（10 句 10 個、5 句 5 個）。建議課堂預設 500–600ms |

### 關鍵工程發現（FSM／正式版 STT 模組必讀）

1. **SpeechAnalyzer 是 finalize 驅動**：不主動 `finalize(through:)` 就零輸出（連 volatile 都沒有）。正式版必須沿用 spike 的「週期 flush（700ms）＋ VAD 靜音 finalize」雙機制。
2. **results 消費迴圈必須在音訊啟動前就緒**（`analyzer.start` → results for-await 就緒訊號 → 才啟動 audio engine）。順序錯了會有機率性的「辨識器工作但結果無人接收」race——此坑已在 spike 除錯中修正，結構直接沿用。
3. **絕不可把未轉換格式餵給 analyzer**：Float32 餵給期待 Int16 的 analyzer 會「靜默中毒」（無錯誤、無輸出、finalize 卡死）。converter 必須依實際 buffer 格式惰性建立，轉換失敗寧可丟棄該塊。
4. `installedLocales` 與 `AssetInventory.status` 語意不同：判斷「能否轉寫」以後者 `== .installed` 為準。
5. 電話報案語境的諧音錯誤是常態：意圖分類必須靠 LLM 容錯，禁止字面完全匹配。
6. 麥克風實測必須在本機已登入桌面的終端執行（TCC 限制，ssh/tmux 下權限請求會 hang）；實測輸出一律 `>` 直寫檔案（tee 的檔案緩衝在 Ctrl-C 時會截斷/吞資料）。

### 工具

`spike/stt_spike.swift`（Swift CLI，swiftc 直編）：live 麥克風模式、`--wav` 檔案回餵（回歸測試用）、`--dump-audio` 錄下 analyzer 實際輸入、`--silence-ms`/`--flush-ms`/`--locale` 可調、JSONL 事件流輸出。用法見 `spike/README.md`。

### 同日並行進度

- 台詞庫草稿完成：`content/zh-TW/adult_script_draft.md`（88 句：canonical 18／variant 36／元台詞 16／FAQ 18），**待維護者逐句審定**——審定通過前不得合成語音。

## 2026-07-06 — 台詞庫定稿 ＋ TTS 全量入庫 ✅

### 已完成

1. **台詞審定完成**：維護者逐句審定 88 句 → 定稿 85 句（通過 79、修改 7、刪除 3）。關鍵臨床決策：S5 擺位不指示移動病人（四步：跪—掌根—交疊—打直）、趴臥情境退出台詞庫交現場講師、數數口令統一「一下兩下」型（與 spike 的 STT 偵測規則對齊）。
2. **定稿正本**：`content/zh-TW/adult_script.yaml`（機器可讀，id＝音檔檔名；FSM 與 TTS 以此為準）；審定表保留為審定紀錄。
3. **TTS 批次合成 85/85**：`tools/tts_batch.py`（Gemini TTS via Vertex AI、Charon、台灣腔派遣員 style prompt、斷點續跑、`--only` 單句補跑、locale 參數化）；音檔入庫 `assets/audio/zh-TW/`（24kHz mono）。金鑰走 `GOOGLE_APPLICATION_CREDENTIALS` 環境變數，位置見維護者私人筆記。
4. 維護者已試聽抽樣（開場／瀕死喘息判定／數數引導／肋骨安撫）與 v11 定稿句。

### 本階段坑（後續合成必讀）

- **雲端內容過濾器會誤擋急救台詞**：`s6_encourage_v11` 原句與派遣員 style prompt 組合被 Vertex AI 判 PROHIBITED_CONTENT（穩定復現，重試無效）。特徵：`resp.candidates` 為 None、失敗於 1–2 秒內（非配額／網路）。解法：同義改寫後對過濾器實測 2 次以上再入庫（本次定稿「保持相同速度往下壓，不要忽快忽慢。」2/2 過）。新增台詞時建議先跑 `--only` 單句驗證。
- Gemini TTS 生成有隨機性：個別句子聽感異常時刪 raw 重跑即可（斷點續跑會補）。
- **TTS 示範數數節奏過慢（55–75/min，未達壓胸標準）**：S6 三句含數數的音檔（`s6_encourage_v03`、`s6_start_c`、`s6_start_v01`）由 `tools/fix_counting_tempo.py` 後製——自動切點偵測、僅數數段 atempo 變速、節奏驗證至 110 下/分鐘（AHA 100–120 標準內），指令段維持原速。維護者已試聽核可。**重新批次合成後 final 會還原成未修正版，必須重跑一次本腳本**。`s6_start_v01` 台詞同步改為「一下、兩下、三下」型（維護者核定，統一數數風格）。

## 2026-07-06（同日稍晚）— FSM 引擎＋三 Provider 介面完成 ✅

### 已完成（SPEC 第九節第 3 項）

1. **FSM 對話引擎**（`server/engine/`）：S0–S7、locale/情境參數化、slot 跳步＋chain-advance、五層防禦全數實作（跳步／元台詞拼接／FAQ 18 意圖／層 4 受約束生成含 logs/layer4/ 存證待課後審核／兩級沉默 timeout）。核心設計：**引擎純同步決定性、零 I/O**，副作用以 SpeakAction 回傳由 driver 執行——完整可測、文字/語音模式共用同一顆引擎。
2. **三 Provider**（`server/providers/`）：STT＝subprocess 驅動 spike binary 消費 JSONL；LLM＝Vertex `gemini-2.5-flash-lite` constrained JSON 多 slot 意圖分類（實測 0.7–1.6s）＋ RegexFastPath（S6 數數/結束訊號不走 LLM）＋ keyword 降級；TTS＝預錄 id→wav（cache key 含 locale）＋ `say` 動態後備。
3. **Metrics 事件流**：monotonic、每筆附觸發原句、JSONL 可序列化；SPEC 第六節指標全對齊。
4. **文字模式 harness**（`server/cli_harness.py`）：stdin 當 STT、stdout 當 TTS，支援 /wait /fault 模擬指令；`--no-llm` 可離線跑。
5. **canonical 四角色制**：詢問句（slot 已填則抑制）／確認判定句（填滿才播）／always-voice／**條件句**（`CONDITIONAL_LINES` 宣告式表格，依 slot 值選句——S4 瀕死喘息探詢與判定句的臨床正確觸發：BREATHING=UNCLEAR→追問 probe、AGONAL→「這種喘」判定、ABSENT→「沒有在正常呼吸」判定）。
6. pytest **72 全綠**；驗收 transcript 於 `sessions/`（gitignored）。

### 引擎階段設計要點（下階段 UI 必讀）

- 對話入口：`server/runtime.py` 的 driver 編排（fastpath→LLM→keyword），UI/WS 層接上它而非直接碰 fsm。
- S0/S6/S7 不套沉默 timeout（壓胸沉默＝專心，不打斷）；S6 插播計時器 15–20s。
- 設定集中 `server/config.py`（`CPR_*` 環境變數可覆蓋）；LLM 認證共用 TTS 的 service account（GOOGLE_APPLICATION_CREDENTIALS）。
- 依賴已入 requirements.txt（fastapi/uvicorn/pytest 等，venv 快照已更新）。

### 待辦（下次 session 從這裡接手）

1. 課堂模式 UI 與資料模型（Class → StudentSession → Events）；WebSocket 接 runtime driver；真語音整測（麥克風＋喇叭，需維護者在 mini 本機配合）。
2. 報告輸出（Word／Excel／debriefing dashboard）。
3. 積欠項：README 文件地圖補 `docs/engine.md` 連結。
