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

### 待辦（下次 session 從這裡接手）

1. 台詞庫審定（維護者進行中）→ 審定後轉 YAML＋Gemini TTS 批次合成。
2. FSM 引擎骨架 + 三 Provider 介面（STT 模組直接移植 spike 的管線結構與六項工程發現）。
3. 課堂模式 UI 與資料模型。
