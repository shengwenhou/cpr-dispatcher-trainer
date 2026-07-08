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

### 2026-07-07 補記 — 維護者實測反饋修正（第一輪課堂改善循環）

維護者文字模式試玩後反饋「S5 跪好了系統不會往下、鬼打牆重複第一步」，修正三項（pytest 99 全綠，+27）：

1. **S5 改逐步引導模式**：sub-step 進度（跪→掌根→交疊→打直一次播一步）；三路推進並存——口頭確認（「好了／再來呢」）立即下一步、**沉默 4 秒自動下一步**（`CPR_S5_AUTOADVANCE_S` 可調；S5 不再問「你還在嗎」）、「都擺好了」跳步進 S6；重問／FAQ 回接錨定**當前步**；每步推進方式（confirmed/auto/skipped）記入 metrics（debriefing 可看全班哪步卡最久）。counting 偵測擴及 S5（邊擺位邊數數即起壓）。
2. **STEP_DONE 意圖**（「再來呢／然後呢／下一步」課堂高頻句）：S5=下一步、S6=回 encourage、S1–S4=重播當前問句；超短確認詞走 fastpath 不花 LLM 往返。
3. **層 4 雙保險**：生成 prompt 加狀態語境與禁令＋生成後驗證層（超前流程動作詞→丟棄降級層 2）——修正試玩抓到的「擺位階段生成『繼續壓胸』」超前指示。

此輪即 SPEC 設計的改善循環首次運轉：課堂使用→層 4 存證→審核→意圖轉正。

### 待辦（下次 session 從這裡接手）

1. 課堂模式 UI 與資料模型（Class → StudentSession → Events）；WebSocket 接 runtime driver；真語音整測（麥克風＋喇叭，需維護者在 mini 本機配合）。→ **UI／資料模型／WS 已於 2026-07-07 完成（見下節）**
2. 報告輸出（Word／Excel／debriefing dashboard）。
3. 積欠項：README 文件地圖補 `docs/engine.md` 連結；`docs/engine.md` 補 S5 逐步模式與 STEP_DONE 章節（fast-worker 未完成）。→ 已於 commit 2e77330／9e0ff5f 補齊。

## 2026-07-07 — 課堂模式 UI＋資料模型＋WS 整合層完成 ✅（SPEC 第九節第 4 項）

### 已完成

1. **架構定案**（deep-reasoner 與 codex 平行獨立設計後整合，兩案在關鍵處收斂）：
   - 持久化＝**JSONL＋manifest**（否決 SQLite）：`data/<class_id>/` 下 `class.json`（含 SessionRef 索引）＋每場 `<session_id>.jsonl`（事件流逐筆 append）＋`.meta.json`（狀態與 summary 快照）。理由：單機單麥克風零寫入併發、引擎 metrics 原生 JSONL、append-only 崩潰損失最小、純文字對維護者可讀。
   - **VoiceDriver＝STT 整場不重啟＋driver 層 half-duplex gate**：非 S6 發聲窗（afplay 進程存活＋echo tail）內 FINAL 硬丟棄＋grace 窗內文字相似度雙保險（門檻 0.75）；S6 軟 gate 只認 counting＋end_signal（首次數數起壓時間戳不丟），其餘丟棄記 GATE_DROPPED 事件（debriefing 素材）。
   - **雙時鐘**：metrics 真 monotonic（SPEC 第六節指標不失真）；引擎 now 餵「邏輯時鐘」（發聲窗凍結、以 session 起點為 0）→ 播放不算沉默、S6 插播不疊播、S5 auto-advance 播完才續走。engine／providers／runtime.py／metrics.py 零改動。
   - 場次狀態存 server 不綁 WS：誤刷新以 session_id `resume`，snapshot 重建（含學員代號）。
2. **新增模組**：`server/ws_protocol.py`（envelope＋MsgType；error 用 message_key 走 i18n）、`server/session_store.py`、`server/audio_player.py`（async afplay/say、可 kill 供緊急中止）、`server/session_runner.py`（Runner 基底＋Text/Voice 子類）；`app.py` 接真 handler（WS 路徑 `/ws/classroom`）＋web/ 靜態 mount；config／factory 純新增。
3. **前端 `web/`**（codex 產出後對齊修正）：vanilla 單頁無 build step、離線可用；開始畫面（情境六項僅成人可選、語言選單僅繁中、學員代號、語音／文字模式）、練習畫面（S0–S7 步驟條、對話流、派遣員說話中指示、緊急中止二次確認、通話計時）、場次結束個人指標卡（AHA 達標紅綠標示）、結束課堂占位；UI 字串全外部化 `web/i18n/zh-TW.json`（key 小寫 s0–s7 與後端 state 值一致）。
4. **start.command**：雙擊啟動、自動開瀏覽器；啟動時載入 `~/.config/cpr-dispatcher-trainer/env.sh`（repo 外本機 env，GOOGLE_APPLICATION_CREDENTIALS 放這裡，已在開發機建好）；`CPR_BIND_HOST=0.0.0.0` 可供區網連入。
5. **端到端驗證（文字模式）**：真 uvicorn＋真瀏覽器完整三場 S0→S7（含 S6 插播鼓勵語、層 5 兩級沉默 reprompt、緊急流程、誤刷新 resume 續走完場）；落地資料與指標全數正確。pytest **116 → 134 全綠**。

