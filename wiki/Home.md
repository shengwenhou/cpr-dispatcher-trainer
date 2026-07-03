# CPR Dispatcher Trainer Wiki

歡迎。本專案是一個 **AI 扮演 119 消防局派遣員**的 CPR 課堂訓練工具：學員以繁體中文語音模擬報案，AI 依 T-CPR protocol 引導至持續壓胸，結束後產出時間指標與課堂 debriefing 報告。

## 頁面導覽

| 頁面 | 內容 |
|---|---|
| [[Introduction]] | 專案介紹：動機、對象、核心架構與關鍵設計決策 |
| [[Maintenance]] | 維護手冊：環境、工作流、金鑰安全、費用、備份 |
| [[Roadmap]] | 版本路線圖：v1.0 → v2.5 與最終目標 |
| [[Plan]] | 開發計畫：階段任務分解與完成定義 |
| [[Tech-Debt]] | 技術債登記簿：已知取捨、風險與償還時點 |

## 文件分工（避免雙源頭）

- **規格正本**：主 repo 的 [`SPEC.md`](https://github.com/shengwenhou/cpr-dispatcher-trainer/blob/main/SPEC.md)——所有開發依據，與其他文件矛盾時以它為準。
- **進度交接**：主 repo 的 [`PROGRESS.md`](https://github.com/shengwenhou/cpr-dispatcher-trainer/blob/main/PROGRESS.md)。
- **本 wiki**：給「人」看的活文件——背景說明、維護知識、計畫與債務追蹤。**規格細節不在此複製全文**，改動規格請先改 SPEC 再回頭更新 wiki 摘要。

## 快速事實

- 開發模式：所有程式碼由 AI coding agent（Claude Code）代寫與維護；使用者為急診醫師（課程講師），負責臨床內容審定。
- 平台：本機網頁 app（Python FastAPI + 瀏覽器），跑在講師的 Mac 上。
- 目前狀態：**規劃完成、開發前**（2026-07-03）。
