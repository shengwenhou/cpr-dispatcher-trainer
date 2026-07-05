#!/usr/bin/env bash
# build.sh —— 編譯 stt_spike.swift（swiftc 直編，不用 SwiftPM）
#
# 用法：
#   ./build.sh          # 編譯，產出同目錄 ./stt_spike
#
# 需求：macOS 26 + CommandLineTools（含 MacOSX26 SDK 與 Speech.framework）
# 說明：Speech / AVFoundation 由 import 自動連結；若某環境未自動連結，
#       可解除下方 EXTRA_LINK 註解手動指定 -framework。

set -euo pipefail

# 切到腳本所在目錄，確保相對輸出路徑正確
cd "$(dirname "$0")"

# CLT-only 環境的坑：裸跑 swiftc 會回報
#   "unable to load standard library for target 'arm64-apple-macosx26.0'"
# 因為它預設不會解析到 MacOSX26 SDK。用 xcrun 包一層讓它自動帶正確 SDK 最穩。
# 若某環境 xcrun 不可用，退回顯式指定 -sdk。
if command -v xcrun >/dev/null 2>&1; then
  SWIFTC=(xcrun swiftc)
  echo "使用編譯器：xcrun swiftc（自動解析 SDK）"
else
  SDK_PATH="/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk"
  if [ ! -d "$SDK_PATH" ]; then
    echo "找不到 xcrun，且 $SDK_PATH 不存在；請確認已安裝 CommandLineTools（xcode-select --install）" >&2
    exit 1
  fi
  SWIFTC=(/Library/Developer/CommandLineTools/usr/bin/swiftc -sdk "$SDK_PATH")
  echo "使用編譯器：swiftc -sdk $SDK_PATH（xcrun 不可用，改顯式指定 SDK）"
fi

"${SWIFTC[@]}" --version

# 若某環境 framework 未自動連結，改成：
#   EXTRA_LINK=(-framework Speech -framework AVFoundation)
# 預設留空。注意 macOS 內建 bash 3.2 對「空陣列 + set -u」的展開很敏感，
# 故用 ${arr[@]+"${arr[@]}"} 這種安全展開寫法。
EXTRA_LINK=()

echo "編譯 stt_spike.swift → ./stt_spike"
# -O 開最佳化（VAD RMS 迴圈受益）；-parse-as-library 配合 @main
"${SWIFTC[@]}" \
  -O \
  -parse-as-library \
  ${EXTRA_LINK[@]+"${EXTRA_LINK[@]}"} \
  -o stt_spike \
  stt_spike.swift

echo "完成：$(pwd)/stt_spike"
