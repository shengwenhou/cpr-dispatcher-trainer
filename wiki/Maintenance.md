# 維護手冊

> 給未來的維護者（多半是 AI coding agent 的新 session）與使用者本人。

## 環境

| 項目 | 內容 |
|---|---|
| 主力開發機 | Apple Silicon Mac（macOS 26），保持開機；主機名稱與網路細節見維護者私人筆記 |
| 遠端工作 | 筆電經 Tailscale 以 `ssh` + `tmux` 連入主力機；tailnet 內瀏覽器可直連 `http://<主機名>:<port>` 使用 app |
| 程式碼正本 | `~/Projects/cpr-dispatcher-trainer/`（GitHub：`shengwenhou/cpr-dispatcher-trainer`） |
| 本 wiki 工作目錄 | `~/Projects/cpr-dispatcher-trainer.wiki/`（獨立 git repo，push 至 `<repo>.wiki.git`，分支 `master`） |
| 人類文件區 | 維護者私有雲端資料夾（報告、進度紀錄），**不放程式碼** |

## Git 工作流

- 單一工作機模式：開發只在 mini 上發生，不存在同步衝突；GitHub 作為備份、歷史與保護。
- GitHub 認證：`gh` 已登入（帳號 `shengwenhou`）並以 `gh auth setup-git` 掛為 credential helper——此機器上 push/pull 自動認證。**筆電若要獨立 clone 工作，需在筆電上另跑一次 `gh auth login`。**
- Commit 訊息：繁體中文、說明「為什麼」；由 Claude 代寫時依慣例附 Co-Authored-By。
- 改壞了怎麼辦：每個 commit 都可整包回滾，找 Claude 說「回到上一個可用版本」即可。

## 金鑰與秘密（🚨 必守）

- GCP TTS service account 金鑰存於維護者本地私有路徑（詳見私人筆記），執行時以**環境變數／本地設定引用**，**絕不複製進 repo**。
- 主 repo `.gitignore` 已防呆（`secrets/`、`*-key.json`、`.env` 等模式一律排除）；新增任何秘密類檔案前先確認 ignore 規則涵蓋。

## 工具環境

- 依維護者全域規則：協助安裝／移除／升級任何工具後，跑一次環境快照腳本（位置見私人筆記）。
- 本專案將用到：Python venv（開發時建立，不入 repo）、`gh`（已裝）、未來 v2.0 需 `ollama`（尚未安裝）。
- TTS 批次合成重用維護者既有的 Gemini TTS 批次腳本模式與台灣腔發音校正庫（私有區）。

## 費用

| 項目 | 量級 |
|---|---|
| 意圖分類 API（Gemini Flash-Lite） | 每場 10 分鐘訓練 <NT$1；v2.0 地端化後歸零 |
| TTS 台詞庫合成 | 一次性批次成本（台詞改版才重跑）；沿用既有 GCP 專案 |
| STT / 預錄播放 | 零（on-device / 本機檔案） |

## 課堂前檢查清單（v1.0 上線後補充完整版）

1. mini 電源與網路（v2.0 全離線後免網路）
2. 麥克風權限與音量測試
3. `start.command` 啟動、瀏覽器開啟正常
4. 學員代號清單備妥
