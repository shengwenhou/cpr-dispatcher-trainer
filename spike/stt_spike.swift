// stt_spike.swift
//
// CPR 派遣員訓練工具 —— macOS 26 SpeechAnalyzer / SpeechTranscriber STT spike helper
//
// 用途：驗證 macOS 26 on-device 語音轉寫（zh_TW）在「壓胸階段連續大聲數數」情境下的
//       斷句與辨識行為，並量測端點延遲。對應 SPEC 第九節第 1 優先 spike。
//
// 這是 spike 工具，不是產品程式碼；目標是把 SpeechAnalyzer 管線接到能實測的最小狀態，
// 並把每一個轉寫事件（volatile / final / endpoint / status）以 JSONL 或人類可讀格式吐到 stdout，
// 讓主持實測的人能從輸出直接讀出三個實測項目的結果。
//
// 編譯：見同目錄 build.sh（swiftc 直編，不用 SwiftPM）
// 依賴：Speech.framework、AVFoundation（由 import 自動連結）
//
// 註：一切 API 簽名以 CommandLineTools SDK 的 Speech.swiftinterface 為準，本檔已據實對齊。

import Foundation
import AVFoundation
import Speech
import CoreMedia

// MARK: - 單調時鐘（延遲量測用）

/// 回傳自程式啟動起算的單調毫秒數（不受系統時間調整影響）。
/// 所有事件的 t_wall_ms 與各項延遲都以此為基準，確保可計算且單調遞增。
///
/// 注意：start 用 lazy static 且以 `&-` 減法時，若在運算式內「先取 now、後觸發 start 初始化」，
/// 第一次會得到 now < start 而回繞成天文數字。因此改為：先讀 start（保證已初始化），
/// 再取 now，並在啟動時呼叫 bootstrap() 主動初始化。
enum MonoClock {
    private static let start: UInt64 = DispatchTime.now().uptimeNanoseconds
    /// 程式啟動時主動呼叫一次，確保 start 在任何 nowMs() 之前完成初始化
    static func bootstrap() { _ = start }
    static func nowMs() -> Double {
        let s = start                                   // 先確保 start 已就緒
        let now = DispatchTime.now().uptimeNanoseconds  // 再取當下
        let elapsed = now >= s ? (now - s) : 0          // 保底：絕不回繞成負值
        return Double(elapsed) / 1_000_000.0
    }
}

// MARK: - 命令列參數

/// 解析後的執行選項。locale 一律參數化（SPEC 八之一 i18n 紀律：禁止 hardcode）。
struct Options {
    var mode: Mode = .live
    var localeID: String = "zh_TW"
    var silenceMs: Double = 800
    var useVAD: Bool = true
    var pretty: Bool = false
    var wavPath: String? = nil   // --wav / --wav-realtime：從 WAV 檔餵同一條 analyzer 管線（繞過麥克風）
    var flushMs: Double = 2000   // 週期 finalize「安全網」間隔毫秒（見 runFlushSafetyNet）：
                                 // 距上次 finalize 達此值且有新音訊才補刀。VAD 靜音斷句為主，
                                 // 此值只界定「連續語音（如壓胸數數）無自然斷點時的最壞延遲上限」。
                                 // 從舊預設 700ms（無條件 metronome，會把對話句切成碎片）上調。
    var dumpAudioPath: String? = nil // --dump-audio：把實際餵進 analyzer 的音訊同步存 WAV

    // ---- --wav-realtime 專屬（無麥克風複現 live 時序行為用）----
    var chunkMs: Double = 100         // 每塊音訊毫秒（realtime 餵入粒度）
    var rtSpeed: Double = 1.0         // 餵入速率倍率（1.0=真實時；>1 加速餵、<1 放慢）
    var rtSleep: Bool = true          // 塊間是否 sleep（--no-rt-sleep 可測「快餵＋週期 finalize」）
    var tailWaitS: Double = 10.0      // 餵完後保持 analyzer 開啟秒數（模擬 live 持續狀態）
    var tailSilence: Bool = true      // 尾段是否持續餵靜音（模擬 mic 不停送靜音、游標續進）
    var useBufferStartTime = false    // AnalyzerInput 是否帶 bufferStartTime（時間軸歧義假說測試）
    var useFastResults = false        // reportingOptions 是否加入 .fastResults（延遲交付假說測試）
    var deliveryDiag = false          // 每筆 result 附交付診斷（wall-clock / range / finalizationTime）到 stderr
    var synthFinal = true             // 「volatile 合成 final」fallback（放棄依賴 SDK 的 final 交付）：
                                      // live 實測 isFinal 全不交付、volatile 正常，故預設開啟；
                                      // --no-synth-final 可關閉做 A/B（file-feed 下真 final 正常交付時比對）
    var dropRealFinals = false        // 除錯：丟棄所有真 isFinal（模擬 live「final 不交付」），
                                      // 讓 file-feed 也能驗證「純靠合成」的段落覆蓋與去重（--drop-real-finals）

    enum Mode {
        case check       // --check：印資產與 locale 狀態，必要時觸發下載
        case live        // 預設：麥克風即時轉寫
        case wav         // --wav：從檔案全速餵，分離驗證 / 回歸測試
        case wavRealtime // --wav-realtime：從檔案以真實速率餵 + 週期 finalize（複現 live 時序）
    }

    static func parse(_ args: [String]) -> Options {
        var o = Options()
        var i = 0
        let a = Array(args.dropFirst()) // 去掉執行檔本身
        while i < a.count {
            switch a[i] {
            case "--check":
                o.mode = .check
            case "--wav":
                i += 1
                if i < a.count { o.wavPath = a[i]; o.mode = .wav }
            case "--wav-realtime":
                i += 1
                if i < a.count {
                    o.wavPath = a[i]
                    o.mode = .wavRealtime
                    o.deliveryDiag = true // 此模式預設開啟逐筆交付診斷（走 stderr）
                }
            case "--chunk-ms":
                i += 1
                if i < a.count, let v = Double(a[i]) { o.chunkMs = v }
            case "--rt-speed":
                i += 1
                if i < a.count, let v = Double(a[i]), v > 0 { o.rtSpeed = v }
            case "--no-rt-sleep":
                o.rtSleep = false
            case "--tail-wait-s":
                i += 1
                if i < a.count, let v = Double(a[i]) { o.tailWaitS = v }
            case "--no-tail-silence":
                o.tailSilence = false
            case "--buffer-start-time":
                o.useBufferStartTime = true
            case "--fast-results":
                o.useFastResults = true
            case "--locale":
                i += 1
                if i < a.count { o.localeID = a[i] }
            case "--silence-ms":
                i += 1
                if i < a.count, let v = Double(a[i]) { o.silenceMs = v }
            case "--flush-ms":
                i += 1
                if i < a.count, let v = Double(a[i]) { o.flushMs = v }
            case "--dump-audio":
                i += 1
                if i < a.count { o.dumpAudioPath = a[i] }
            case "--no-vad":
                o.useVAD = false
            case "--no-synth-final":
                o.synthFinal = false
            case "--drop-real-finals":
                o.dropRealFinals = true
            case "--pretty":
                o.pretty = true
            case "-h", "--help":
                printUsage()
                exit(0)
            default:
                FileHandle.standardError.write("未知參數：\(a[i])\n".data(using: .utf8)!)
            }
            i += 1
        }
        return o
    }
}

func printUsage() {
    let usage = """
    用法：stt_spike [選項]

    模式：
      （預設）           live 模式：麥克風擷取 → 即時轉寫 → stdout 輸出事件
      --check            印出 SpeechTranscriber 的 supportedLocales / installedLocales、
                         指定 locale 資產狀態；若未安裝則觸發下載到完成後退出
      --wav <path>       從 WAV 檔全速餵同一條 analyzer 管線（繞過麥克風），做分離驗證/回歸測試
      --wav-realtime <p> 從 WAV 檔以「真實速率」餵 + 週期 finalize + VAD + 診斷（複現 live 時序，無麥克風）

    --wav-realtime 專屬選項（用於隔離變因的對照實驗）：
      --chunk-ms <n>     每塊音訊毫秒（預設 100）
      --rt-speed <x>     餵入速率倍率（預設 1.0=真實時；>1 加速、<1 放慢）
      --no-rt-sleep      關掉塊間 sleep（測「快餵＋週期 finalize」）
      --flush-ms 0       停用週期 finalize（只靠收尾 finalize；隔離「週期 finalize」變因）
      --tail-wait-s <n>  餵完後保持 analyzer 開啟秒數（預設 10，模擬 live 持續狀態）
      --no-tail-silence  尾段不餵靜音（預設會餵靜音讓游標續進，忠實對應 mic 不停）
      --buffer-start-time  AnalyzerInput 帶明確 bufferStartTime（測時間軸歧義假說）
      --fast-results     reportingOptions 加入 .fastResults（測 final 交付延遲假說）
      --drop-real-finals 除錯：丟棄所有真 isFinal（模擬 live「final 不交付」），
                         讓 file-feed 也能驗證「純靠 volatile 合成」的段落覆蓋與去重

    選項：
      --locale <id>      轉寫語言（預設 zh_TW；SPEC i18n 紀律要求參數化）
      --silence-ms <n>   自製 VAD 靜音門檻毫秒（預設 800）：靜音超過門檻即語義斷句
      --flush-ms <n>     週期 finalize「安全網」間隔毫秒（預設 2000）：距上次 finalize
                         達此值且有新音訊才補刀。VAD 靜音斷句為主，此值只界定連續語音
                         （壓胸數數）無自然斷點時的最壞延遲上限。設 0 則純靠 VAD＋收尾 finalize
      --no-vad           關閉自製 VAD 的靜音斷句（週期 flush 仍運作，只是少了語義端點）
      --no-synth-final   關閉「volatile 合成 final」fallback（預設開啟）。live 實測 SDK 的 isFinal
                         完全不交付、volatile 正常，故 helper 端在 VAD 端點／安全網逾時時以最後
                         volatile 合成 final（含 "synthesized":true），並對隨後到達的真 final 去重
      --dump-audio <path> live 模式下，把「實際餵進 analyzer 的音訊」同步存成 WAV
                         （16kHz Int16 mono）。失敗時可用此檔以 --wav 回餵離線復現
      --pretty           人類可讀輸出（預設 JSONL，一行一事件）
      -h, --help         顯示本說明

    產生 zh_TW 測試音檔（供 --wav）：
      say -v Meijia --data-format=LEF32@16000 -o /tmp/test_zh.wav "我要救護車，他沒有呼吸"

    三個實測項目對應：
      1. 短句正確率      → 讀 final 事件的 text 欄位，與講稿逐句比對
      2. 連續數數斷句    → 連續數 1 到 30，觀察 final 事件切成幾段、每段 text 內容
      3. 端點延遲        → 讀 final 事件的 latency_since_audio_end_ms（VAD 斷句）
                           與 latency_since_last_volatile_ms（volatile→final 收斂）
    """
    print(usage)
}

