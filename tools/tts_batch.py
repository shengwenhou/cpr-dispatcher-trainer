"""
CPR 派遣員訓練工具 — 台詞庫批次 TTS 合成
------------------------------------------------------------
讀取 content/<locale>/adult_script.yaml，取出所有台詞（canonical / variants /
inserts / meta_phrases / faq），用 Gemini 3.1 Flash TTS（via Vertex AI）逐句
合成語音，輸出成以台詞 id 命名的 wav 檔。

設計（沿用既有簡報配音 pipeline 的核心邏輯）：
- 兩層輸出：
    assets/audio/<locale>/raw/<id>.wav   — Gemini 原始輸出（24 kHz mono LINEAR16）
    assets/audio/<locale>/<id>.wav        — 最終版（speed=1.0 時直接複製 raw；
                                             否則套用 ffmpeg atempo 後製）
- 斷點續跑：final wav 若已存在且 >10KB 就跳過該句
- 失敗自動重試（429 / 5xx 指數退避，最多 4 次）；重試後仍失敗則記錄、不中斷全體

金鑰：本檔絕不寫入任何私有路徑。認證一律透過環境變數
GOOGLE_APPLICATION_CREDENTIALS 走 Google 標準 ADC 機制（google-genai 原生支援），
執行前請自行 export 指向你的 GCP 服務帳戶金鑰 json。

用法（範例，於專案根目錄執行）：
  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-key.json
  ~/presentation-env/bin/python3 tools/tts_batch.py
  ~/presentation-env/bin/python3 tools/tts_batch.py --only s0_open_c,s7_handover_c
  ~/presentation-env/bin/python3 tools/tts_batch.py --speed 0.95
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

# ============ 專案路徑 ============
SCRIPT_DIR = Path(__file__).resolve().parent
PROJ = SCRIPT_DIR.parent

# ============ 認證檢查（絕不 hardcode 私有路徑，一律走環境變數） ============
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    sys.stderr.write(
        "錯誤：未設定環境變數 GOOGLE_APPLICATION_CREDENTIALS。\n"
        "請先執行（換成你自己的 GCP 服務帳戶金鑰路徑）：\n"
        "  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-key.json\n"
    )
    sys.exit(2)

try:
    import yaml
except ImportError:
    sys.stderr.write("錯誤：找不到 pyyaml，請先 pip install pyyaml。\n")
    sys.exit(2)

from google import genai
from google.genai import types
from google.api_core import exceptions as gax_exc

# ============ 常數 ============
MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_VOICE = "Charon"
GCP_PROJECT = "atls-tts"
GCP_LOCATION = "us-central1"
FFMPEG = "ffmpeg"

STYLE_INSTRUCTION = """請用台灣國語（繁體中文）腔調朗讀，不要有大陸普通話的翹舌音或兒化音。
你是台灣一一九的接線派遣員，正在電話中冷靜引導報案民眾對倒下的人做壓胸急救。
語氣：專業、冷靜、清楚、帶著安定人心的力量；像講電話的自然口語，不是朗讀文章。
指令句要乾脆有力，安撫句要溫和堅定。句尾自然收，不要拖長音。
「AED」念英文字母「A、E、D」。數字依上下文自然朗讀，例如「一分鐘一百到一百二十下」。
以下是要念的台詞：
"""


def load_script_items(yaml_path: Path):
    """從台詞庫 YAML 取出全部 (id, text)。

    涵蓋範圍：states.*.canonical[] / states.*.variants[] / states.s6.inserts[]
    （inserts 目前只在 s6 出現，用 .get 泛用寫法即可覆蓋其他 state 若未來新增）、
    meta_phrases.*[]、faq[]（僅取 text，intent 欄位不合成）。
    """
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    items = []

    for _sid, sval in data.get("states", {}).items():
        for c in sval.get("canonical", []):
            items.append((c["id"], c["text"]))
        for v in sval.get("variants", []):
            items.append((v["id"], v["text"]))
        for ins in sval.get("inserts", []):
            items.append((ins["id"], ins["text"]))

    for _cat, arr in data.get("meta_phrases", {}).items():
        for m in arr:
            items.append((m["id"], m["text"]))

    for f in data.get("faq", []):
        items.append((f["id"], f["text"]))

    return items


def save_wav_from_pcm(pcm: bytes, out_path: Path, sample_rate: int = 24000):
    """原始 raw PCM（16-bit mono）補上 WAV header 存檔。"""
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def apply_atempo(src: Path, dst: Path, speed: float):
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
         "-filter:a", f"atempo={speed:.4f}", str(dst)],
        check=True,
    )


def synth_one_to_raw(client, text: str, voice: str, raw_path: Path, max_retries: int = 4):
    """合成單句到 raw wav。429 / 5xx 用指數退避重試，其他例外直接拋出。"""
    prompt = STYLE_INSTRUCTION + text
    delay = 4.0
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                        )
                    ),
                ),
            )
            pcm = resp.candidates[0].content.parts[0].inline_data.data
            save_wav_from_pcm(pcm, raw_path)
            return True
        except (gax_exc.ResourceExhausted, gax_exc.ServiceUnavailable,
                gax_exc.DeadlineExceeded, gax_exc.InternalServerError) as e:
            last_err = e
            print(f"    ⚠ attempt {attempt} 失敗（{type(e).__name__}），等 {delay:.0f}s 重試")
            time.sleep(delay)
            delay *= 2
        except Exception:
            raise
    raise RuntimeError(f"重試 {max_retries} 次仍失敗：{last_err}")


def main():
    ap = argparse.ArgumentParser(description="CPR 台詞庫批次 TTS 合成")
    ap.add_argument("--locale", default="zh-TW", help="語系（預設 zh-TW）")
    ap.add_argument("--only", default=None, help="只合成指定 id（逗號分隔），用於重跑壞句")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"Gemini TTS 聲音（預設 {DEFAULT_VOICE}）")
    ap.add_argument("--speed", type=float, default=1.0, help="輸出語速，≠1.0 時用 ffmpeg atempo 後製（預設 1.0）")
    args = ap.parse_args()

    yaml_path = PROJ / "content" / args.locale / "adult_script.yaml"
    if not yaml_path.exists():
        sys.stderr.write(f"錯誤：找不到台詞庫 {yaml_path}\n")
        sys.exit(2)

    out_dir = PROJ / "assets" / "audio" / args.locale
    raw_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    items = load_script_items(yaml_path)

    only_ids = None
    if args.only:
        only_ids = {x.strip() for x in args.only.split(",") if x.strip()}
        items = [(i, t) for i, t in items if i in only_ids]
        missing = only_ids - {i for i, _ in items}
        if missing:
            print(f"⚠ 警告：--only 指定的以下 id 在台詞庫中找不到：{sorted(missing)}")

    print(f"共 {len(items)} 句，總 {sum(len(t) for _, t in items)} 字")
    print(f"模型：{MODEL}  聲音：{args.voice}  語速：{args.speed}")
    print(f"raw 輸出：{raw_dir}")
    print(f"final 輸出：{out_dir}\n")

    client = None  # 延遲初始化，只在真的需要合成時才建立

    t0 = time.time()
    synth, skipped, failed = 0, 0, []
    for item_id, text in items:
        final_path = out_dir / f"{item_id}.wav"
        raw_path = raw_dir / f"{item_id}.wav"

        if final_path.exists() and final_path.stat().st_size > 10_000:
            print(f"  ─ {item_id}  已存在（{final_path.stat().st_size // 1024} KB），跳過")
            skipped += 1
            continue

        t_seg = time.time()
        try:
            if client is None:
                client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)
            synth_one_to_raw(client, text, args.voice, raw_path)

            if abs(args.speed - 1.0) < 1e-9:
                shutil.copyfile(raw_path, final_path)
            else:
                apply_atempo(raw_path, final_path, args.speed)

            dt = time.time() - t_seg
            kb = final_path.stat().st_size // 1024
            print(f"  ✓ {item_id}  {len(text):3d} 字  →  {kb} KB  ({dt:.1f}s)")
            synth += 1
        except Exception as e:
            print(f"  ✗ {item_id}  失敗：{type(e).__name__}: {str(e)[:150]}")
            failed.append(item_id)

    total_dt = time.time() - t0
    print(f"\n完成。新合成 {synth} 句、跳過 {skipped} 句、失敗 {len(failed)} 句；總耗時 {total_dt:.0f}s")
    if failed:
        print(f"失敗的 id：{failed}")
        print("可以直接再跑一次這隻腳本（或用 --only 指定失敗清單），已完成的會自動跳過。")
        sys.exit(1)


if __name__ == "__main__":
    main()
