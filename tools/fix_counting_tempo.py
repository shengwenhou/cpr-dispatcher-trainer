"""
數數段節奏後製 — 把 S6 示範數數調到壓胸標準速率（預設 110 下/分鐘）
--------------------------------------------------------------------
背景：Gemini TTS 合成的數數段（「一下、兩下、三下」「一、二、三」）節奏
約 55–75 下/分鐘，達不到 AHA 100–120 標準。本腳本把音檔切段，只對數數段
做 ffmpeg atempo 變速（不改音高），指令段維持原速，再自動驗證節奏。

設計要點：
- 輸入一律取 raw/（Gemini 原始輸出），輸出覆寫 final——冪等，重跑不會疊加變速。
- 重新批次合成（tts_batch.py）後，final 會被還原成未修正版；跑一次本腳本即恢復。
- 每句的切點用「搜尋窗內能量最低點」自動定位（頓號/逗號的停頓谷），
  數數速率由 onset 間隔自動量測，加速比 = 現速率 ÷ 目標速率。

用法（專案根目錄）：
  python3 tools/fix_counting_tempo.py [--locale zh-TW] [--target-bpm 110]
"""
import argparse
import math
import struct
import subprocess
import wave
from pathlib import Path

FFMPEG = "/opt/homebrew/bin/ffmpeg"

# 每句的後製配置：
#   cut_windows — 切點搜尋窗列表（秒），在窗內找能量谷；n 個切點把音檔切成 n+1 段
#   speed_seg   — 要變速的段序號（0-based）
#   count_win   — 原始檔中數數 onset 所在的時間區間（用來量現在的速率）
# 時間都以 raw 檔為準。
CONFIGS = {
    "s6_encourage_v03": {   # 大聲數給我聽，｜一下、兩下、三下、四下
        "cut_windows": [(1.5, 2.2)],
        "speed_seg": 1,
        "count_win": (2.0, 5.6),
    },
    "s6_start_c": {         # 現在開始壓，跟著我數，｜一下、兩下、三下，｜用力壓、不要停
        "cut_windows": [(2.2, 2.7), (5.35, 5.78)],
        "speed_seg": 1,
        "count_win": (2.5, 5.3),
    },
    "s6_start_v01": {       # 好，現在雙手用力往下壓，大聲數｜一下、兩下、三下，｜不要停下來
        "cut_windows": [(4.2, 4.55), (6.75, 7.15)],
        "speed_seg": 1,
        "count_win": (4.4, 6.5),
    },
}


def read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        x = struct.unpack(f"<{n}h", w.readframes(n))
    return sr, x


def energy_envelope(x, sr, win_s=0.020, hop_s=0.010):
    win, hop = int(win_s * sr), int(hop_s * sr)
    return [math.sqrt(sum(v * v for v in x[i:i + win]) / win)
            for i in range(0, len(x) - win, hop)], hop


def find_valley(x, sr, t0, t1):
    """在 [t0,t1] 秒內找 20ms 能量最低的位置（停頓谷），作為切點。"""
    win = int(0.020 * sr)
    hop = int(0.010 * sr)
    best_t, best_e = t0, float("inf")
    for i in range(int(t0 * sr), int(t1 * sr) - win, hop):
        e = sum(v * v for v in x[i:i + win])
        if e < best_e:
            best_e, best_t = e, i / sr
    return best_t


def onsets_in(x, sr, t0, t1):
    """量測 [t0,t1] 內的音節群 onset（能量跨閾值、最小間隔 150ms）。"""
    env, hop = energy_envelope(x, sr)
    peak = max(env)
    thr = peak * 0.12
    onsets, below = [], True
    for i, e in enumerate(env):
        t = i * hop / sr
        if below and e > thr:
            if (not onsets or t - onsets[-1] > 0.15) and t0 <= t <= t1:
                onsets.append(t)
            below = False
        elif e < thr * 0.6:
            below = True
    return onsets


