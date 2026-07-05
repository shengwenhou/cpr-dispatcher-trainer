# STT Spike Helper（macOS 26 SpeechAnalyzer / SpeechTranscriber）

對應 `SPEC.md` 第九節第 1 優先 spike：驗證 macOS 26 on-device 語音轉寫（zh_TW）在
**壓胸階段連續大聲數數**情境下的斷句與辨識行為，並量測端點延遲。

這是 spike 工具，不是產品程式碼。目標只有一個：把 `SpeechAnalyzer` 管線接到能實測的最小
可行狀態，並把每一個轉寫事件（volatile / final / endpoint / status）吐到 stdout，讓實測
主持者能直接從輸出讀出結果。

---

## 一、環境需求

- macOS 26（本工具針對 macOS 26 的 `Speech.framework` 新 API：`SpeechAnalyzer`、
  `SpeechTranscriber`、`AnalyzerInput`、`AssetInventory`）。
- 僅需 **CommandLineTools**（含 MacOSX26 SDK），不需完整 Xcode.app。
- 麥克風可用（實測建議用實際課堂會用的收音裝置）。

---

## 二、編譯

```bash
cd spike
./build.sh
```

產出同目錄下的 `stt_spike` 執行檔（已被根目錄 `.gitignore` 排除，不入 repo）。

`build.sh` 用 `xcrun swiftc` 編譯。**CLT-only 環境的坑**：裸跑 `swiftc` 會報
`unable to load standard library for target 'arm64-apple-macosx26.0'`，因為它預設不會
解析到 MacOSX26 SDK；`xcrun` 包一層即可自動帶正確 SDK（`build.sh` 已處理，`xcrun`
不可用時會自動退回 `-sdk` 顯式指定）。

---

## 三、輸出格式

預設輸出 **JSONL**（stdout，一行一事件），方便程式解析；`--pretty` 改人類可讀。

**重要分流約定**：
- **事件資料**（`volatile` / `final` / `endpoint`）走 **stdout**。
- **診斷/狀態**（`status`：模型下載、權限、格式等）走 **stderr**。

因此正式實測抓資料時可這樣分流：

```bash
./stt_spike > events.jsonl 2> diag.log
```

事件欄位（JSONL）：

| type | 欄位 |
|---|---|
| `volatile` | `text`, `t_wall_ms`, `audio_start`, `audio_end` |
| `final` | `text`, `t_wall_ms`, `audio_start`, `audio_end`, `latency_since_last_volatile_ms`, `latency_since_audio_end_ms` |
| `endpoint` | `reason`（`vad_silence` 或 `natural`）, `t_wall_ms` |
| `status` | `msg`, `t_wall_ms` |

- `t_wall_ms`：自程式啟動起算的**單調毫秒**（monotonic，用於算延遲，單調遞增）。
- `audio_start` / `audio_end`：該段結果對應的**音訊時間軸秒數**（來自 SDK 的 `audioTimeRange`）。
- `latency_since_last_volatile_ms`：同一段話，最後一次 volatile 到 final 定稿的牆鐘延遲。
- `latency_since_audio_end_ms`：該段音訊講完（audio_end）到 final 產出的延遲；對照
  `SPEC.md` 第七節端點偵測預算 300–600ms。

---

## 四、參數

| 參數 | 說明 |
|---|---|
| （無）| 預設 live 模式：麥克風擷取 → 即時轉寫 → 輸出事件 |
| `--check` | 印 `supportedLocales` / `installedLocales`、指定 locale 資產狀態；未安裝則觸發下載到完成後退出 |
| `--wav <path>` | 從 WAV 檔餵同一條 analyzer 管線（繞過麥克風），做分離驗證/回歸測試 |
| `--locale <id>` | 轉寫語言（預設 `zh_TW`；SPEC 八之一 i18n 紀律要求參數化，勿寫死） |
| `--silence-ms <n>` | 自製 VAD 靜音門檻毫秒（預設 800）：靜音超過此門檻即產生一次語義斷句（`endpoint reason=vad_silence`） |
| `--flush-ms <n>` | 週期 finalize 間隔毫秒（預設 700）：**即使沒有靜音也定期 flush**，讓連續語音（壓胸數數）持續吐出結果 |
| `--no-vad` | 關閉自製 VAD 的靜音斷句（週期 flush 仍運作，只是少了語義端點） |
| `--dump-audio <path>` | live 模式下，把「實際餵進 analyzer 的音訊」同步存成 WAV（16kHz Int16 mono）；失敗時可用此檔以 `--wav` 回餵做離線復現 |
| `--pretty` | 人類可讀輸出（volatile 同行覆寫、final 換行標註延遲）；預設為 JSONL |
| `-h`, `--help` | 說明 |

### 除錯：`--dump-audio` 與消費進度診斷

當 live 模式「音訊有進、RMS 正常，卻零 result」時，用這兩個工具分離病灶：

- **`--dump-audio out.wav`**：把 analyzer 實際吃到的音訊落地。失敗當下錄一份，事後
  `./stt_spike --wav out.wav` 回餵即可離線復現，並可直接播放聽「analyzer 聽到了什麼」。
