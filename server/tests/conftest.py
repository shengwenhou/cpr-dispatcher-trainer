"""pytest 共用 fixtures：假時鐘、決定性引擎、腳本、以及不依賴網路的分類助手。

所有測試以真實台詞庫（content/zh-TW/adult_script.yaml）為資料來源，但完全不接真 STT/LLM/TTS：
- 時鐘用可控假時鐘 → metrics 時間戳與 timeout/S6 計時可精確斷言。
- 分類器可直接構造 IntentResult 餵進引擎（繞過 LLM），或用 KeywordFallbackClassifier。
- rng 固定種子 → 變體/插播輪替可重現。
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from server.config import load_config
from server.engine.fsm import DialogueEngine, EngineConfig
from server.engine.metrics import MetricsRecorder
from server.engine.script_store import ScriptStore


class FakeClock:
    """可控假時鐘。tests 直接 set/advance 控制引擎看到的時間。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s

    def set(self, s: float) -> None:
        self.t = s


@pytest.fixture
def script_path() -> Path:
    return load_config().script_path


@pytest.fixture
def script(script_path) -> ScriptStore:
    # 固定 rng 讓變體輪替決定性
    return ScriptStore(script_path, rng=random.Random(1234))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def metrics(clock) -> MetricsRecorder:
    return MetricsRecorder(clock=clock)


@pytest.fixture
def engine_config() -> EngineConfig:
    # 縮短 S6 插播間隔便於測試（15–20s 的行為另有專門測試覆蓋預設值）
    return EngineConfig(
        confidence_threshold=0.55,
        s6_insert_min_s=15.0,
        s6_insert_max_s=20.0,
        timeout_l1_s=5.0,
        timeout_l2_s=10.0,
        layer4_enabled=True,
        layer4_max_chars=40,
    )


@pytest.fixture
def make_engine(script, metrics, engine_config):
    """工廠：可注入 layer4_generator 與自訂 rng 建立引擎。"""

    def _make(layer4_generator=None, rng_seed=1234, config=None):
        return DialogueEngine(
            script=script,
            metrics=metrics,
            config=config or engine_config,
            rng=random.Random(rng_seed),
            layer4_generator=layer4_generator,
        )

    return _make


@pytest.fixture
def engine(make_engine) -> DialogueEngine:
    return make_engine()