// MARK: - 事件輸出（JSONL / pretty）

/// 事件輸出器：統一負責 JSONL 與 pretty 兩種格式，避免格式邏輯散落各處。
/// pretty 模式下 volatile 以 \r 同行覆寫，final 換行並標註兩項延遲，方便肉眼觀察。
final class EventEmitter {
    let pretty: Bool
    private var lastWasVolatile = false
    // 輸出鎖：合成 final 修法後，final 可能同時來自「results 消費 task」「安全網 task」「tap 執行緒
    // 的 VAD 端點」三個上下文；此鎖確保每筆 JSONL/pretty 行的寫出不被其他執行緒穿插而破行。
    private let ioLock = NSLock()

    init(pretty: Bool) { self.pretty = pretty }

    // 以毫秒（保留整數）輸出，避免 JSON 出現過長浮點尾數
    private func ms(_ v: Double) -> Int { Int(v.rounded()) }

    private func jsonLine(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
              let s = String(data: data, encoding: .utf8) else { return }
        ioLock.lock(); defer { ioLock.unlock() }
        print(s)
    }

    func status(_ msg: String) {
        // status 一律走 stderr 並即時輸出：
        //   1. 診斷訊息本就屬於 stderr，與 stdout 的事件流分離，方便 `2>診斷.log` 分流。
        //   2. stdout 在非 tty（背景/重導向）時預設全緩衝，若程式卡在某個 async 呼叫，
        //      緩衝內的 status 會看不到；stderr 行緩衝可即時看到，對排錯（尤其 hang）至關重要。
        if pretty {
            ioLock.lock(); defer { ioLock.unlock() }
            clearVolatileLineIfNeeded()
            FileHandle.standardError.write("[狀態] \(msg)\n".data(using: .utf8)!)
        } else {
            if let data = try? JSONSerialization.data(
                withJSONObject: ["type": "status", "msg": msg, "t_wall_ms": ms(MonoClock.nowMs())] as [String: Any],
                options: [.sortedKeys]),
               var s = String(data: data, encoding: .utf8) {
                s += "\n"
                ioLock.lock(); defer { ioLock.unlock() }
                FileHandle.standardError.write(s.data(using: .utf8)!)
            }
        }
    }

    func volatile(text: String, tWall: Double, audioStart: Double, audioEnd: Double) {
        if pretty {
            ioLock.lock(); defer { ioLock.unlock() }
            // 同行覆寫：\r 回到行首，輸出即時（未定稿）結果
            let line = "  … \(text)"
            FileHandle.standardOutput.write(("\r" + line + "\u{1B}[K").data(using: .utf8)!)
            lastWasVolatile = true
        } else {
            jsonLine([
                "type": "volatile",
                "text": text,
                "t_wall_ms": ms(tWall),
                "audio_start": audioStart,
                "audio_end": audioEnd,
            ])
        }
    }

    func final(text: String, tWall: Double, audioStart: Double, audioEnd: Double,
               latencySinceLastVolatile: Double?, latencySinceAudioEnd: Double?,
               synthesized: Bool = false) {
        if pretty {
            ioLock.lock(); defer { ioLock.unlock() }
            clearVolatileLineIfNeeded()
            let lv = latencySinceLastVolatile.map { " | volatile→final \(ms($0))ms" } ?? ""
            let ae = latencySinceAudioEnd.map { " | audioEnd→final \(ms($0))ms" } ?? ""
            let syn = synthesized ? " [合成]" : ""
            print("✓\(syn) \(text)\(lv)\(ae)")
        } else {
            var dict: [String: Any] = [
                "type": "final",
                "text": text,
                "t_wall_ms": ms(tWall),
                "audio_start": audioStart,
                "audio_end": audioEnd,
            ]
            if let l = latencySinceLastVolatile { dict["latency_since_last_volatile_ms"] = ms(l) }
            if let l = latencySinceAudioEnd { dict["latency_since_audio_end_ms"] = ms(l) }
            // 只有合成時才加 "synthesized" 欄位：確保既有（真 final）輸出逐位元不變（--wav 回歸）
            if synthesized { dict["synthesized"] = true }
            jsonLine(dict)
        }
    }

    func endpoint(reason: String, tWall: Double) {
        if pretty {
            ioLock.lock(); defer { ioLock.unlock() }
            clearVolatileLineIfNeeded()
            print("── 斷句（\(reason)）──")
        } else {
            jsonLine(["type": "endpoint", "reason": reason, "t_wall_ms": ms(tWall)])
        }
    }

    private func clearVolatileLineIfNeeded() {
        if lastWasVolatile {
            // 清掉當前 volatile 覆寫行，讓後續 final / status 從乾淨行開始
            FileHandle.standardOutput.write("\r\u{1B}[K".data(using: .utf8)!)
            lastWasVolatile = false
        }
    }
}

// MARK: - 麥克風權限

/// 請求麥克風權限；被拒時印出完整診斷（含系統設定路徑）。
/// 重點：CLI 無 app bundle 時 TCC 授權掛在父進程；從 ssh session 可能無彈窗直接 deny，
/// 這種情況要把 authorizationStatus 印清楚，本身就是重要的 spike 情報。
func ensureMicPermission(emitter: EventEmitter) async -> Bool {
    let status = AVCaptureDevice.authorizationStatus(for: .audio)

    func describe(_ s: AVAuthorizationStatus) -> String {
        switch s {
        case .authorized: return "authorized（已授權）"
        case .denied: return "denied（已拒絕）"
        case .restricted: return "restricted（受系統限制，如家長控制）"
        case .notDetermined: return "notDetermined（尚未決定，將觸發請求）"
        @unknown default: return "unknown"
        }
    }

    emitter.status("麥克風授權狀態（請求前）：\(describe(status))")

    switch status {
    case .authorized:
        return true
    case .notDetermined:
        // 觸發系統授權請求。重大實測坑：CLI 無 app bundle 時，此呼叫在有登入 GUI 的
        // session 才會彈窗並回傳；在純 ssh／headless session 下經實測會「無限 hang」
        // （既不彈窗、也不回傳 false），因此必須包一層 timeout，逾時視為此環境無法互動授權。
        emitter.status("狀態為 notDetermined，發出麥克風授權請求（headless 環境可能無彈窗）…")
        let granted = await requestAccessWithTimeout(seconds: 8, emitter: emitter)
        switch granted {
        case .some(true):
            emitter.status("麥克風授權請求結果：granted")
            return true
        case .some(false):
            emitter.status("麥克風授權請求結果：denied")
            printMicDeniedHelp(emitter: emitter)
            return false
        case .none:
            emitter.status("麥克風授權請求逾時（8 秒未回應）——此環境很可能是無 GUI 的 ssh/headless session，系統無法彈出授權視窗。")
            printMicDeniedHelp(emitter: emitter)
            return false
        }
    case .denied, .restricted:
        printMicDeniedHelp(emitter: emitter)
        return false
    @unknown default:
        printMicDeniedHelp(emitter: emitter)
        return false
    }
}

/// 帶超時的麥克風授權請求。回傳 nil 代表逾時（headless 環境的典型症狀）。
///
/// 實作坑（實測踩到）：不能用 async 版 `await AVCaptureDevice.requestAccess` 搭配
/// `Task.sleep` 計時器競速——在無 GUI 的 headless 環境，async requestAccess 會卡死並
/// 佔滿 Swift concurrency 的 cooperative thread pool，導致同池的計時器 task 一起餓死，
/// timeout 永遠不觸發。
///
/// 正解：用「completion-handler 版 requestAccess」+「DispatchQueue.global 的計時器」，
/// 兩者都不依賴 Swift 並發池，用 NSLock 保證只有先到者 resume continuation。
func requestAccessWithTimeout(seconds: Double, emitter: EventEmitter) async -> Bool? {
    await withCheckedContinuation { (cont: CheckedContinuation<Bool?, Never>) in
        let lock = NSLock()
        var finished = false
        func finishOnce(_ value: Bool?) {
            lock.lock(); defer { lock.unlock() }
            if finished { return }
            finished = true
            cont.resume(returning: value)
        }
        // 計時器：跑在 global queue，不受被卡住的授權請求影響
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) {
            finishOnce(nil) // 逾時 → nil
        }
        // completion-handler 版：回呼在任意 thread，正常時會帶回 granted 布林
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            finishOnce(granted)
        }
    }
}

func printMicDeniedHelp(emitter: EventEmitter) {
    emitter.status("""
    麥克風權限未取得。診斷指引：
      1. 若透過 ssh／遠端 session 執行：TCC 可能無法彈出授權視窗而直接拒絕。
         請改在有登入 GUI 的本機終端機（或 tmux 附著於本機 session）執行，讓系統彈窗出現。
      2. 手動授權路徑：系統設定 → 隱私權與安全性 → 麥克風，
         找到執行本程式的終端機（Terminal／iTerm 等）並開啟開關；改完需重啟該終端機。
      3. CLI 無 app bundle 時，TCC 授權掛在「父進程」（你的終端機 app），
         而非本 binary；換終端機或換 shell 可能需重新授權。
      4. 確認當前輸入裝置存在且未被其他 app 佔用（本專案預設麥克風為藍牙 16kHz mono）。
    """)
}

// MARK: - 自製 VAD（RMS 靜音端點偵測）

/// 以音訊 RMS 判斷語音／靜音狀態的簡易端點偵測器。
/// 設計取向：門檻可控、行為可解釋，便於 spike 比較「自製 VAD 斷句」與「自然 finalize」。
/// 狀態機：偵測到語音後開始累積靜音時間，靜音持續超過門檻即回報一次端點事件（上緣觸發）。
final class SilenceVAD {
    private let silenceThresholdMs: Double
    // RMS 門檻（線性振幅）：低於此值視為靜音。16kHz 藍牙麥克風底噪偏高，取相對寬鬆值。
    private let rmsFloor: Float = 0.012

    private var hasHeardSpeech = false     // 是否曾偵測到語音（避免開場純靜音就亂斷）
    private var silenceStartMs: Double? = nil
    private var endpointPending = false    // 是否已對本段靜音回報過端點（防重複觸發）

    init(silenceThresholdMs: Double) {
        self.silenceThresholdMs = silenceThresholdMs
    }

