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
