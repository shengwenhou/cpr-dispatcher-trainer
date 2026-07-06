"""CPR 派遣員訓練工具 — 後端引擎套件。

模組結構：
- config：集中設定（Provider 選擇、模型 id、helper 路徑、locale 預設）。
- engine：FSM 引擎（fsm/intents/fastpath/script_store/metrics/actions），純同步可測。
- providers：三 Provider 抽象與實作（STT／LLM／TTS），與引擎分離。
- runtime：把 Provider 接到引擎的 driver（文字模式與語音模式共用）。
- app：最小 FastAPI 骨架（health＋WebSocket 預留）。
"""