    /// 計算 buffer 的 RMS（取第 0 聲道），回傳 0.0–1.0 的正規化振幅。
    ///
    /// 重大修正（第 6 點 RMS 矛盾的根因）：原本只讀 `floatChannelData`，但當 buffer 是
    /// **Int16 格式時 `floatChannelData` 回傳 nil**，RMS 直接變 0——這正是「心跳 RMS 與
    /// dump 內容差 10 倍、時有時無」的成因（喇叭走 Float32 路徑正常、DJI 走 Int16 路徑歸零）。
    /// 修法：Float32 讀 floatChannelData，Int16 讀 int16ChannelData 並正規化到 ±1.0，兩者皆支援。
    static func rms(of buffer: AVAudioPCMBuffer) -> Float {
        let n = Int(buffer.frameLength)
        guard n > 0 else { return 0 }
        if let ch = buffer.floatChannelData {
            let samples = ch[0]
            var sum: Float = 0
            for i in 0..<n { let s = samples[i]; sum += s * s }
            return (sum / Float(n)).squareRoot()
        }
        if let ch = buffer.int16ChannelData {
            // Int16 正規化：除以 32768 換算成 ±1.0 的等效振幅，與 Float32 路徑同尺度
            let samples = ch[0]
            var sum: Double = 0
            for i in 0..<n { let s = Double(samples[i]) / 32768.0; sum += s * s }
            return Float((sum / Double(n)).squareRoot())
        }
        // 其他格式（如 Int32）：無法讀取，回 0（並非靜音，只是無法計算——診斷會另外標示）
        return 0
    }

    /// 餵入「已算好的 RMS」與其對應的牆鐘時間；若本塊造成「靜音跨過門檻」則回傳 true（應斷句）。
    /// （RMS 由呼叫端計算後傳入，避免對同一 buffer 重複計算。）
    func feed(rms level: Float, nowMs: Double) -> Bool {
        if level >= rmsFloor {
            // 偵測到語音：重置靜音計時，解除 pending
            hasHeardSpeech = true
            silenceStartMs = nil
            endpointPending = false
            return false
        }
        // 靜音中
        guard hasHeardSpeech else { return false } // 還沒講過話，不斷句
        if silenceStartMs == nil {
            silenceStartMs = nowMs
            return false
        }
        let dur = nowMs - (silenceStartMs ?? nowMs)
        if dur >= silenceThresholdMs && !endpointPending {
            endpointPending = true // 只在跨過門檻的當下觸發一次
            return true
        }
        return false
    }
}

// MARK: - 主轉寫流程

/// 封裝 SpeechAnalyzer 管線的建立、音訊擷取、結果消費與 VAD 斷句。
final class STTRunner: @unchecked Sendable {
    let opts: Options
    let emitter: EventEmitter

    private let engine = AVAudioEngine()
    private var analyzer: SpeechAnalyzer!
    private var transcriber: SpeechTranscriber!
    private var converter: AVAudioConverter?
    private var analyzerFormat: AVAudioFormat!

    // AnalyzerInput 串流：tap callback 把轉換後的 buffer 丟進來
    private var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?

    // 音訊時間游標（以 analyzer 格式的 sample 累計），供 VAD finalize(through:) 使用
    private var audioSampleCursor: AVAudioFramePosition = 0
    private let cursorLock = NSLock()

    // VAD 觸發 finalize 需要在 async context 呼叫；用一個序列化 Task 佇列避免重入
    private let vad: SilenceVAD?

    // volatile→final 延遲量測：記錄「同一段」最後一次 volatile 的牆鐘時間
    private var lastVolatileWallMs: Double? = nil

    // ---- 診斷計數器（週期 status 用；跨 audio thread 存取，故上鎖）----
    private let diagLock = NSLock()
    private var tapBufferCount = 0        // tap 進來幾塊
    private var convertedFrameTotal = 0   // 轉換後累計送入 analyzer 的 frames
    private var yieldCount = 0            // 實際 yield 給 analyzer 幾塊（生產端計數，非消費端）
    // RMS 統計改為「上一個心跳週期內的峰值 + 樣本數」——比「最近一塊」有代表性，
    // 且能反映該週期是否真的有語音（第 6 點：原本只記最近一塊，時點不對又易漏峰值）。
    private var rmsPeakThisPeriod: Float = 0   // 本週期 tap buffer RMS 峰值
    private var rmsSampleCount = 0             // 本週期取樣了幾塊（0 = 這 2 秒沒有任何 tap）
    // ---- results 交付診斷（區分「已 commit 未交付」vs「消費者餓死」的關鍵）----
    // live 27s 凍結現場：finalize 正常返回、volatileRange 前進（analyzer 已 commit），卻只交付 1 筆。
    // 這兩個計數讓週期診斷能直接顯示「analyzer 消費前進但 results 卻長時間收 0 筆」＝交付端凍結鐵證。
    private var resultsReceivedCount = 0       // consumeResults 迄今收到的 result 總筆數
    private var lastResultWallMs: Double = 0   // 上一次收到 result 的牆鐘時間（0=尚未收到任何）

    // ---- --dump-audio：把實際餵進 analyzer 的音訊同步寫成 WAV ----
    // 用 AVAudioFile 以 analyzer 期待格式（16kHz Int16 mono）開檔；tap thread 寫入需上鎖。
    private var dumpFile: AVAudioFile?
    private let dumpLock = NSLock()
    private var dumpFramesWritten: AVAudioFramePosition = 0

    // tap 實際 buffer 格式與安裝格式不符時，只示警一次（避免洗版）
    private var formatMismatchWarned = false
    // 轉換器重建失敗時，只示警一次（避免洗版）
    private var converterRebuildWarned = false

    init(opts: Options, emitter: EventEmitter) {
        self.opts = opts
        self.emitter = emitter
        self.vad = opts.useVAD ? SilenceVAD(silenceThresholdMs: opts.silenceMs) : nil
    }

    /// 建立 transcriber + analyzer，取最佳音訊格式，確認 locale 資產可用。
    func setup() async throws {
        let locale = Locale(identifier: opts.localeID)

        // 先檢查引擎是否可用（硬體/OS 層級）
        if !SpeechTranscriber.isAvailable {
            emitter.status("警告：SpeechTranscriber.isAvailable == false，此機型或 OS 可能不支援 on-device 轉寫。")
        }

        // reportingOptions 開 volatileResults 才拿得到即時（未定稿）結果；
        // attributeOptions 開 audioTimeRange 才能從結果取音訊時間範圍。
        // --fast-results：額外加入 .fastResults（診斷「final 交付延遲」假說用；預設不開，維持既有行為）。
        var reporting: Set<SpeechTranscriber.ReportingOption> = [.volatileResults]
        if opts.useFastResults { reporting.insert(.fastResults) }
        transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: reporting,
            attributeOptions: [.audioTimeRange]
        )
        emitter.status("transcriber reportingOptions：volatileResults\(opts.useFastResults ? " + fastResults" : "")")

        // 確認 locale 資產已安裝；未安裝則提示（下載由 --check 負責，此處只警告不阻斷）
        let installed = await SpeechTranscriber.installedLocales
        let isInstalled = installed.contains { $0.identifier(.bcp47) == locale.identifier(.bcp47)
            || $0.identifier == locale.identifier }
        if !isInstalled {
            emitter.status("警告：locale \(opts.localeID) 的資產似乎未安裝。請先執行 --check 觸發下載，否則轉寫可能失敗。")
        }

        analyzer = SpeechAnalyzer(modules: [transcriber])

