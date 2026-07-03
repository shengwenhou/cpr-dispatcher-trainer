# 開發計畫（v1.0）

> 任務分解與完成定義。執行順序依風險優先：先驗證最不確定的，再全力開發。

## Phase 0 — Spike：語音辨識實測（最優先）

**目的**：驗證全案最大技術風險——macOS 26 SpeechAnalyzer/SpeechTranscriber(zh_TW) 在課堂情境的表現。

| 測試 | 完成定義 |
|---|---|
| 一般對話句辨識 | 台灣口語短句（「要救護車」「他沒有呼吸」）辨識正確率可接受 |
| **壓胸數數階段** | 連續大聲數數（1-2-3…）時：能穩定偵測「正在數數」；斷句行為可控（不會瘋狂觸發 turn 結束） |
| 端點偵測手感 | 靜音門檻可調，講完 →判定完成 <600ms |
| Swift helper 雛形 | 麥克風 → transcript + 端點事件 → local WebSocket 吐給 Python，跑通即可 |

**不通過的備案**：切 whisper.cpp large-v3-turbo + Silero VAD（介面不變，只換 Provider）。

## Phase 1 — 台詞庫

1. Claude 依 protocol（`docs/reference/`）起草成人情境全套台詞：canonical 必講句 + 每狀態 2–4 個變體 + 元台詞（釐清／承接拉回／安撫接管）+ FAQ 初稿（10–20 條，含 AED、翻身、肋骨、嘔吐）。
2. **使用者逐句審定**（臨床內容關卡）。
3. 以既有 Gemini TTS pipeline（Charon + 台灣腔 style prompt）批次合成 → 音檔入庫 → 使用者抽聽驗收。

已定稿句：開場「你好，這裡是天才消防局，請問你要消防車還是救護車？」；地址確認「好的，地址我記下了，預估5分鐘之後會抵達。」

## Phase 2 — 核心引擎

- FSM（S0–S7，情境為參數）+ 多 slot 跳步 + 低信心不前進規則
- 三個 Provider 介面（STT／LLM／TTS）+ 起步實作（AppleSTT／GeminiFlashLite／CachedAudio）
- 意圖分類 prompt + JSON schema（constrained decoding）
- 五層防禦邏輯（含第 4 層受約束生成與記錄回流）

**完成定義**：純文字模式（打字代替語音）可完整走完一場 S0→S7，時間戳正確。

## Phase 3 — 課堂 UI 與資料模型

- 開始畫面：情境選單（六項、僅成人可選）+ 學員代號輸入
- 練習畫面:即時逐字稿、狀態指示、緊急中止
- 資料模型：Class → StudentSession → Events（SQLite）
- `start.command` 一鍵啟動

**完成定義**：語音端到端一場完整練習，預錄命中路徑回應 ≤800ms。

## Phase 4 — 報告

- 每場：個人指標畫面 + Word 匯出
- 「結束課堂」：全班 Excel + debriefing dashboard（指標分布、達標狀況、fallback 統計）

**完成定義**：模擬 6 名學員的課堂，產出三種報告皆正確可讀。

## Phase 5 — 整合驗收

- 模擬完整課堂（含故意離題、跳步、沉默、問 FAQ 的學員）
- 課堂前檢查清單定稿（寫入 [[Maintenance]]）
- 使用者實際試用 → 回饋修正 → v1.0 發布（git tag）

## 里程碑依賴

```
Phase 0 (spike) ─┐
                 ├─→ Phase 2 → Phase 3 → Phase 4 → Phase 5
Phase 1 (台詞)  ─┘      （Phase 1 審定可與 Phase 2 開發平行）
```