### 本階段坑（後續必讀）

- **虛擬時鐘測試抓不到的真時鐘 bug**：`engine.start()` 的 S0 進入基準（metrics.now()，session 起點≈0）與 runner 邏輯時鐘若用 monotonic 絕對值為底會基準不一致，S0 dwell 爆成開機時長量級。已修（邏輯時鐘統一以 metrics.now() 為底）＋新增大絕對值時鐘回歸測試。教訓：**時間相關功能必須有非零起點時鐘的測試**。
- **平行實作的接縫錯位**：WS 路徑（後端沿用骨架 `/ws/session` vs 前端 `/ws/classroom`）、summary 欄位名（前端 spec 臆測 vs `metrics.summary()` 正本）兩處在端到端才抓到。教訓：跨 worker 對接的欄位名一律以現有程式碼為正本寫進委派 prompt，不憑記憶轉述。
- 真語音（VoiceSessionRunner）的 gate 三態／雙時鐘已以假 STT＋假 player 單元驗證，但 **echo_tail_ms 與相似度門檻需真聲學調校**——這是下一步真語音實測的重點。

### 待辦（下次 session 從這裡接手）

1. **真語音整測**（需維護者在 mini 本機配合；指引已交付於維護者私有文件區《真語音實測指引》）：DJI Mic 2＋喇叭、全語音 S0→S7、延遲對照 SPEC 第七節（預錄命中 500–800ms）、echo gate 參數調校（CPR_ECHO_TAIL_MS 等）、層 4 存證審核。
2. 報告輸出（Word／Excel／debriefing dashboard）——資料層已存夠（events.jsonl＋summary），純消費端工作。

### 2026-07-08 補記 — 首輪真語音實測回報修復

維護者首測（DJI）：講話無反應、中止／結束按鈕「沒反應」。查明與修復（commit 0caee35）：

1. **中止／結束其實後端有執行**（meta 已標 aborted），但 `_finalize` 不推 WS，前端不知情。已統一收口：推 `session_aborted`（回開始畫面）／`session_ended`（指標卡）＋存證事件；手動結束語意改標 `ended`。
2. **STT helper 兩種死法**：(a) tap 以啟動前查詢的 hwFormat 宣告，route 不符即 crash——改 `installTap(format: nil)`，`--wav` 回歸通過；(b) 系統**無輸入裝置**時 engine.start 以 -10868 失敗（Mac mini 無內建麥，DJI 未被認到＝零聲源），helper 正確走層 5 技術故障。首測「活著零輸出」最可能是 DJI 掛著但未進音。
3. **診斷盲區已補**：STT helper stderr 全量落地事件流（`stt_status`：授權、格式、每 2 秒音訊塊數與 RMS 峰值）——下輪實測若再異常，事件流直接可判。
4. 待辦不變：真語音全流程 S0→S7（實測前**必查系統聲音輸入選 DJI 且音量條有動**，指引已更新）。

### 2026-07-08 補記二 — STT live 堵塞根因與修法（第二、三輪實測循環）

實測「講很多只辨識一筆、延遲 26 秒」確診為兩問題（詳 commit）：(1) 週期 finalize metronome 硬切致碎片化＋丟內容——改 VAD 為主＋2s 安全網（`CPR_STT_FLUSH_MS` 預設 2000，數數偵測最壞延遲＝此值，S6 若需更快可調小）；(2) live 管線 results 消費者餓死（file-feed 不可複現）——高優先級緩解＋「results 交付」診斷儀器，待真麥克風驗收。**排錯方法論沉澱：spike 新增 `--wav-realtime`（實測 dump 音訊以真實速率回放）——live 時序 bug 從此可離線複現迭代，不需佔用維護者麥克風場。**同輪亦修：緊急中止撕斷播放鏈致「說話中」指示與邏輯時鐘永久掛起（_speak 改 finally 保證收尾＋播放 30s 上限）。