        // 取最佳音訊格式：analyzer 要求的格式通常與麥克風硬體格式不同，需 AVAudioConverter 轉換。
        guard let best = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw SpikeError.noAudioFormat
        }
        analyzerFormat = best
        emitter.status("analyzer 最佳音訊格式：\(best.sampleRate)Hz, \(best.channelCount)ch, \(best.commonFormat.rawValue)")
    }

    /// 啟動音訊擷取與轉寫，阻塞直到收到中斷訊號。
    func run() async throws {
        // 建立 AnalyzerInput 串流
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.inputContinuation = continuation

        // 設定麥克風 tap
        let inputNode = engine.inputNode
        let hwFormat = inputNode.outputFormat(forBus: 0)
        emitter.status("麥克風硬體格式：\(hwFormat.sampleRate)Hz, \(hwFormat.channelCount)ch, commonFormat=\(hwFormat.commonFormat.rawValue)")

        // 注意：這裡「不」預先建立 converter。
        // 原本在此以安裝時的 hwFormat 一次性建立 converter，但藍牙裝置（DJI Mic 2）的
        // 實際 tap buffer 格式可能與 outputFormat(forBus:) 回報的不同、或啟動瞬間 route 未穩，
        // 導致 converter 綁到錯的來源格式。改為在 convertIfNeeded 內「以每一塊 buffer 的實際
        // 格式」惰性建立/重建 converter，並保證絕不把未轉換的 buffer 餵給 analyzer。
        emitter.status("analyzer 期待格式：\(analyzerFormat.sampleRate)Hz/\(analyzerFormat.commonFormat == .pcmFormatInt16 ? "Int16" : "common\(analyzerFormat.commonFormat.rawValue)")/il\(analyzerFormat.isInterleaved ? 1 : 0)（轉換器將依實際 buffer 格式惰性建立）")
        if formatsEqual(hwFormat, analyzerFormat) {
            emitter.status("tap 安裝格式與 analyzer 期待相同，預期免轉換（仍會逐塊核對實際格式）。")
        }

        // ★★★ 本輪零事件 race 的核心修復：嚴格的啟動順序 ★★★
        //
        // 鐵證（run 1 失敗 vs run 2 成功的診斷對比）：兩者音訊格式、轉換器、analyzer 消費
        // （volatileRange 前進）全都正常，唯一差別是 run 1 的 results 迴圈「已啟動卻永遠收不到
        // 第 1 筆」。另一探針證實：`transcriber.results` 是**單一消費者** AsyncSequence，
        // 同時開兩個迭代器會 fatal error——代表 analyzer.start 內部會連接 transcriber 的
        // results 通道，若我方 for-await 與 SDK 內部連接的相對時序不對（高併發下才觸發），
        // 我方迭代器就接不到 analyzer 送出的結果。--wav 不失敗是因為它 start 後緊接單一 async
        // 迴圈餵檔、無其他並發打斷；live 有 audio thread + 多 task 併發，才會 race。
        //
        // 修法：把順序鎖死為
        //   (1) analyzer.start(inputSequence:)  ← 先讓 SDK 完成對 transcriber 的內部連接
        //   (2) 建立 results 消費 task 並「確認它真的開始 for-await」（等一個就緒訊號）
        //   (3) 才安裝 tap + 啟動 engine  ← 音訊在消費者就緒後才開始流動
        // 這樣 SDK 內部連接與我方訂閱不再與音訊/其他 task 競爭時序。

        // (1) 先啟動 analyzer（此時尚無音訊流入，stream 為空）
        try await analyzer.start(inputSequence: stream)
        emitter.status("analyzer.start 已返回（SDK 內部通道就緒）")

        // (2) 建立 results 消費 task，並等它確實進入 for-await 才繼續
        let resultsReady = AsyncStream<Void>.makeStream()
        // ★ 消費者提升為高優先級：對抗「live 27s 凍結」最可能的機制——結果消費 Task 在 live 進程
        //   （即時音訊執行緒 + on-device 推論的 CPU 壓力下）被協程排程餓死，導致 analyzer 已 commit
        //   的 final 遲遲交付不出來。高優先級讓消費者能搶先被排程，不因背景負載而長時間停擺。
        //   （此為針對 live 的緩解；無麥克風環境的 file-feed 本就不凍結，改動已驗證不影響其輸出。）
        let resultsTask = Task(priority: .high) { [weak self] in
            guard let self else { return }
            await self.consumeResults(readyContinuation: resultsReady.continuation)
        }
        // 等待「消費迴圈已進入 for-await」訊號（最多等 1 秒保底，不無限卡）
        await withTaskGroup(of: Void.self) { g in
            g.addTask { var it = resultsReady.stream.makeAsyncIterator(); _ = await it.next() }
            g.addTask { try? await Task.sleep(nanoseconds: 1_000_000_000) }
            _ = await g.next()
            g.cancelAll()
        }

        // 週期診斷 Task
        let diagTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { break }
                await self.emitPeriodicDiag()
            }
        }

        // 週期 finalize「安全網」Task：SpeechAnalyzer 是「finalize 才 flush」模型。
        // 改用 VAD 抑制的安全網（見 runFlushSafetyNet 說明）——VAD 在真實靜音邊界定稿為主，
        // 安全網只在連續語音長時間無自然斷點時兜底，避免原本 metronome 把對話句切成碎片。
        let flushTask = Task { [weak self] in
            guard let self else { return }
            await self.runFlushSafetyNet()
        }

        // --dump-audio：開檔以 analyzerFormat 落地為 WAV
        if let dumpPath = opts.dumpAudioPath {
            openDumpFile(path: dumpPath)
        }

        // (3) 消費者就緒後，才安裝 tap + 啟動 engine，讓音訊開始流入
        //
        // ★ tap format 一律傳 nil（使用 input node 當下的實際格式）：
        // 以 engine 啟動前查詢的 hwFormat 宣告 tap，遇到「查詢值與實際 route 不符」
        // （裝置切換後常見，實測 44.1kHz/2ch 內建麥）會直接拋
        // 'Failed to create tap due to format mismatch' 讓 helper 崩潰。
        // 本程式本就依每塊 buffer 的實際格式惰性建 converter（見 convertIfNeeded），
        // 不依賴 tap 宣告格式；hwFormat 僅作診斷比對基準。
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: nil) { [weak self] buffer, _ in
            guard let self else { return }
            self.handleTap(buffer: buffer, hwFormat: hwFormat)
        }
        engine.prepare()
        try engine.start()
        emitter.status("音訊引擎已啟動，等待音訊…（Ctrl-C 結束）")

        // 阻塞等待：靠外部訊號（Ctrl-C）觸發 finish。
        while !Task.isCancelled && !stopRequested {
            try? await Task.sleep(nanoseconds: 200_000_000)
        }

        flushTask.cancel()
        diagTask.cancel()
        resultsTask.cancel()
    }

    // finalize 序列化旗標：避免週期 flush 與 VAD 斷句同時對 analyzer finalize 造成重入
    private let finalizeLock = NSLock()
    private var finalizeInFlight = false
    private var lastFinalizedCursor: AVAudioFramePosition = 0
    private var finalizeSeq = 0                    // 已發出的 finalize 次數（診斷）
    private var finalizeSkipCount = 0              // in-flight 造成的連續 skip（卡死偵測）
    private var finalizeLastThroughSec: Double = 0 // 最後一次 finalize 的 through 秒數（診斷）
    private var lastFinalizeAtWall: Double = 0     // 上一次成功 finalize 的牆鐘時間（VAD 或安全網皆更新）
                                                   // ——安全網據此判斷「距上次 finalize 是否已達 flushMs」

    // ---- 「volatile 合成 final」fallback 狀態（跨 results task / tap 執行緒 / 安全網 task，需上鎖）----
    // 背景：live 實測 SDK 的 isFinal 完全不交付（volatile 正常）。放棄依賴 SDK final，改由 helper
    // 在段落邊界（VAD 靜音端點、安全網逾時）以「最近一筆 volatile」合成 final，確保「有 volatile 就終究有 final」。
    private let synthLock = NSLock()
    private var curVolatileText = ""       // 最近一筆 volatile 的文字（合成內容來源）
    private var curVolatileStart = 0.0     // 最近 volatile 的 audio_start（秒）
    private var curVolatileEnd = 0.0       // 最近 volatile 的 audio_end（秒）
    private var lastEmittedEnd = -1.0      // 已（合成或真）吐出 final 的最後 audio 秒——去重水位
    private var lastEmittedAtWall = 0.0    // 上次吐出任何 final 的牆鐘（安全網合成兜底據此判「太久沒 final」）
    private var synthCount = 0             // 合成 final 筆數（診斷）
    private var sawSpeechSinceEmit = false // 自上次吐 final 後是否偵測到語音（RMS≥門檻）
                                           // ——安全網合成只在「有語音」時才觸發，避免對靜音期殘留 volatile 反覆合成垃圾
    // 真 final 去重容差：VAD 端點合成後隨即 periodicFinalize(through:游標)，其真 final 的 audio_end
    // 可能比合成當下游標略大（期間又餵入少許音訊）。給 0.6s 容差把「同一段的真 final」視為重複丟棄，
    // 同時遠小於 VAD 靜音門檻（≥600ms）＋下一句長度，不會誤丟真正的新段。
    private let dedupTolSec = 0.6

    /// 週期 flush：對目前音訊游標 finalize(through:)，讓 analyzer 吐出累積結果。
    /// 不中止串流（後續音訊照常轉寫）。有新音訊才做，避免對同一位置重複 finalize。
    private func periodicFinalize() async {
        // 取游標；若自上次 finalize 後沒有新音訊，跳過
        cursorLock.lock()
        let cursor = audioSampleCursor
        cursorLock.unlock()

        finalizeLock.lock()
        let already = finalizeInFlight
        let last = lastFinalizedCursor
        if already || cursor <= last {
            // 診斷「finalize 卡死」假說的關鍵訊號：若 in-flight 造成的 skip 連續累積，
            // 表示上一次 finalize 的 await 從未返回（analyzer 靜默卡死，不拋錯）。
            if already {
                finalizeSkipCount += 1
                if finalizeSkipCount == 3 || finalizeSkipCount % 20 == 0 {
                    emitter.status("⚠ 週期 finalize 已連續 skip \(finalizeSkipCount) 次：上一次 finalize（#\(finalizeSeq)，through=\(String(format: "%.2f", finalizeLastThroughSec))s）之 await 尚未返回——疑似 analyzer finalize 卡死。")
                }
            }
            finalizeLock.unlock()
            return
        }
        finalizeInFlight = true
        finalizeSkipCount = 0
        finalizeSeq += 1
        let seq = finalizeSeq
        finalizeLastThroughSec = Double(cursor) / analyzerFormat.sampleRate
        finalizeLock.unlock()

        // 前 5 次逐次記錄、之後每 10 次一記（節流；卡死時「開始」有記而「完成」缺席即為鐵證）
        let verbose = seq <= 5 || seq % 10 == 0
        if verbose {
            emitter.status("週期 finalize #\(seq) 開始（through=\(String(format: "%.2f", finalizeLastThroughSec))s）")
        }
        let through = CMTime(value: cursor, timescale: CMTimeScale(analyzerFormat.sampleRate))
        do {
            try await analyzer?.finalize(through: through)
            if verbose {
                emitter.status("週期 finalize #\(seq) 完成")
            }
        } catch {
            emitter.status("週期 finalize #\(seq) 失敗：\(error.localizedDescription)")
        }
        finalizeLock.lock()
        finalizeInFlight = false
        lastFinalizedCursor = cursor
        lastFinalizeAtWall = MonoClock.nowMs() // 記錄本次 finalize 完成時刻，供安全網抑制冗餘 finalize
        finalizeLock.unlock()
    }

    /// 取當前音訊游標對應秒數（合成時作為「涵蓋到此」的去重水位）。
    private func currentCursorSec() -> Double {
        cursorLock.lock(); let c = audioSampleCursor; cursorLock.unlock()
        return analyzerFormat != nil ? Double(c) / analyzerFormat.sampleRate : 0
    }

    /// 記錄最近一筆 volatile（供合成 final 使用）。在 consumeResults 的 volatile 分支呼叫。
    private func noteVolatile(text: String, start: Double, end: Double) {
        synthLock.lock()
        curVolatileText = text
        curVolatileStart = start
        curVolatileEnd = end
        synthLock.unlock()
    }

    /// 標記本塊音訊是否為語音（RMS≥門檻）。供安全網合成兜底判斷「該段自上次 final 後有語音」，
    /// 避免對靜音期的殘留/雜訊 volatile 反覆合成。門檻與 SilenceVAD.rmsFloor 對齊（0.012）。
    private func markSpeech(rms: Float) {
        if rms >= 0.012 {
            synthLock.lock(); sawSpeechSinceEmit = true; synthLock.unlock()
        }
    }

    /// 真 isFinal 到達時的去重判定＋水位更新。回傳 true＝應丟棄（該段已由合成送出）。
    /// 規則（協調者指定）：audio_end ≤ 已吐水位（含容差）→ 丟棄；否則視為新段，照常吐並更新水位。
    private func shouldDropRealFinal(audioEnd: Double, nowWall: Double) -> Bool {
        synthLock.lock(); defer { synthLock.unlock() }
        if audioEnd <= lastEmittedEnd + dedupTolSec {
            return true
        }
        lastEmittedEnd = audioEnd
        lastEmittedAtWall = nowWall
        curVolatileText = ""   // 該段已由真 final 定稿，清 volatile 累積
        sawSpeechSinceEmit = false
        return false
    }

    /// 從「最近一筆 volatile」合成一筆 final（放棄依賴 SDK 的 final 交付）。
    /// - reason：觸發來源（"vad" 靜音端點 ／ "safety" 安全網逾時），僅供診斷。
    /// - coverThroughSec：本段「涵蓋到」的 audio 秒（去重水位取此值與 volatile end 的較大者）。
    ///   通常傳當下游標：確保隨後 finalize(through:游標) 若真吐 final，會因 audio_end≤水位而被去重。
    /// 回傳是否確實合成（僅在「有 volatile 內容且該段尚未吐過 final」時）。
    @discardableResult
    private func synthesizeFinal(reason: String, coverThroughSec: Double) -> Bool {
        guard opts.synthFinal else { return false }
        synthLock.lock()
        let text = curVolatileText
        let start = curVolatileStart
        let end = curVolatileEnd
        // 空內容、或該段已吐過 final（end 未超過水位）→ 不合成
        if text.isEmpty || end <= lastEmittedEnd + 1e-6 {
            synthLock.unlock()
            return false
        }
        let now = MonoClock.nowMs()
        let watermark = max(end, coverThroughSec)
        lastEmittedEnd = watermark
        lastEmittedAtWall = now
        synthCount += 1
        let sc = synthCount
        curVolatileText = ""   // 清累積，避免同段重複合成
        sawSpeechSinceEmit = false
        synthLock.unlock()

        let lv = lastVolatileWallMs.map { now - $0 }
        let ae = latencySinceAudioEnd(audioEndSec: end, nowMs: now)
        emitter.final(text: text, tWall: now, audioStart: start, audioEnd: end,
                      latencySinceLastVolatile: lv, latencySinceAudioEnd: ae, synthesized: true)
        lastVolatileWallMs = nil
        emitter.status(String(format: "合成 final #%d（%@）：audio %.2f–%.2fs（去重水位=%.2fs）「%@」",
                              sc, reason, start, end, watermark, text))
        return true
    }

    /// 週期 finalize「安全網」（取代原本的固定間隔 metronome）。
    ///
    /// 設計轉變的根因：原本每 flushMs 無條件 finalize(through:)，在「連續語音、無自然靜音」
    /// （壓胸數數、慌亂連講）時可接受，但套到「有停頓的對話」上，會在句子講到一半就硬切，
    /// 迫使 analyzer 在缺乏後文脈絡時定稿 → 產出單字碎片甚至誤字（實測 700ms 把整段報案
    /// 切成「是天才相」「租車」等無用碎片；且中途硬切會丟失原本 VAD 在真實靜音切能保住的內容）。
    ///
    /// 新語意：安全網只在「距上一次任何 finalize（VAD 靜音斷句或前一次安全網）已達 flushMs」
    /// 時才補一刀。效果：
    ///   1. VAD 在真實靜音邊界的 finalize 成為主力 → 完整、保內容的句子。
    ///   2. 安全網只在持續語音長時間沒有自然斷點時兜底 → 把最壞延遲限制在 flushMs 內
    ///      （數數等連續語音仍能定期吐出、被偵測），同時大幅降低對 analyzer 的 finalize 壓力。
    /// flushMs<=0 則完全停用安全網（純 VAD + 收尾 finalize）。
    private func runFlushSafetyNet() async {
        let flushMs = opts.flushMs
        if flushMs <= 0 {
            emitter.status("週期 finalize 安全網已停用（flush-ms<=0）：僅靠 VAD 靜音斷句與收尾 finalize。")
            return
        }
        emitter.status("週期 finalize 安全網啟用：距上次 finalize 達 \(Int(flushMs))ms 且有新音訊才補刀（VAD 為主）。")
        // 起始基準設為現在，讓「開場第一句」有機會先由 VAD 在其自然停頓定稿，而非被安全網提前切斷
        let startNow = MonoClock.nowMs()
        finalizeLock.lock()
        lastFinalizeAtWall = startNow
        finalizeLock.unlock()
        synthLock.lock()
        lastEmittedAtWall = startNow
        synthLock.unlock()
        // 合成兜底的額外寬限：安全網 finalize 後，先給真 final 一段時間到達（file-feed 有真 final），
        // 逾此才合成 → live（無真 final）定期合成、file-feed 則因真 final 刷新水位而不誤觸。
        let synthGraceMs = 500.0
        // 以較短輪詢間隔檢查「距上次 finalize 是否已超過 flushMs」（不再是固定間隔硬切）
        let pollNs: UInt64 = 200_000_000
        while !Task.isCancelled && !stopRequested {
            try? await Task.sleep(nanoseconds: pollNs)
            if Task.isCancelled || stopRequested { break }
            let now = MonoClock.nowMs()
            finalizeLock.lock()
            let since = now - lastFinalizeAtWall
            finalizeLock.unlock()
            if since >= flushMs {
                await periodicFinalize()
            }
            // 合成兜底（連續語音如壓胸數數無 VAD 斷點時的保底）：距上次吐出「任何 final」已超過
            // flushMs+寬限、且仍有未定稿 volatile → 以最近 volatile 合成，確保「有 volatile 就終究有 final」。
            synthLock.lock()
            let vEnd = curVolatileEnd
            let emittedEnd = lastEmittedEnd
            let emittedWall = lastEmittedAtWall
            let hasText = !curVolatileText.isEmpty
            let hadSpeech = sawSpeechSinceEmit
            synthLock.unlock()
            if hadSpeech && hasText && vEnd > emittedEnd + 1e-6 && (now - emittedWall) >= (flushMs + synthGraceMs) {
                synthesizeFinal(reason: "safety", coverThroughSec: currentCursorSec())
            }
        }
    }

    /// 判斷兩個音訊格式是否「完全等價」（取樣率 + 位元深度 + 聲道數）。
    private func formatsEqual(_ a: AVAudioFormat, _ b: AVAudioFormat) -> Bool {
        a.commonFormat == b.commonFormat
            && a.sampleRate == b.sampleRate
            && a.channelCount == b.channelCount
    }

    /// SIGINT 旗標：讓 run() 的等待迴圈能主動結束
    private var stopRequested = false
    func requestStop() { stopRequested = true }

    /// 印一次週期診斷（走 status → stderr）。
    ///
    /// 關鍵：yield 計數是「生產端」（丟進 AsyncStream 幾塊），AsyncStream 預設 unbounded
    /// 緩衝、yield 永不阻塞，所以就算 analyzer 完全沒消費，yield/frames 照樣一直漲。
    /// 因此額外印 analyzer.volatileRange 作為「消費端」代理指標——它是 analyzer 實際
    /// 處理到的音訊時間範圍。若 yield 一直漲、但 volatileRange 停在 nil 或不前進，
    /// 即代表「音訊有進、analyzer 卻沒消費」（生產/消費脫鉤），這正是要抓的失敗模式。
    private func emitPeriodicDiag() async {
        diagLock.lock()
        let taps = tapBufferCount
        let frames = convertedFrameTotal
        let yields = yieldCount
        let rmsPeak = rmsPeakThisPeriod
        let rmsSamples = rmsSampleCount
        let recvCount = resultsReceivedCount
        let lastRecv = lastResultWallMs
        // 讀完即重置本週期 RMS 統計，讓下次心跳反映的是「這 2 秒」的峰值
        rmsPeakThisPeriod = 0
        rmsSampleCount = 0
        diagLock.unlock()

        // 讀 analyzer 實際處理進度（actor 隔離屬性，需 await）
        var consumedDesc = "nil"
        if let a = analyzer {
            if let vr = await a.volatileRange {
                let endSec = (vr.start + vr.duration).seconds
                consumedDesc = String(format: "%.2f–%.2fs", vr.start.seconds, endSec.isFinite ? endSec : vr.start.seconds)
            } else {
                consumedDesc = "nil（analyzer 尚未產生 volatile 範圍）"
            }
        }

        let fedSec = Double(frames) / (analyzerFormat?.sampleRate ?? 16000)
        // rmsSamples==0 表示這 2 秒完全沒有 tap buffer 進來（tap 停了）——與「有 tap 但很安靜」不同
        let rmsDesc = rmsSamples == 0 ? "無tap" : String(format: "%.4f(峰/%d塊)", rmsPeak, rmsSamples)
        // results 交付狀態：收到筆數 + 距上次收到秒數。若 analyzer 消費(volatileRange)持續前進、
        // 但此處 recv 長時間不動 → 就是「已 commit 未交付／消費者餓死」的凍結鐵證。
        let recvDesc: String
        if recvCount == 0 {
            recvDesc = "0筆(尚未交付任何 result！)"
        } else {
            let sinceLast = (MonoClock.nowMs() - lastRecv) / 1000.0
            recvDesc = String(format: "%d筆(距上次%.1fs)", recvCount, sinceLast)
        }
        emitter.status(String(format: "診斷：tap %d 塊 / yield %d 塊 %d frames(≈%.1fs) / RMS %@ / analyzer 消費(volatileRange)=%@ / results 交付=%@",
                              taps, yields, frames, fedSec, rmsDesc, consumedDesc, recvDesc))
    }

    /// tap callback：計算 VAD、轉換格式、推進音訊游標、丟入 analyzer 串流。
    private func handleTap(buffer: AVAudioPCMBuffer, hwFormat: AVAudioFormat) {
        let now = MonoClock.nowMs()

        // ★ 藍牙 route/codec 變化偵測（本次推理的核心可疑點）：
        // tap 是以「安裝當下的 hwFormat」宣告的，但實際傳入的 buffer 帶自己的 format。
        // 藍牙裝置（如 DJI Mic 2）在 HFP/A2DP 切換或重連時，實際取樣率可能與安裝時不符；
        // 此時 converter（用舊格式建立）會產出變速/錯誤音訊，甚至讓 analyzer 靜默排斥。
        // 這裡偵測到不符就大聲示警（spike 的職責是揭露真相，不默默吞掉）。
        let bufFmt = buffer.format
        if bufFmt.sampleRate != hwFormat.sampleRate || bufFmt.channelCount != hwFormat.channelCount {
            if !formatMismatchWarned {
                formatMismatchWarned = true
                emitter.status(String(format: "⚠ 音訊格式不符！tap 安裝時 %.0fHz/%dch，但實際 buffer 為 %.0fHz/%dch。可能是藍牙 route 切換——converter 用的是舊格式，analyzer 可能收到變速音訊而靜默不吐。這極可能就是「音訊有進卻零 result」的成因。",
                                          hwFormat.sampleRate, hwFormat.channelCount, bufFmt.sampleRate, bufFmt.channelCount))
            }
        }

        // 診斷：tap 進來一塊 + 記錄本週期 RMS 峰值（RMS 只算一次，同時給 VAD 用）
        let rms = SilenceVAD.rms(of: buffer)
        diagLock.lock()
        tapBufferCount += 1
        rmsSampleCount += 1
        if rms > rmsPeakThisPeriod { rmsPeakThisPeriod = rms }
        diagLock.unlock()
        markSpeech(rms: rms) // 標記本段是否有語音（供安全網合成兜底判斷）

        // VAD 靜音端點：用已算好的 rms（不重算）。偵測到端點走序列化 finalize 路徑。
        if let vad = vad {
            let shouldEndpoint = vad.feed(rms: rms, nowMs: now)
            if shouldEndpoint {
                emitter.endpoint(reason: "vad_silence", tWall: now)
                // 「volatile 合成 final」：靜音端點＝一個語義段結束，直接以最近 volatile 合成 final。
                // （live SDK 的 isFinal 不交付，這是拿到 final 的主力路徑；隨後的真 final 會被去重。）
                synthesizeFinal(reason: "vad", coverThroughSec: currentCursorSec())
                Task { [weak self] in
                    guard let self else { return }
                    await self.periodicFinalize()
                }
            }
        }

        // 轉換格式（若需要）並送入串流
        guard let outBuffer = convertIfNeeded(buffer) else { return }
        feedToAnalyzer(outBuffer)
    }

    // 目前 converter 是以哪個「來源格式」建立的（用來偵測 buffer 格式改變需重建）
    private var converterSourceFormat: AVAudioFormat?

    /// 把來源 buffer 轉成 analyzer 期待的格式，**保證回傳的一定是 analyzerFormat**。
    ///
    /// ★★ 本輪零事件根因的核心修復 ★★
    /// 實測鐵證：把 Float32 buffer 餵給期待 Int16 的 analyzer 時，analyzer 會「靜默中毒」
    /// ——收下 buffer、volatileRange 照常前進，但永不吐 result、永不報錯，且後續 finalize
    /// 永久卡住。這與使用者失敗現場（零事件＋volatileRange 前進＋零錯誤＋回餵正常）完全吻合。
    ///
    /// 原實作的致命缺陷：`guard let converter else { return buffer }` ——當 converter 因故為 nil
    /// （藍牙 route 啟動瞬間未穩、AVAudioConverter 建立失敗等）時，直接把原始 Float32 buffer
    /// 餵進 analyzer，正中毒點。喇叭走 48kHz 內建 route（converter 正常）所以不觸發。
    ///
    /// 新契約：
    ///   1. 若 buffer 已是 analyzerFormat（同格式），直接回傳。
    ///   2. 否則必須轉換；converter 缺失或來源格式與 buffer 不符時，就地以「buffer 的實際格式」重建。
    ///   3. 轉換失敗或連 converter 都建不出來 → **回傳 nil（丟棄該塊）**，絕不把非 Int16 buffer 餵進去。
    private func convertIfNeeded(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let inFmt = buffer.format

        // (1) 已是 analyzer 格式：直接用
        if formatsEqual(inFmt, analyzerFormat) {
            return buffer
        }

        // (2) 確保有一個「來源格式 == 當前 buffer 格式」的 converter；不符就重建
        if converter == nil || converterSourceFormat == nil || !formatsEqual(converterSourceFormat!, inFmt) {
            let newConv = AVAudioConverter(from: inFmt, to: analyzerFormat)
            if newConv == nil {
                if !converterRebuildWarned {
                    converterRebuildWarned = true
                    emitter.status("⚠ 無法建立 \(inFmt.sampleRate)Hz/common\(inFmt.commonFormat.rawValue) → \(analyzerFormat.sampleRate)Hz/Int16 的轉換器，丟棄此類 buffer（不餵未轉換音訊給 analyzer，以免其靜默中毒）。")
                }
                converter = nil
                converterSourceFormat = nil
                return nil // 寧可丟棄，絕不餵原始格式
            }
            converter = newConv
            converterSourceFormat = inFmt
            emitter.status("音訊轉換器（重）建立：\(inFmt.sampleRate)Hz/common\(inFmt.commonFormat.rawValue)/il\(inFmt.isInterleaved ? 1 : 0) → \(analyzerFormat.sampleRate)Hz/Int16")
        }
        guard let converter = converter else { return nil }

        // (3) 執行轉換；輸出 buffer 以 analyzerFormat 建立
        let ratio = analyzerFormat.sampleRate / inFmt.sampleRate
        let outCapacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let conv = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: max(outCapacity, 64)) else {
            emitter.status("建立輸出 buffer 失敗")
            return nil
        }
        var consumed = false
        var convErr: NSError?
        let stat = converter.convert(to: conv, error: &convErr) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if stat == .error || conv.frameLength == 0 {
            if let e = convErr { emitter.status("音訊轉換錯誤：\(e.localizedDescription)") }
            return nil
        }
        return conv
    }

    /// 推進游標、更新診斷計數、（若啟用）寫入 dump WAV 並 yield 給 analyzer。
    private func feedToAnalyzer(_ outBuffer: AVAudioPCMBuffer) {
        // 先取「本塊起始」游標（推進前），供 bufferStartTime 使用
        cursorLock.lock()
        let startCursor = audioSampleCursor
        cursorLock.unlock()

        advanceCursor(by: AVAudioFramePosition(outBuffer.frameLength))
        diagLock.lock()
        convertedFrameTotal += Int(outBuffer.frameLength)
        yieldCount += 1
        diagLock.unlock()

        // --dump-audio：把「餵給 analyzer 的同一塊 buffer」寫進 WAV（tap thread，需上鎖）
        writeDump(outBuffer)

        // --buffer-start-time：帶明確 bufferStartTime（以 analyzer 取樣率的樣本游標為時間軸）。
        // 預設不帶（既有行為）：讓 SDK 以輸入累計時長自行推算時間軸。
        if opts.useBufferStartTime {
            let startTime = CMTime(value: startCursor, timescale: CMTimeScale(analyzerFormat.sampleRate))
            inputContinuation?.yield(AnalyzerInput(buffer: outBuffer, bufferStartTime: startTime))
        } else {
            inputContinuation?.yield(AnalyzerInput(buffer: outBuffer))
        }
    }

    /// 開啟 dump WAV 檔（以 analyzerFormat = 16kHz Int16 mono 落地）。
    private func openDumpFile(path: String) {
        let url = URL(fileURLWithPath: path)
        do {
            // 用 analyzerFormat 的 settings 開檔，確保落地格式 = analyzer 實際吃的格式。
            // commonFormat=Int16 時，AVAudioFile 會寫 16-bit PCM WAV。
            let file = try AVAudioFile(forWriting: url,
                                       settings: analyzerFormat.settings,
                                       commonFormat: analyzerFormat.commonFormat,
                                       interleaved: analyzerFormat.isInterleaved)
            dumpLock.lock()
            dumpFile = file
            dumpFramesWritten = 0
            dumpLock.unlock()
            emitter.status("--dump-audio：開始寫入 \(path)（\(analyzerFormat.sampleRate)Hz \(analyzerFormat.commonFormat == .pcmFormatInt16 ? "Int16" : "格式\(analyzerFormat.commonFormat.rawValue)") mono）")
        } catch {
            emitter.status("--dump-audio 開檔失敗：\(error.localizedDescription)")
        }
    }

    /// 把一塊 buffer 寫入 dump WAV（若已開檔）。
    private func writeDump(_ buffer: AVAudioPCMBuffer) {
        dumpLock.lock()
        defer { dumpLock.unlock() }
        guard let file = dumpFile else { return }
        do {
            try file.write(from: buffer)
            dumpFramesWritten += AVAudioFramePosition(buffer.frameLength)
        } catch {
            // 寫入失敗只記一次狀態，避免洗版；後續放棄 dump
            emitter.status("--dump-audio 寫入失敗，停止 dump：\(error.localizedDescription)")
            dumpFile = nil
        }
    }

    /// 關閉 dump 檔：AVAudioFile 於釋放時會補正 WAV header 的 size 欄位。
    /// 這裡主動置 nil 觸發釋放，確保 SIGINT 收尾時檔案 header 完整、可被 --wav 回餵。
    private func closeDumpFile() {
        dumpLock.lock()
        let frames = dumpFramesWritten
        let had = dumpFile != nil
        dumpFile = nil // 釋放 → AVAudioFile 寫回正確的 RIFF/data chunk size
        dumpLock.unlock()
        if had {
            let secs = Double(frames) / (analyzerFormat?.sampleRate ?? 16000)
            emitter.status(String(format: "--dump-audio：已寫入 %d frames（≈%.1fs），檔案已關閉。", frames, secs))
        }
    }

    /// --wav 模式：把 WAV 檔逐塊讀入 → 轉成 analyzer 格式 → 餵進同一條管線。
    /// 繞過麥克風，用於分離驗證與回歸測試。
    func runWav(path: String) async throws {
        let url = URL(fileURLWithPath: path)
        let file = try AVAudioFile(forReading: url)
        let srcFormat = file.processingFormat
        emitter.status("讀入 WAV：\(path)")
        emitter.status("WAV 格式：\(srcFormat.sampleRate)Hz, \(srcFormat.channelCount)ch, commonFormat=\(srcFormat.commonFormat.rawValue)，長度 \(file.length) frames")

        // 建轉換器（WAV 格式 → analyzer 格式）
        if !formatsEqual(srcFormat, analyzerFormat) {
            converter = AVAudioConverter(from: srcFormat, to: analyzerFormat)
            if converter == nil {
                throw SpikeError.converterFailed
            }
            emitter.status("已建立音訊轉換器：\(srcFormat.sampleRate)Hz → \(analyzerFormat.sampleRate)Hz \(analyzerFormat.commonFormat == .pcmFormatInt16 ? "Int16" : "格式\(analyzerFormat.commonFormat.rawValue)")")
        } else {
            emitter.status("WAV 格式與 analyzer 期待格式相同，免轉換。")
        }

        // 建立輸入串流與結果消費
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.inputContinuation = continuation
        let resultsTask = Task { [weak self] in
            guard let self else { return }
            await self.consumeResults()
        }

        try await analyzer.start(inputSequence: stream)
        emitter.status("analyzer 已啟動，開始逐塊餵檔案…")

        // 逐塊讀（0.1 秒一塊 @ 來源取樣率）
        let chunk = AVAudioFrameCount(srcFormat.sampleRate * 0.1)
        var chunks = 0
        while file.framePosition < file.length {
            guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: chunk) else { break }
            try file.read(into: inBuf)
            if inBuf.frameLength == 0 { break }
            guard let outBuf = convertIfNeeded(inBuf) else { break }
            feedToAnalyzer(outBuf)
            chunks += 1
        }
        emitter.status("檔案讀畢，共餵 \(chunks) 塊；finalize 收尾…")

        continuation.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        // 給 results 迴圈時間吐完最後的 final
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        resultsTask.cancel()
        emitter.status("--wav 完成。")
    }

    /// --wav-realtime 模式：讀 WAV 檔但以「真實速率」餵（每塊 chunkMs 音訊、餵後 sleep chunkMs/rtSpeed），
    /// 並啟動與 live 模式**完全相同**的週期 finalize（flushMs）＋ VAD ＋ 週期診斷（不裝 tap、不碰麥克風）。
    /// 餵完後保持 analyzer 開啟 tailWaitS 秒（可選持續餵靜音，模擬 mic 不停送、游標續進），才收尾。
    /// 目的：在無麥克風環境重現 live 的時序行為，隔離「realtime 餵入 + 週期 finalize」這組變因。
    func runWavRealtime(path: String) async throws {
        let url = URL(fileURLWithPath: path)
        let file = try AVAudioFile(forReading: url)
        let srcFormat = file.processingFormat
        let totalSec = Double(file.length) / srcFormat.sampleRate
        emitter.status("讀入 WAV（realtime）：\(path)")
        emitter.status("WAV 格式：\(srcFormat.sampleRate)Hz, \(srcFormat.channelCount)ch, commonFormat=\(srcFormat.commonFormat.rawValue)，長度 \(file.length) frames（≈\(String(format: "%.1f", totalSec))s）")
        emitter.status("realtime 參數：chunk=\(Int(opts.chunkMs))ms speed=\(opts.rtSpeed)x sleep=\(opts.rtSleep) flush=\(Int(opts.flushMs))ms VAD=\(opts.useVAD ? "on(\(Int(opts.silenceMs))ms)" : "off") tailWait=\(opts.tailWaitS)s tailSilence=\(opts.tailSilence) bufferStartTime=\(opts.useBufferStartTime) fastResults=\(opts.useFastResults)")

        // 建立輸入串流
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.inputContinuation = continuation

        // ── 沿用 live 的嚴格啟動順序鎖（不可動）：analyzer.start → results 就緒 → 才餵音訊 ──
        // (1) 先啟動 analyzer（此時尚無音訊流入）
        try await analyzer.start(inputSequence: stream)
        emitter.status("analyzer.start 已返回（SDK 內部通道就緒）")

        // (2) 建立 results 消費 task，等它確實進入 for-await 才繼續
        let resultsReady = AsyncStream<Void>.makeStream()
        // ★ 消費者提升為高優先級：對抗「live 27s 凍結」最可能的機制——結果消費 Task 在 live 進程
        //   （即時音訊執行緒 + on-device 推論的 CPU 壓力下）被協程排程餓死，導致 analyzer 已 commit
        //   的 final 遲遲交付不出來。高優先級讓消費者能搶先被排程，不因背景負載而長時間停擺。
        //   （此為針對 live 的緩解；無麥克風環境的 file-feed 本就不凍結，改動已驗證不影響其輸出。）
        let resultsTask = Task(priority: .high) { [weak self] in
            guard let self else { return }
            await self.consumeResults(readyContinuation: resultsReady.continuation)
        }
        await withTaskGroup(of: Void.self) { g in
            g.addTask { var it = resultsReady.stream.makeAsyncIterator(); _ = await it.next() }
            g.addTask { try? await Task.sleep(nanoseconds: 1_000_000_000) }
            _ = await g.next()
            g.cancelAll()
        }

        // 週期診斷 task（2s，與 live 相同）
        let diagTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { break }
                await self.emitPeriodicDiag()
            }
        }

        // 週期 finalize「安全網」task（與 live 相同機制）：VAD 抑制的安全網，見 runFlushSafetyNet。
        // --flush-ms 0 則 runFlushSafetyNet 內部自行停用（純 VAD + 收尾 finalize）。
        let flushTask = Task { [weak self] in
            guard let self else { return }
            await self.runFlushSafetyNet()
        }

        // (3) 消費者就緒後，才以真實速率餵音訊
        let chunkFrames = AVAudioFrameCount(max(srcFormat.sampleRate * opts.chunkMs / 1000.0, 1))
        let sleepNs = UInt64(max(opts.chunkMs / opts.rtSpeed, 0) * 1_000_000)
        var chunks = 0
        while file.framePosition < file.length {
            guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: chunkFrames) else { break }
            try file.read(into: inBuf)
            if inBuf.frameLength == 0 { break }
            // 語音偵測（供安全網合成兜底）＋ VAD 靜音端點（與 live handleTap 相同邏輯）
            let rms = SilenceVAD.rms(of: inBuf)
            markSpeech(rms: rms)
            if let vad = vad {
                if vad.feed(rms: rms, nowMs: MonoClock.nowMs()) {
                    emitter.endpoint(reason: "vad_silence", tWall: MonoClock.nowMs())
                    // 與 live 相同：靜音端點以最近 volatile 合成 final（真 final 隨後去重）
                    synthesizeFinal(reason: "vad", coverThroughSec: currentCursorSec())
                    Task { [weak self] in await self?.periodicFinalize() }
                }
            }
            guard let outBuf = convertIfNeeded(inBuf) else { break }
            feedToAnalyzer(outBuf)
            chunks += 1
            if opts.rtSleep && sleepNs > 0 { try? await Task.sleep(nanoseconds: sleepNs) }
        }
        emitter.status("真實速率餵畢，共餵 \(chunks) 塊；保持 analyzer 開啟 \(opts.tailWaitS)s（tailSilence=\(opts.tailSilence)）…")

        // 尾段：模擬 live 持續狀態。tailSilence 開 → 持續餵靜音塊（游標續進、週期 finalize 續跑），
        // 這才忠實對應「mic 不會停、講完後仍源源送靜音」的真實 live 情境。
        let tailChunks = Int(opts.tailWaitS * 1000.0 / max(opts.chunkMs, 1))
        for _ in 0..<max(tailChunks, 0) {
            if opts.tailSilence {
                if let sil = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: chunkFrames) {
                    sil.frameLength = chunkFrames
                    // AVAudioPCMBuffer 配置後內容未定，需清 0 才是真靜音
                    if let ch = sil.floatChannelData {
                        for c in 0..<Int(srcFormat.channelCount) {
                            memset(ch[c], 0, Int(chunkFrames) * MemoryLayout<Float>.size)
                        }
                    }
                    if let outSil = convertIfNeeded(sil) { feedToAnalyzer(outSil) }
                }
            }
            if opts.rtSleep && sleepNs > 0 {
                try? await Task.sleep(nanoseconds: sleepNs)
            } else {
                try? await Task.sleep(nanoseconds: UInt64(opts.chunkMs * 1_000_000))
            }
        }

        emitter.status("尾段結束，收尾 finalizeAndFinishThroughEndOfInput…")
        flushTask.cancel()
        diagTask.cancel()
        continuation.finish()
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        resultsTask.cancel()
        synthLock.lock(); let sc = synthCount; synthLock.unlock()
        emitter.status("--wav-realtime 完成（合成 final \(sc) 筆）。")
    }

    private func advanceCursor(by frames: AVAudioFramePosition) {
        cursorLock.lock()
        audioSampleCursor += frames
        cursorLock.unlock()
    }

    private func currentAudioTime() -> CMTime {
        cursorLock.lock()
        let cursor = audioSampleCursor
        cursorLock.unlock()
        return CMTime(value: cursor, timescale: CMTimeScale(analyzerFormat.sampleRate))
    }

    /// 消費 transcriber.results：逐筆判斷 volatile / final，計算延遲並輸出事件。
    ///
    /// 加了生命週期診斷（走 stderr）：啟動、收到第 1 筆、串流結束、catch 錯誤，
    /// 之後任何一次失敗都能立即分辨「results 迴圈到底有沒有在跑、跑到哪一步」。
    ///
    /// readyContinuation：在「即將進入 for-await」時發一次訊號，讓 run() 能等到消費者
    /// 真正就緒後才放音訊進來（避免訂閱與音訊流的 race）。
    private func consumeResults(readyContinuation: AsyncStream<Void>.Continuation? = nil) async {
        emitter.status("results 消費迴圈已啟動（開始 for-await transcriber.results）")
        var firstSeen = false
        do {
            // 取得結果序列後、進入迴圈前發出就緒訊號
            let sequence = transcriber.results
            readyContinuation?.yield(())
            readyContinuation?.finish()
            for try await result in sequence {
                if !firstSeen {
                    firstSeen = true
                    emitter.status("results：收到第 1 筆 result（消費迴圈確實在運行）")
                }
                let now = MonoClock.nowMs()
                diagLock.lock()
                resultsReceivedCount += 1
                lastResultWallMs = now
                diagLock.unlock()
                let text = String(result.text.characters)

                // 從結果的音訊時間範圍取 audio_start / audio_end（秒）
                let range = result.range
                let audioStart = range.start.seconds.isFinite ? range.start.seconds : 0
                let audioEnd = (range.start + range.duration).seconds
                let audioEndVal = audioEnd.isFinite ? audioEnd : audioStart

                // 逐筆交付診斷（--wav-realtime 預設開啟）：把「這筆 result 在牆鐘幾秒送達消費端、
                // 對應音訊哪一段、SDK 標的 resultsFinalizationTime」全印出來，才能精準定位
                // 「音訊段結束 → final 送達」的真實延遲，以及 volatile/final 交付是否被卡住。
                if opts.deliveryDiag {
                    let rft = result.resultsFinalizationTime.seconds
                    let rftDesc = rft.isFinite ? String(format: "%.2f", rft) : "n/a"
                    // 以當前音訊游標秒數當「即時前緣」，估算此段音訊講完到現在過了多久
                    cursorLock.lock()
                    let curSec = analyzerFormat != nil ? Double(audioSampleCursor) / analyzerFormat.sampleRate : 0
                    cursorLock.unlock()
                    let lag = (curSec - audioEndVal) * 1000.0
                    emitter.status(String(format: "交付：%@ @wall %.0fms | 音訊段 %.2f–%.2fs | 前緣落後 %.0fms | rft=%@s | 「%@」",
                                          result.isFinal ? "FINAL " : "volat.", now, audioStart, audioEndVal, lag, rftDesc, text))
                }

                if result.isFinal {
                    // 除錯：模擬 live「isFinal 不交付」——直接丟棄真 final，只留合成路徑（驗證用）
                    if opts.dropRealFinals {
                        emitter.status(String(format: "[--drop-real-finals] 丟棄真 final（模擬 live）：audio %.2f–%.2fs「%@」", audioStart, audioEndVal, text))
                        continue
                    }
                    // 真 isFinal 到達：先去重——若該段已由合成 final 送出（audio_end≤水位）則丟棄，
                    // 避免「合成＋真」重複同一句（file-feed 下真 final 正常交付時的關鍵防重）。
                    if shouldDropRealFinal(audioEnd: audioEndVal, nowWall: now) {
                        emitter.status(String(format: "真 final 去重丟棄（audio_end %.2f ≤ 已吐水位）：「%@」", audioEndVal, text))
                    } else {
                        // volatile→final 延遲：距同段最後一次 volatile 的時間差
                        let lv: Double? = lastVolatileWallMs.map { now - $0 }
                        // audioEnd→final 延遲：final 產出牆鐘時間 − 該段音訊結束對應的牆鐘時間。
                        // 音訊結束的牆鐘時間 = 啟動後累積播放到 audioEnd 秒的時刻，近似以
                        // now − (最新音訊游標秒 − audioEnd) 估算。此處以「結果 finalize 時間」為準較穩：
                        let ae: Double? = latencySinceAudioEnd(audioEndSec: audioEndVal, nowMs: now)
                        emitter.final(text: text, tWall: now,
                                      audioStart: audioStart, audioEnd: audioEndVal,
                                      latencySinceLastVolatile: lv, latencySinceAudioEnd: ae)
                        lastVolatileWallMs = nil
                    }
                } else {
                    lastVolatileWallMs = now
                    // 記錄最近 volatile，供 VAD 端點／安全網逾時合成 final 使用
                    noteVolatile(text: text, start: audioStart, end: audioEndVal)
                    emitter.volatile(text: text, tWall: now,
                                     audioStart: audioStart, audioEnd: audioEndVal)
                }
            }
            // 串流自然結束（analyzer finish）→ 視為一次自然端點
            emitter.status("results 串流已結束（原因：串流正常結束/analyzer finish，共收到\(firstSeen ? "≥1" : "0")筆）")
            emitter.endpoint(reason: "natural", tWall: MonoClock.nowMs())
        } catch {
            // 完整印出錯誤（含 domain/code），這是判斷 analyzer 為何靜默的關鍵線索：
            // 例如 SFSpeechError 的 audioDisordered（音訊時序錯亂）、unexpectedAudioFormat
            // （格式非預期）、incompatibleAudioFormats 都會讓 results 串流丟錯而非默默不吐。
            let ns = error as NSError
            emitter.status("results 迴圈 catch 到錯誤：domain=\(ns.domain) code=\(ns.code) desc=\(ns.localizedDescription)")
        }
    }

    /// 估算 audioEnd→final 延遲：以當前音訊游標對應的秒數為「即時音訊前緣」，
    /// final 對應的 audioEnd 落後前緣多少秒，換算成毫秒即為此段音訊講完到 final 產出的延遲。
    private func latencySinceAudioEnd(audioEndSec: Double, nowMs: Double) -> Double? {
        cursorLock.lock()
        let cursorSec = analyzerFormat != nil ? Double(audioSampleCursor) / analyzerFormat.sampleRate : 0
        cursorLock.unlock()
        guard audioEndSec.isFinite, cursorSec >= audioEndSec else { return nil }
        return (cursorSec - audioEndSec) * 1000.0
    }

    /// 收尾：停止擷取、結束輸入串流、finalize 剩餘輸入。
    ///
    /// SIGINT hang 修復：先移除 tap、停引擎、finish 串流（讓 analyzer 的輸入序列結束），
    /// 再 finalize。finalize 本身包一層超時保護——萬一底層仍卡住（例如管線從未收到
    /// 有效音訊），不讓收尾無限等待；超時就放棄 finalize，由呼叫端接手退出。
    func stop() async {
        stopRequested = true
        engine.inputNode.removeTap(onBus: 0)
        if engine.isRunning { engine.stop() }
        inputContinuation?.finish()

        // 先關 dump 檔（tap 已移除，不會再有寫入）→ 補正 WAV header size，確保檔案完整可回餵
        closeDumpFile()

        // finalize 帶 2 秒超時：先完成者勝出
        await withTaskGroup(of: Bool.self) { group in
            group.addTask { [weak self] in
                do {
                    try await self?.analyzer?.finalizeAndFinishThroughEndOfInput()
                    return true
                } catch {
                    self?.emitter.status("finalizeAndFinishThroughEndOfInput 失敗：\(error.localizedDescription)")
                    return false
                }
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                return false
            }
            _ = await group.next()
            group.cancelAll()
        }
    }
}

