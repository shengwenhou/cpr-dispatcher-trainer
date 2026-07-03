# CLAUDE.md — 開發指引

## 專案一句話

AI 扮演 119 派遣員的 CPR 課堂訓練工具：繁中語音對話、FSM 對話引擎、全預錄台詞、時間指標報告。

## 每個 session 先讀

1. `LOCAL_NOTES.md`（私有路徑與機器資訊；**不在 repo 內**，只存在維護者本機——若不存在表示在陌生機器，先向使用者確認）
2. `SPEC.md` — 規格正本，所有開發依據，與其他文件矛盾時以它為準
3. `PROGRESS.md` — 進度交接，從「待辦」接手

## 必守規則

- **本 repo 是 public**：第三方 In-House 教材（派遣 protocol、QI 表單）、私有絕對路徑、機器名稱、內網 IP、金鑰，**一律不得進入 repo**（含 commit 訊息與歷史）。私有資源一律透過 `LOCAL_NOTES.md` 指路。
- **臨床內容關卡**：所有派遣員台詞、FAQ 答句、醫療相關文案，必須經維護者（急診醫師）逐句審定才可定稿與合成語音。
- **i18n 紀律（SPEC 八之一）**：使用者可見字串一律外部化，禁止 hardcode；FSM 以 locale 與情境為參數；cache key 含 locale。
- Commit 訊息用繁體中文、說明「為什麼」。
- 與使用者的溝通、程式註解一律繁體中文；使用者為急診醫師（醫學內容以專業同儕水準溝通），**不具程式能力**（技術操作由 Claude 代為執行並以清楚步驟說明）。
- Wiki 編輯正本在 `../cpr-dispatcher-trainer.wiki/`（獨立 git repo，push 至 `.wiki.git`，分支 `master`）；規格變更先改 SPEC，再回頭更新 wiki 摘要。

## 協作分工

主 agent 負責規劃、拆解、整合；深度推理（架構、複雜除錯、演算法）委派 `deep-reasoner`；機械性工作（boilerplate、測試、格式化）委派 `fast-worker`；需要不同視角時用 codex（把它當 peer 而非 reviewer）。高風險決策：同一問題平行交給 deep-reasoner 與 codex（互不見對方答案），由主 agent 整合。

## 目前階段（2026-07-03 起）

規劃完成、開發前。依 SPEC 第九節優先序：Phase 0 spike（壓胸數數 STT 實測）→ 台詞庫 → FSM 引擎 → 課堂 UI → 報告。
