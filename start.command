#!/usr/bin/env bash
# 講師 Mac 雙擊啟動：啟 uvicorn，開瀏覽器連 localhost。
#
# 綁定位址預設僅本機（127.0.0.1）。若要讓 tailnet 內其他機器的瀏覽器連入，於啟動前設
# 環境變數 CPR_BIND_HOST=0.0.0.0（本檔不寫入任何具體機器名或內網位址）。
set -euo pipefail

# 切到本腳本所在目錄（repo 根）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 本機私有環境檔（不在 repo 內）：存在則載入。用於 GOOGLE_APPLICATION_CREDENTIALS 等
# 不得入 repo 的設定（金鑰路徑、CPR_* 覆蓋值）。維護者機器上由安裝步驟建立。
LOCAL_ENV="$HOME/.config/cpr-dispatcher-trainer/env.sh"
if [ -f "$LOCAL_ENV" ]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi

HOST="${CPR_BIND_HOST:-127.0.0.1}"
PORT="${CPR_BIND_PORT:-8000}"

# 找虛擬環境的 python
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  echo "找不到虛擬環境（.venv/）。請先建立並安裝依賴："
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# 瀏覽器一律連本機（即使綁 0.0.0.0，本機仍以 127.0.0.1 連入）
URL="http://127.0.0.1:${PORT}"
( sleep 2; command -v open >/dev/null 2>&1 && open "$URL" ) &

echo "啟動 CPR 派遣員訓練後端： http://${HOST}:${PORT}  （本機開 ${URL}）"
exec "$PY" -m uvicorn server.app:app --host "$HOST" --port "$PORT"