enum SpikeError: Error, CustomStringConvertible {
    case noAudioFormat
    case converterFailed
    var description: String {
        switch self {
        case .noAudioFormat: return "取不到相容的音訊格式（bestAvailableAudioFormat 回傳 nil）"
        case .converterFailed: return "無法建立 AVAudioConverter（來源格式無法轉成 analyzer 期待格式）"
        }
    }
}

// MARK: - --check：locale 與資產狀態、觸發下載

func runCheck(opts: Options, emitter: EventEmitter) async {
    let locale = Locale(identifier: opts.localeID)

    emitter.status("SpeechTranscriber.isAvailable = \(SpeechTranscriber.isAvailable)")

    // SpeechDetector（SDK 內建 VAD module）存在性：spike 需知道有沒有這條路可走
    emitter.status("SDK 含 SpeechDetector module（可作為 SDK 內建 VAD 的替代方案，本 spike 端點仍用自製 RMS）")

    let supported = await SpeechTranscriber.supportedLocales
    let installed = await SpeechTranscriber.installedLocales
    emitter.status("supportedLocales（共 \(supported.count) 個）：\(supported.map { $0.identifier }.sorted().joined(separator: ", "))")
    emitter.status("installedLocales（共 \(installed.count) 個）：\(installed.map { $0.identifier }.sorted().joined(separator: ", "))")

    // 目標 locale 是否受支援
    let equivalent = await SpeechTranscriber.supportedLocale(equivalentTo: locale)
    if let eq = equivalent {
        emitter.status("目標 locale \(opts.localeID) 受支援，等價 locale：\(eq.identifier)")
    } else {
        emitter.status("目標 locale \(opts.localeID) 不在 supportedLocales，可能無法轉寫。")
    }

    // 建立一個對應 transcriber 以查詢資產安裝狀態與觸發下載
    let transcriber = SpeechTranscriber(
        locale: locale,
        transcriptionOptions: [],
        reportingOptions: [.volatileResults],
        attributeOptions: [.audioTimeRange]
    )

    let status = await AssetInventory.status(forModules: [transcriber])
    emitter.status("AssetInventory.status（\(opts.localeID)）= \(describeAssetStatus(status))")

    if status == .installed {
        emitter.status("資產已安裝，無需下載。")
        return
    }

    // 未安裝 → 觸發安裝請求並顯示進度到完成
    emitter.status("資產尚未安裝，開始下載…（可能數百 MB，請耐心等候至完成）")

    // 部分 OS 版本要求先 reserve locale 才能安裝；若失敗僅記錄，續嘗試安裝請求。
    do {
        let reserved = try await AssetInventory.reserve(locale: locale)
        emitter.status("AssetInventory.reserve(\(opts.localeID)) = \(reserved)")
    } catch {
        emitter.status("reserve 失敗（可能非必要，繼續嘗試安裝）：\(error.localizedDescription)")
    }

    do {
        guard let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) else {
            emitter.status("assetInstallationRequest 回傳 nil：可能已無需安裝，或此 locale 不提供下載。")
            return
        }

        // 進度回報：另開 Task 每 500ms 印一次百分比
        let progress = request.progress
        let progressTask = Task {
            while !Task.isCancelled {
                let pct = Int((progress.fractionCompleted * 100).rounded())
                emitter.status("下載進度：\(pct)%（\(progress.completedUnitCount)/\(progress.totalUnitCount)）")
                if progress.isFinished || progress.fractionCompleted >= 1.0 { break }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }

        try await request.downloadAndInstall()
        progressTask.cancel()
        emitter.status("下載並安裝完成。")

        // 再查一次狀態確認
        let after = await AssetInventory.status(forModules: [transcriber])
        emitter.status("安裝後 AssetInventory.status = \(describeAssetStatus(after))")
    } catch {
        emitter.status("下載或安裝失敗：\(error.localizedDescription)")
        emitter.status("錯誤細節：\(error)")
    }
}