- **週期診斷的 `analyzer 消費(volatileRange)` 欄位**（走 stderr）：`yield` 是丟進
  AsyncStream 的**生產端**計數（緩衝 unbounded，永不阻塞，analyzer 沒消費也照漲）；
  `volatileRange` 是 analyzer **實際處理到的音訊範圍**（消費端代理指標）。判讀：
  - `yield/frames 漲、volatileRange 也跟著前進` → analyzer 有在消費（問題在別處）。
  - `yield/frames 漲、volatileRange 卡在 nil 或不動` → **音訊進了但 analyzer 沒消費**
    （生產/消費脫鉤，常見於輸入格式與 analyzer 期待不符而被靜默排斥）。

### 關鍵行為：為何需要 `--flush-ms`（務必理解）

`SpeechAnalyzer` 是**「呼叫 finalize 才產出結果」**的模型：把音訊 yield 進去後，即使開了
`.volatileResults`，在對它呼叫 `finalize(through:)` 或 `finalizeAndFinishThroughEndOfInput()`
**之前，不會吐出任何 volatile 或 final 事件**。

因此本工具用兩種 finalize 並存：
- **週期 finalize（`--flush-ms`）**：每隔固定時間對「目前音訊游標」finalize 一次，不中止串流。
  這是連續語音也能持續出結果的關鍵——**壓胸數數不停頓時，就靠這個切段**。
- **VAD 靜音 finalize（`--silence-ms`）**：偵測到靜音端點時 finalize，作為「語義斷句點」，
  額外標記一筆 `endpoint reason=vad_silence`。

若把 `--flush-ms` 設得很大而語音又完全不停頓，會變回「只有 SIGINT/結束時才吐字」的行為——
這正是早期版本 live 模式「餵了音訊卻 0 事件」的根因。

---

## 五、三個實測項目：執行指令與讀法

先跑一次 `--check` 確保 zh_TW 資產已安裝：

```bash
./stt_spike --check
```

（`installedLocales` 與 `AssetInventory.status` 語意不同：前者是 locale 層級註冊，後者才是
模型資產是否落地。以 `AssetInventory.status = installed` 為準。）

### 實測 1：短句正確率

固定講一組派遣情境短句（如「我在家裡」「他沒有反應」「沒有呼吸」「我已經開始壓了」），
每句講完停頓約 1 秒讓 VAD 斷句。

```bash
./stt_spike --pretty                        # 肉眼觀察
./stt_spike > s1.jsonl 2> s1.log            # 存檔逐句比對
```

**怎麼讀**：每個 `final` 事件的 `text` 即該句的辨識結果，與講稿逐句比對算正確率。

### 實測 2：連續數數斷句（本 spike 核心）

模擬壓胸節奏，連續大聲數「1、2、3、……、30」不停頓（100–120/min）。這是 SPEC
最擔心的情境：SpeechTranscriber 會把連續數數切成幾段？每段內容是什麼？

先看**自製 VAD 關閉**時的自然行為，再看**開啟 VAD**時能否用靜音門檻切出乾淨段落：

```bash
# A. 純觀察 SDK 自然 finalize（不主動斷句）
./stt_spike --no-vad > s2_natural.jsonl 2> s2_natural.log

# B. 自製 VAD 端點（可調門檻，連續數數幾乎無靜音 → 觀察 SDK 靠什麼斷）
./stt_spike --silence-ms 800 > s2_vad.jsonl 2> s2_vad.log
```

**怎麼讀**：
- 數 `final` 事件的**段數**，看每段 `text`（例如「1 2 3 4 5」或整段「一二三四五…」）。
- 用 `audio_start` / `audio_end` 看每段涵蓋的音訊區間，判斷有無漏數/併字/誤辨。
- 連續數數沒有靜音，自製 VAD（靠 RMS）通常不會觸發 `vad_silence` 端點；此時 `final`
  來自 SDK 自然 finalize，`endpoint reason=natural` 出現在串流結束時。這正是要觀察的：
  **SDK 在無靜音的連續語流下多久 finalize 一次、切點落在哪**。

### 實測 3：端點延遲

講一句話後**明確停頓**（讓 RMS 掉到靜音門檻以下），觀察從「講完」到「斷句 + final」的延遲。

```bash
./stt_spike --silence-ms 800 --pretty       # pretty 模式直接看到兩項延遲標註
./stt_spike --silence-ms 800 > s3.jsonl 2> s3.log
```

**怎麼讀**：
- `endpoint reason=vad_silence` 的 `t_wall_ms` = VAD 判定靜音達門檻的時刻。
- 對應 `final` 的 `latency_since_audio_end_ms` = 音訊講完到 final 產出的延遲，直接對照
  `SPEC.md` 第七節端點預算 **300–600ms**。
- 調 `--silence-ms`（如 500 / 800 / 1200）觀察門檻對「反應快慢 vs 誤斷」的取捨。

---

## 六、已知環境限制（實測前必讀）

- **麥克風權限（TCC）**：本工具是無 app bundle 的 CLI，TCC 授權掛在**父進程**（啟動它的
  終端機 app），不是 binary 本身。
- **必須在有登入 GUI 的 session 執行**：經實測，在純 ssh／headless session 下，
  `AVCaptureDevice.requestAccess` 會**無限 hang**（既不彈窗也不回傳）。本工具已加 8 秒
  逾時保護：逾時會印完整診斷並以結束碼 `2` 乾淨退出，不會卡死。
- 因此正式實測請在**本機、已登入桌面**的終端機直接跑（不要透過遠端 ssh session），
  第一次執行時允許系統彈出的麥克風授權；若曾拒絕，到
  「系統設定 → 隱私權與安全性 → 麥克風」手動勾選該終端機後重啟終端機再跑。
