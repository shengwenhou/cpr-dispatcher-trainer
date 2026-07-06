"""工廠：依 config 組裝引擎與三 Provider。切換實作＝改 config（或環境變數），呼叫端不變。

build_session() 回傳一組就緒的 (engine, pipeline, tts, text_of, llm)；
harness 與 app 都用它，避免組裝邏輯散落。
"""
from __future__ import annotations

import random
from typing import Callable, Optional

from .config import Config, load_config
from .engine.fsm import DialogueEngine, EngineConfig
from .engine.metrics import MetricsRecorder
from .engine.script_store import ScriptStore
from .providers.base import LLMProvider, TTSProvider
from .providers.tts import PrerecordedTTS, TextTTS
from .runtime import IntentPipeline


def build_script(cfg: Config, rng: Optional[random.Random] = None) -> ScriptStore:
    return ScriptStore(cfg.script_path, rng=rng)


def build_llm(cfg: Config, faq_intents: dict[str, str]) -> Optional[LLMProvider]:
    """依設定建 LLM Provider。provider=none 或建立失敗回 None（走降級）。"""
    if cfg.llm.provider == "none":
        return None
    if cfg.llm.provider == "gemini":
        try:
            from .providers.llm_gemini import GeminiIntentClassifier

            return GeminiIntentClassifier(
                model_id=cfg.llm.model_id,
                project=cfg.llm.project,
                location=cfg.llm.location,
                faq_intents=faq_intents,
                timeout_s=cfg.llm.request_timeout_s,
            )
        except Exception:
            return None
    return None


def build_layer4(cfg: Config, llm: Optional[LLMProvider]):
    """層 4 生成器：僅在 gemini LLM 可用且啟用時建立，否則 None（引擎降級為層 2）。"""
    if not cfg.layer4.enabled or llm is None:
        return None
    try:
        from .providers.llm_gemini import GeminiIntentClassifier, GeminiLayer4Generator

        if isinstance(llm, GeminiIntentClassifier):
            return GeminiLayer4Generator(
                classifier=llm, max_chars=cfg.layer4.max_chars, log_dir=cfg.layer4.log_dir
            )
    except Exception:
        return None
    return None


def build_tts(
    cfg: Config,
    text_of: Callable[[str], str],
    text_mode: bool,
    on_speak=None,
) -> TTSProvider:
    """依設定建 TTS。text_mode 或 provider=text → TextTTS（不出聲）；否則 PrerecordedTTS。"""
    if text_mode or cfg.tts.provider == "text":
        return TextTTS(text_lookup=text_of, on_speak=on_speak)
    if cfg.tts.provider == "prerecorded":
        from .providers.tts import SayTTS

        fallback = SayTTS(voice=cfg.tts.say_voice, text_lookup=text_of)
        return PrerecordedTTS(
            audio_dir=cfg.audio_dir, locale=cfg.locale, fallback=fallback, text_lookup=text_of
        )
    # 未知 provider：安全退回文字模式
    return TextTTS(text_lookup=text_of, on_speak=on_speak)


def build_engine(
    cfg: Config,
    script: ScriptStore,
    metrics: MetricsRecorder,
    rng: Optional[random.Random] = None,
    layer4_generator=None,
) -> DialogueEngine:
    ecfg = EngineConfig(
        confidence_threshold=cfg.llm.confidence_threshold,
        s6_insert_min_s=cfg.s6.insert_min_s,
        s6_insert_max_s=cfg.s6.insert_max_s,
        timeout_l1_s=cfg.timeout.level1_s,
        timeout_l2_s=cfg.timeout.level2_s,
        layer4_enabled=cfg.layer4.enabled,
        layer4_max_chars=cfg.layer4.max_chars,
    )
    return DialogueEngine(
        script=script,
        metrics=metrics,
        config=ecfg,
        rng=rng,
        layer4_generator=layer4_generator,
    )