func describeAssetStatus(_ s: AssetInventory.Status) -> String {
    switch s {
    case .unsupported: return "unsupported（不支援）"
    case .supported: return "supported（支援但未安裝）"
    case .downloading: return "downloading（下載中）"
    case .installed: return "installed（已安裝）"
    @unknown default: return "unknown"
    }
}

// MARK: - 進入點

@main
struct STTSpike {
    static func main() async {
        MonoClock.bootstrap() // 先初始化單調時鐘，避免第一筆時間戳回繞
        let opts = Options.parse(CommandLine.arguments)
        let emitter = EventEmitter(pretty: opts.pretty)

        switch opts.mode {
        case .check:
            await runCheck(opts: opts, emitter: emitter)

        case .wav:
            guard let path = opts.wavPath else {
                emitter.status("--wav 需要檔案路徑")
                exit(2)
            }
            let runner = STTRunner(opts: opts, emitter: emitter)
            do {
                try await runner.setup()
                try await runner.runWav(path: path)
                exit(0)
            } catch {
                emitter.status("--wav 模式錯誤：\(error)")
                exit(1)
            }

        case .wavRealtime:
            guard let path = opts.wavPath else {
                emitter.status("--wav-realtime 需要檔案路徑")
                exit(2)
            }
            let runner = STTRunner(opts: opts, emitter: emitter)
            do {
                try await runner.setup()
                try await runner.runWavRealtime(path: path)
                exit(0)
            } catch {
                emitter.status("--wav-realtime 模式錯誤：\(error)")
                exit(1)
            }

        case .live:
            // 先確認麥克風權限
            let ok = await ensureMicPermission(emitter: emitter)
            guard ok else {
                emitter.status("因麥克風權限未取得，live 模式無法啟動。以上診斷即為此環境的權限實況。")
                exit(2)
            }

            let runner = STTRunner(opts: opts, emitter: emitter)

            // 安裝 SIGINT handler：Ctrl-C 時優雅收尾。
            //
            // SIGINT hang 修復（兩處）：
            //   1. 訊號源掛在 global queue（非 .main）——在 @main async 的 Swift 並發模型下，
            //      主執行緒被 concurrency runtime 佔用，掛 .main 的 DispatchSource 事件處理器
            //      可能永遠不觸發，導致 Ctrl-C 沒反應。global queue 不受此限。
            //   2. 收尾包硬性逾時：正常 2.5 秒內完成 stop() 後 exit(0)；萬一 stop 仍卡住，
            //      另一條計時器 3 秒後強制 exit(0)，保證「Ctrl-C 後數秒內一定退出」。
            let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
            signal(SIGINT, SIG_IGN) // 交給 DispatchSource 處理，避免預設立即終止
            sigintSource.setEventHandler {
                emitter.status("收到中斷訊號，收尾中…")
                runner.requestStop()
                // 硬性退出保底：無論 stop() 是否卡住，3 秒後一定退出
                DispatchQueue.global().asyncAfter(deadline: .now() + 3.0) {
                    emitter.status("收尾逾時，強制退出。")
                    exit(0)
                }
                Task {
                    await runner.stop()
                    try? await Task.sleep(nanoseconds: 300_000_000)
                    exit(0)
                }
            }
            sigintSource.resume()

            do {
                try await runner.setup()
                try await runner.run()
                // run() 因 stopRequested 正常退出後也收尾
                await runner.stop()
                exit(0)
            } catch {
                emitter.status("live 模式錯誤：\(error)")
                await runner.stop()
                exit(1)
            }
        }
    }
}
