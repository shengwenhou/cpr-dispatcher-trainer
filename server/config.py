"""集中設定：Provider 選擇、模型 id、helper 路徑、locale 預設。

設計紀律（SPEC 八之一 i18n）：
- 程式碼禁止 hardcode zh-TW 字串；預設 locale 走設定值，可被環境變數覆蓋。
- 切換 Provider 實作＝改本檔一個欄位（或設對應環境變數），不動引擎與其他模組。
- 私有絕對路徑／金鑰內容一律不寫進本檔。STT helper 路徑預設為 repo 內相對路徑；
  認證走標準環境變數 GOOGLE_APPLICATION_CREDENTIALS（本檔不引用其值）。

本檔為 public repo 內容，任何欄位都不得含機器名、內網位址或金鑰。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── 專案根目錄（server/ 的上一層）────────────────────────────────
PROJ_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class STTConfig:
    """STT Provider 設定。

    provider: 選哪個實作（"speechanalyzer" 走 spike binary；"text" 為文字模式假 STT）。
    helper_path: spike/stt_spike binary 路徑（可執行檔，非 .swift 原始碼）。
    locale: 傳給 helper 的 --locale（SDK 用 zh_TW 底線格式）。
    silence_ms / flush_ms: 傳給 helper 的 VAD 靜音門檻與週期 flush 間隔（見 spike README）。
    """

    provider: str = field(default_factory=lambda: _env("CPR_STT_PROVIDER", "speechanalyzer"))
    helper_path: Path = field(
        default_factory=lambda: Path(
            _env("CPR_STT_HELPER", str(PROJ_ROOT / "spike" / "stt_spike"))
        )
    )
    # SDK locale 用底線格式（zh_TW）；與台詞庫／資產的連字號 locale（zh-TW）分開，避免混用。
    stt_locale: str = field(default_factory=lambda: _env("CPR_STT_LOCALE", "zh_TW"))
    silence_ms: int = field(default_factory=lambda: _env_int("CPR_STT_SILENCE_MS", 600))
    flush_ms: int = field(default_factory=lambda: _env_int("CPR_STT_FLUSH_MS", 700))
    # 收到 SIGINT 後等 helper 自行收尾的秒數（spike 內建 3s 硬退出，這裡給 5s 裕度後才 SIGKILL）
    shutdown_grace_s: float = 5.0


@dataclass
class LLMConfig:
    """LLM Provider 設定（意圖分類）。

    provider: "gemini" 走 Vertex AI；"none" 為停用（純降級路徑，只靠 RegexFastPath＋關鍵字）。
    model_id: 意圖分類模型 id（設定值，預設 flash-lite 級；執行時可列出可用模型確認）。
    project / location: GCP 專案與區域（與 TTS 批次共用同一 service account）。
    認證：走環境變數 GOOGLE_APPLICATION_CREDENTIALS（google-genai 原生 ADC），本檔不引用金鑰內容。
    """

    provider: str = field(default_factory=lambda: _env("CPR_LLM_PROVIDER", "gemini"))
    model_id: str = field(
        default_factory=lambda: _env("CPR_LLM_MODEL", "gemini-2.5-flash-lite")
    )
    project: str = field(default_factory=lambda: _env("CPR_GCP_PROJECT", "atls-tts"))
    location: str = field(default_factory=lambda: _env("CPR_GCP_LOCATION", "us-central1"))
    # 意圖分類逾時（秒）：超過即視為 LLM 不可用，走該狀態澄清句（層 5 精神）
    request_timeout_s: float = field(
        default_factory=lambda: float(_env("CPR_LLM_TIMEOUT_S", "6"))
    )
    # 信心門檻：低於此值 FSM 不前進、播澄清句（SPEC「LLM 信心不足→不前進」）
    confidence_threshold: float = field(
        default_factory=lambda: float(_env("CPR_LLM_CONF_THRESHOLD", "0.55"))
    )


@dataclass
class TTSConfig:
    """TTS Provider 設定。

    provider: "prerecorded" 走預錄 wav（afplay）；"text" 為文字模式（印 id＋全文，不出聲）。
    audio_root: 預錄音檔根目錄；實際檔案在 <audio_root>/<locale>/<id>.wav。
    fallback: speak_dynamic（層 4 即時生成）的後備 TTS；"say" 走 macOS say -v <voice>。
    """

    provider: str = field(default_factory=lambda: _env("CPR_TTS_PROVIDER", "prerecorded"))
    audio_root: Path = field(
        default_factory=lambda: Path(_env("CPR_AUDIO_ROOT", str(PROJ_ROOT / "assets" / "audio")))
    )
    fallback_provider: str = field(default_factory=lambda: _env("CPR_TTS_FALLBACK", "say"))
    say_voice: str = field(default_factory=lambda: _env("CPR_SAY_VOICE", "Meijia"))


@dataclass
class Layer4Config:
    """層 4 受約束即時生成設定。"""

    enabled: bool = field(
        default_factory=lambda: _env("CPR_LAYER4_ENABLED", "1") not in ("0", "false", "False")
    )
    max_chars: int = 40  # 生成上限字數（SPEC 層 4 限字數）
    # 每次生成留存待課後審核的目錄（開發者產物，不入 repo；見 .gitignore logs/）
    log_dir: Path = field(default_factory=lambda: PROJ_ROOT / "logs" / "layer4")


@dataclass
class TimeoutConfig:
    """層 5 分級 timeout（沉默 reprompt）。"""

    level1_s: float = 5.0   # 第一級：5s 沉默 → timeout_l1 台詞
    level2_s: float = 10.0  # 第二級：10s 沉默 → timeout_l2 台詞


@dataclass
class S5Config:
    """S5 擺位逐步引導設定。"""

    # 沉默 auto-advance：每步播完逾此秒數未收到回應 → 視為學員正在做動作、自動播下一步。
    # 這取代 S5 的沉默 timeout reprompt（S5 不問「你還在嗎」，改為持續往前帶）。
    autoadvance_s: float = field(default_factory=lambda: float(_env("CPR_S5_AUTOADVANCE_S", "4")))


@dataclass
class S6Config:
    """S6 壓胸階段設定。"""

    insert_min_s: float = 15.0  # 插播計時器下限（SPEC 15–20 秒）
    insert_max_s: float = 20.0  # 插播計時器上限


@dataclass
class ServerConfig:
    """Web／WebSocket 伺服器與課堂執行期設定（課堂模式階段新增）。

    bind_host / bind_port：uvicorn 綁定位址與埠。預設僅本機（127.0.0.1）；設 0.0.0.0 可讓
        tailnet 內其他機器的瀏覽器連入。**本檔不寫入任何具體機器名或內網位址**——欲對外綁定
        由使用者自行以環境變數覆蓋（start.command 會讀取）。
    web_dir：前端靜態檔目錄（vanilla 單頁，另由前端 worker 產出）；不存在也不影響 server 啟動。
    data_root：課堂資料落地根目錄（JSONL＋manifest）；預設 repo 內 data/（.gitignore 收錄）。
    echo_tail_ms：發聲窗尾端緩衝——afplay 結束後仍視為「系統發聲中」的毫秒數，濾殘響 echo。
    echo_similarity_threshold：非 S6 情境「疑似 echo」的文字相似度門檻（0–1，雙保險用）。
    tick_interval_ms：VoiceDriver 週期 tick 間隔（驅動 S5 auto-advance／S6 插播／沉默 timeout）。
    """

    bind_host: str = field(default_factory=lambda: _env("CPR_BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: _env_int("CPR_BIND_PORT", 8000))
    web_dir: Path = field(
        default_factory=lambda: Path(_env("CPR_WEB_DIR", str(PROJ_ROOT / "web")))
    )
    data_root: Path = field(
        default_factory=lambda: Path(_env("CPR_DATA_ROOT", str(PROJ_ROOT / "data")))
    )
    echo_tail_ms: int = field(default_factory=lambda: _env_int("CPR_ECHO_TAIL_MS", 400))
    echo_similarity_threshold: float = field(
        default_factory=lambda: float(_env("CPR_ECHO_SIM_THRESHOLD", "0.75"))
    )
    tick_interval_ms: int = field(default_factory=lambda: _env_int("CPR_TICK_MS", 250))


@dataclass
class Config:
    """整體設定聚合。locale 與 scenario 為引擎建構參數（SPEC 八之一：locale 參數化）。"""

    # 資產／台詞庫 locale 用連字號格式（zh-TW），對應 content/<locale>/ 與 assets/audio/<locale>/
    locale: str = field(default_factory=lambda: _env("CPR_LOCALE", "zh-TW"))
    scenario: str = field(default_factory=lambda: _env("CPR_SCENARIO", "adult"))

    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    layer4: Layer4Config = field(default_factory=Layer4Config)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    s5: S5Config = field(default_factory=S5Config)
    s6: S6Config = field(default_factory=S6Config)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def script_path(self) -> Path:
        """台詞庫 YAML 路徑：content/<locale>/<scenario>_script.yaml。"""
        return PROJ_ROOT / "content" / self.locale / f"{self.scenario}_script.yaml"

    @property
    def audio_dir(self) -> Path:
        """本 locale 的音檔目錄。"""
        return self.tts.audio_root / self.locale


def load_config() -> Config:
    """建立設定實例。目前全走 dataclass 預設＋環境變數覆蓋；未來可擴充讀 settings.yaml。"""
    return Config()
