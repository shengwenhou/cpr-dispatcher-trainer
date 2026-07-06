"""文字模式測試 harness（cli_harness.py）。

用途：不接真 STT/TTS，用文字完整驅動 FSM，跑通一整場對話並印出 metrics 摘要。
- --text（預設）：stdin 一行＝一句 final；stdout 印「[台詞 id] 全文」當作播放。
  指令（以 / 開頭）：
    /wait <秒>   推進虛擬時鐘 N 秒並 pump tick（模擬沉默 → 觸發分級 timeout；S6 插播計時）
    /fault       模擬技術故障（層 5 tech_fault）
    /state       印出目前狀態與已填 slot（除錯）
    /dump <路徑> 把 metrics 事件流寫成 JSONL
    /quit        結束並印 metrics 摘要
  → 虛擬時鐘（--virtual-clock，預設開）讓整場決定性、可從檔案餵入、可精確控時。
- --voice：接真 Provider（SpeechAnalyzerSTT 等）。本階段能啟動即可，真語音整測留待 UI 階段。

i18n：harness 的提示文字為開發者字串（繁中直寫）；派遣員台詞一律經台詞庫 id 取得。
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

# 允許以 `python server/cli_harness.py` 直接執行（把專案根加入 sys.path）
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from server.config import load_config  # noqa: E402
from server.engine.metrics import MetricsRecorder  # noqa: E402
from server.engine.actions import SpeakAction, SpeakKind  # noqa: E402
from server.factory import (  # noqa: E402
    build_engine,
    build_layer4,
    build_llm,
    build_script,
    build_tts,
)
from server.runtime import IntentPipeline, TextModeDriver, execute_action  # noqa: E402


class VirtualClock:
    """虛擬時鐘：文字模式用，讓 metrics 時間戳與 timeout/S6 計時決定性可控。"""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _fmt_action(action: SpeakAction, text_of) -> str:
    if action.kind == SpeakKind.PRERECORDED and action.line_id:
        layer_tag = f" (層{action.layer})" if action.layer else ""
        return f"  [{action.line_id}]{layer_tag} {text_of(action.line_id)}"
    if action.kind == SpeakKind.FILLER_THEN_DYNAMIC:
        filler = f"[{action.filler_id}] {text_of(action.filler_id)}\n  " if action.filler_id else ""
        return f"  {filler}[<dynamic 層4>] {action.text}"
    if action.kind == SpeakKind.DYNAMIC:
        return f"  [<dynamic 層{action.layer}>] {action.text}"
    return f"  [?] {action}"


def print_metrics_summary(metrics: MetricsRecorder) -> None:
    s = metrics.summary()
    print("\n" + "=" * 60)
    print("Metrics 摘要（SPEC 第六節）")
    print("=" * 60)

    def fmt(v):
        return f"{v:.2f}s" if isinstance(v, (int, float)) else "—（未發生）"

    print(f"  辨識 OHCA 時間（進 S5）      ： {fmt(s['ohca_recognized_s'])}")
    print(f"  開始按壓時間（S6 首次數數）  ： {fmt(s['compression_start_s'])}")
    print(f"  EMS 抵達時間（S7）           ： {fmt(s['ems_arrived_s'])}")
    print(f"  通話結束                     ： {fmt(s['session_end_s'])}")
    print("  各狀態停留時間：")
    for st in sorted(s["state_dwell_s"].keys()):
        print(f"      {st}: {s['state_dwell_s'][st]:.2f}s")
    if s["defense_counts"]:
        print("  五層防禦觸發統計：")
        for layer, cnt in sorted(s["defense_counts"].items()):
            print(f"      層 {layer}: {cnt} 次")
    print(f"  事件總數： {s['total_events']}")
    print("=" * 60)


def run_text_mode(args) -> int:
    cfg = load_config()
    # 決定性 rng（--seed）讓變體/插播輪替可重現；未指定則隨機
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    clock = VirtualClock()

    script = build_script(cfg, rng=rng)
    metrics = MetricsRecorder(clock=clock.now)  # 用虛擬時鐘
    text_of = script.text_of

    # LLM：--no-llm 或環境不足時走降級（keyword）；文字模式預設不強制 LLM
    llm = None
    if not args.no_llm:
        llm = build_llm(cfg, script.faq_intents())
    layer4 = build_layer4(cfg, llm) if not args.no_llm else None

    engine = build_engine(cfg, script, metrics, rng=rng, layer4_generator=layer4)
    pipeline = IntentPipeline(llm=llm)

    # 文字模式 TTS：即時印出播放內容
    def on_speak(marker, text, is_dynamic):
        pass  # 實際列印在 driver 迴圈統一處理（用回傳的 actions），避免重複

    tts = build_tts(cfg, text_of, text_mode=True, on_speak=on_speak)
    driver = TextModeDriver(engine=engine, pipeline=pipeline, tts=tts, text_of=text_of)

    def emit(actions):
        for a in actions:
            print(_fmt_action(a, text_of))

    llm_status = "LLM=停用（降級：RegexFastPath＋keyword）" if llm is None else f"LLM={cfg.llm.model_id}"
    print(f"# CPR 派遣員訓練 — 文字模式 harness  ({cfg.locale}/{cfg.scenario}, {llm_status})")
    print("# 一行＝一句 final。指令：/wait <秒>  /fault  /state  /dump <路徑>  /quit\n")

    print("派遣員：")
    emit(driver.start())

    for raw in _input_lines(args):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("/"):
            cont = _handle_command(line, driver, engine, clock, metrics, emit)
            if not cont:
                break
            continue

        # 一句 final
        print(f"\n學員：{line}")
        clock.advance(args.step)  # 每句預設推進一點時間（可用 /wait 明確控制）
        print("派遣員：")
        actions = driver.feed(line, now=clock.now())
        if not actions:
            print("  （無回應／壓胸進行中）")
        else:
            emit(actions)
        if engine.finished:
            print("\n# 對話結束（S7）。")
            break

    print_metrics_summary(metrics)
    if args.dump:
        metrics.dump_jsonl(args.dump)
        print(f"# metrics 事件流已寫入：{args.dump}")
    return 0


def _handle_command(line, driver, engine, clock, metrics, emit) -> bool:
    parts = line.split()
    cmd = parts[0]
    if cmd == "/quit":
        return False
    if cmd == "/wait":
        secs = float(parts[1]) if len(parts) > 1 else 5.0
        # 分段推進以便逐級觸發 timeout / 多次插播（每 0.5s pump 一次）
        remaining = secs
        astep = 0.5
        print(f"\n（沉默 {secs:.1f}s…）")
        print("派遣員：")
        any_out = False
        while remaining > 1e-9:
            d = min(astep, remaining)
            clock.advance(d)
            actions = driver.tick(clock.now())
            if actions:
                any_out = True
                emit(actions)
            remaining -= d
        if not any_out:
            print("  （無反應）")
        return True
    if cmd == "/fault":
        print("派遣員（技術故障）：")
        emit([a for a in engine.tech_fault()])
        return True
    if cmd == "/state":
        filled = {s.value: v.value for s, v in engine.filled.items()}
        print(f"# 狀態={engine.state.value}  已填 slot={filled}  finished={engine.finished}")
        return True
    if cmd == "/dump":
        path = parts[1] if len(parts) > 1 else "sessions/harness_dump.jsonl"
        metrics.dump_jsonl(path)
        print(f"# 已寫入 {path}")
        return True
    print(f"# 未知指令：{cmd}")
    return True


def _input_lines(args):
    if args.script_file:
        with open(args.script_file, "r", encoding="utf-8") as f:
            for line in f:
                # 允許腳本檔用 # 當註解行
                if line.lstrip().startswith("#"):
                    continue
                yield line
    else:
        for line in sys.stdin:
            yield line


def run_voice_mode(args) -> int:
    """--voice：接真 Provider。本階段驗證「能啟動」；真語音整測留待 UI 階段。"""
    cfg = load_config()

    async def _main():
        from server.providers.stt_speechanalyzer import SpeechAnalyzerSTT
        from server.providers.base import STTEventType

        stt = SpeechAnalyzerSTT(
            helper_path=cfg.stt.helper_path,
            locale=cfg.stt.stt_locale,
            silence_ms=cfg.stt.silence_ms,
            flush_ms=cfg.stt.flush_ms,
            shutdown_grace_s=cfg.stt.shutdown_grace_s,
        )
        print(f"# 嘗試啟動 STT helper：{cfg.stt.helper_path}")
        try:
            await stt.start()
        except Exception as e:
            print(f"# STT 啟動失敗：{type(e).__name__}: {e}")
            print("# （語音模式需 macOS + 已編譯 stt_spike + 已登入桌面的麥克風權限。）")
            return 2
        print("# STT 已啟動。Ctrl-C 結束。以下為 final 事件（本階段僅印出，未接引擎完整整測）：")
        try:
            async for ev in stt.events():
                if ev.type == STTEventType.FINAL:
                    print(f"[final] {ev.text}")
                elif ev.type == STTEventType.ERROR:
                    print(f"[error] {ev.raw}")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            await stt.stop()
        return 0

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CPR 派遣員訓練 — 對話引擎 CLI harness")
    ap.add_argument("--voice", action="store_true", help="接真 STT/TTS Provider（本階段能啟動即可）")
    ap.add_argument("--no-llm", action="store_true", help="停用 LLM，強制走降級路徑（RegexFastPath＋keyword）")
    ap.add_argument("--script-file", default=None, help="從檔案讀輸入（每行一句／指令），取代 stdin")
    ap.add_argument("--dump", default=None, help="結束時把 metrics 事件流寫成 JSONL 到此路徑")
    ap.add_argument("--seed", type=int, default=None, help="固定亂數種子（變體/插播輪替可重現）")
    ap.add_argument("--step", type=float, default=0.5, help="每句 final 之間推進的虛擬秒數（預設 0.5）")
    args = ap.parse_args()

    if args.voice:
        return run_voice_mode(args)
    return run_text_mode(args)


if __name__ == "__main__":
    sys.exit(main())