def counting_bpm(onsets, gap_lo, gap_hi):
    """由 onset 間隔推算數數速率：只取落在 [gap_lo, gap_hi] 的「詞組級」間隔平均。
    雙音節詞（如「兩下」）內部第二音節的 onset 會產生更短的詞內間隔，必須排除。"""
    gaps = [b - a for a, b in zip(onsets, onsets[1:])]
    group_gaps = [g for g in gaps if gap_lo <= g <= gap_hi]
    if not group_gaps:
        return None
    return 60.0 / (sum(group_gaps) / len(group_gaps))


def process(audio_dir: Path, sent_id: str, cfg: dict, target_bpm: float) -> bool:
    raw = audio_dir / "raw" / f"{sent_id}.wav"
    final = audio_dir / f"{sent_id}.wav"
    if not raw.exists():
        print(f"  ✗ {sent_id}: 找不到 raw 檔 {raw}")
        return False

    sr, x = read_wav(raw)
    cuts = [find_valley(x, sr, lo, hi) for lo, hi in cfg["cut_windows"]]

    # 量測原速：raw 的詞組間隔落在 0.60–1.5s（40–100/min 的慢速數數）
    ons = onsets_in(x, sr, *cfg["count_win"])
    bpm_now = counting_bpm(ons, 0.60, 1.5)
    if bpm_now is None:
        print(f"  ✗ {sent_id}: 量不到數數節奏（onset 不足），跳過")
        return False
    tempo = target_bpm / bpm_now
    if tempo > 2.0:
        print(f"  ⚠ {sent_id}: 需要 {tempo:.2f}x 超過 atempo 單級上限，取 2.0x")
        tempo = 2.0
    if tempo < 1.02:
        print(f"  ─ {sent_id}: 原速已達 {bpm_now:.0f}/min，僅複製")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(raw), str(final)], check=True)
        return True

    # 組 filter：n 個切點 → n+1 段，speed_seg 那段套 atempo
    bounds = [0.0] + cuts + [None]
    parts, labels = [], []
    for i in range(len(bounds) - 1):
        rng = f"atrim={bounds[i]}" + (f":{bounds[i+1]}" if bounds[i + 1] is not None else "")
        f = f"[0]{rng},asetpts=PTS-STARTPTS"
        if i == cfg["speed_seg"]:
            f += f",atempo={tempo:.4f}"
        lab = f"[s{i}]"
        parts.append(f + lab)
        labels.append(lab)
    fc = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(raw),
                    "-filter_complex", fc, str(final)], check=True)

    # 驗證：對輸出檔在「變速後對應區間」重測數數速率
    sr2, x2 = read_wav(final)
    seg_start = bounds[cfg["speed_seg"]]
    new_start = seg_start  # 之前各段皆原速，起點不變
    seg_end = bounds[cfg["speed_seg"] + 1]
    span = ((seg_end or len(x) / sr) - seg_start) / tempo
    # 驗證：變速後預期詞組間隔 ≈ 60/target_bpm（±40%），排除同步縮小的詞內間隔
    exp = 60.0 / target_bpm
    ons2 = onsets_in(x2, sr2, new_start - 0.1, new_start + span + 0.1)
    bpm_new = counting_bpm(ons2, exp * 0.72, exp * 1.4)
    tag = f"{bpm_new:.0f}" if bpm_new else "?"
    ok = bpm_new and 100 <= bpm_new <= 120
    print(f"  {'✓' if ok else '⚠'} {sent_id}: {bpm_now:.0f} → {tag} 下/分鐘"
          f"（切點 {[f'{c:.2f}' for c in cuts]}，變速 {tempo:.3f}x）")
    return bool(ok)


def main():
    ap = argparse.ArgumentParser(description="S6 數數段節奏後製")
    ap.add_argument("--locale", default="zh-TW")
    ap.add_argument("--target-bpm", type=float, default=110.0)
    args = ap.parse_args()

    audio_dir = Path("assets/audio") / args.locale
    print(f"目標速率：{args.target_bpm:.0f} 下/分鐘（AHA 標準 100–120）")
    results = [process(audio_dir, sid, cfg, args.target_bpm) for sid, cfg in CONFIGS.items()]
    print(f"\n完成：{sum(results)}/{len(results)} 句通過節奏驗證")


if __name__ == "__main__":
    main()
