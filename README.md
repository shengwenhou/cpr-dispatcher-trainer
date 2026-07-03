# CPR Dispatcher Trainer — 119 派遣員互動訓練工具

AI 扮演 119 消防局派遣員，與 CPR 課程學員進行**繁體中文語音對話**，模擬 T-CPR（telecommunicator-assisted CPR）報案指導流程，結束後產出時間指標（time metrics）與課堂 debriefing 報告。

## 專案狀態

**規劃完成，開發前。** 完整規格見 [SPEC.md](SPEC.md)，進度見 [PROGRESS.md](PROGRESS.md)。

## 架構一句話

本機網頁 app（Python FastAPI + 瀏覽器）＋ 有限狀態機對話引擎（LLM 僅做意圖理解）＋ 全預錄台詞語音（Gemini TTS 批次生成）＋ on-device 語音辨識（macOS 26, zh_TW）。

## 文件地圖

| 檔案 | 內容 |
|---|---|
| [SPEC.md](SPEC.md) | 開發規格正本（定案決策、FSM、台詞規則、metrics、路線圖） |
| [PROGRESS.md](PROGRESS.md) | 進度與交接記錄 |
| [docs/reference/](docs/reference/) | 公開 T-CPR 教材（LifeLinks、RA Toolkit；另兩份第三方內部文件不隨 repo 散布，見該目錄 README） |
| [Wiki](https://github.com/shengwenhou/cpr-dispatcher-trainer/wiki) | 專案 wiki（介紹、維護、路線圖、計畫、技術債） |

（完整架構分析報告為規劃階段的內部文件，存於維護者私有文件區；其結論摘要見 [Wiki 的 Introduction](https://github.com/shengwenhou/cpr-dispatcher-trainer/wiki/Introduction) 與 SPEC。）

## 授權 License

本專案**原創內容**（程式碼、規格文件、wiki、台詞庫）以 [MIT License](LICENSE) 授權。

**第三方內容例外**：`docs/reference/` 內之 T-CPR 教材（CPR LifeLinks Toolkit、Resuscitation Academy T-CPR Toolkit 等）為第三方著作，**不在 MIT 授權範圍內**，版權歸原權利人所有，於本專案僅作開發設計參考。

## 免責聲明

本工具為 **CPR 課堂教學輔助軟體**，僅供訓練情境使用：

- 不能取代正式 CPR 認證課程、合格指導員或真實的 119 報案。
- 模擬對話內容依 T-CPR 教材設計，但不構成醫療建議；真實緊急情況請立即撥打 119。
- 訓練產出之時間指標僅供教學回饋，非臨床或研究級量測。

## 工作環境備忘

- 程式碼正本在本 repo，以 GitHub 同步與保護；**不放雲端同步資料夾**（避免半成品即時同步與衝突副本；完整論證存於維護者私有文件區之架構分析）。
- 開發相關的人類文件（報告、進度紀錄）存於維護者私有文件區。
- 主要開發機：Apple Silicon Mac（macOS 26）；遠端以 ssh + tailscale + tmux 連入工作。
- GCP TTS 金鑰以本地私有路徑／環境變數提供，**絕不進 repo**。
