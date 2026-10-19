from leadership_pilot.config import Settings
from leadership_pilot.llm.base import LLMError, LLMProvider
from leadership_pilot.llm.replay import (
    ReplayProvider,
    record_extraction,
    record_synthesis,
    record_update,
)


class RecordingProvider:
    """Wraps a live provider and saves each response for later offline replay."""

    def __init__(self, inner: LLMProvider, settings: Settings):
        self.inner = inner
        self.settings = settings
        self.name = f"{inner.name} (recording)"

    def extract_interview(self, notes: str, stakeholder: str):
        result = self.inner.extract_interview(notes, stakeholder)
        record_extraction(self.settings.replay_dir, notes, result)
        return result

    def synthesize(self, input_text: str):
        result = self.inner.synthesize(input_text)
        record_synthesis(self.settings.replay_dir, input_text, result)
        return result

    def narrate(self, input_text: str):
        result = self.inner.narrate(input_text)
        record_update(self.settings.replay_dir, input_text, result)
        return result


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "replay":
        return ReplayProvider(settings.replay_dir)
    if settings.llm_provider in ("anthropic", "foundry"):
        if settings.llm_provider == "anthropic":
            from leadership_pilot.llm.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider(settings.model)
        else:
            from leadership_pilot.llm.foundry import foundry_provider

            provider = foundry_provider(settings.model)
        return RecordingProvider(provider, settings) if settings.record else provider
    raise ValueError(f"Unknown LP_LLM_PROVIDER: {settings.llm_provider!r}")


__all__ = ["LLMError", "LLMProvider", "build_provider"]
