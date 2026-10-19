"""Offline provider: serves recorded responses so demos never depend on the network."""

import json
from pathlib import Path

from leadership_pilot.llm.base import LLMError, input_key
from leadership_pilot.models import InterviewExtraction, Synthesis, WeeklyUpdateDraft


class ReplayProvider:
    name = "replay"

    def __init__(self, replay_dir: Path):
        self.replay_dir = replay_dir

    def extract_interview(self, notes: str, stakeholder: str) -> InterviewExtraction:
        path = extraction_path(self.replay_dir, notes)
        if not path.exists():
            raise LLMError("Offline demo mode only has recordings for the demo interviews. "
                           "Use one of them, or switch to live AI mode for new notes.")
        return InterviewExtraction.model_validate(json.loads(path.read_text())["extraction"])

    def synthesize(self, input_text: str) -> Synthesis:
        path = synthesis_path(self.replay_dir, input_text)
        if not path.exists():
            raise LLMError("Offline demo mode has a recorded synthesis only for the demo set: "
                           "all eight interviews accepted as extracted (use 'Accept all "
                           "extracted' on the Interviews page). Live AI mode works on any set.")
        return Synthesis.model_validate(json.loads(path.read_text())["synthesis"])

    def narrate(self, input_text: str) -> WeeklyUpdateDraft:
        path = update_path(self.replay_dir, input_text)
        if not path.exists():
            raise LLMError("Offline demo mode has a recorded weekly update only for the demo "
                           "status data as seeded. Live AI mode works on any data.")
        return WeeklyUpdateDraft.model_validate(json.loads(path.read_text())["update"])


def update_path(replay_dir: Path, input_text: str) -> Path:
    return replay_dir / "updates" / f"{input_key(input_text)}.json"


def record_update(replay_dir: Path, input_text: str, update: WeeklyUpdateDraft) -> Path:
    path = update_path(replay_dir, input_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"input": input_text, "update": update.model_dump()},
                               indent=2) + "\n")
    return path


def synthesis_path(replay_dir: Path, input_text: str) -> Path:
    return replay_dir / "synthesis" / f"{input_key(input_text)}.json"


def record_synthesis(replay_dir: Path, input_text: str, synthesis: Synthesis) -> Path:
    path = synthesis_path(replay_dir, input_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"input": input_text, "synthesis": synthesis.model_dump()},
                               indent=2) + "\n")
    return path


def extraction_path(replay_dir: Path, notes: str) -> Path:
    return replay_dir / "interviews" / f"{input_key(notes)}.json"


def record_extraction(replay_dir: Path, notes: str, extraction: InterviewExtraction) -> Path:
    path = extraction_path(replay_dir, notes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notes": notes, "extraction": extraction.model_dump()},
                               indent=2) + "\n")
    return path
