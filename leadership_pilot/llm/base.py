"""Provider interface shared by the live and replay LLM backends."""

import hashlib
from typing import Protocol

from leadership_pilot.models import InterviewExtraction, Synthesis, WeeklyUpdateDraft


class LLMError(Exception):
    """A provider could not produce a usable result; the message is user-facing."""


class LLMProvider(Protocol):
    name: str

    def extract_interview(self, notes: str, stakeholder: str) -> InterviewExtraction: ...

    def synthesize(self, input_text: str) -> Synthesis: ...

    def narrate(self, input_text: str) -> WeeklyUpdateDraft: ...


def input_key(text: str) -> str:
    """Stable key for an input, insensitive to whitespace and case differences."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]
