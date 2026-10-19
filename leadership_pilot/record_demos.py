"""Run the demo interviews through the live model and save responses for offline replay.

Usage: python -m leadership_pilot.record_demos [interview-file ... | synthesis | update]
  (default: every interview, then the synthesis, then the weekly update)
The synthesis is recorded for the canonical demo set: every interview extracted from its
recording and accepted as drafted, which is what 'Extract all' and 'Accept all extracted'
produce offline. The weekly update is recorded for the seeded demo status data.
Credentials come from the environment (API key, `ant auth login`, or workload identity
federation in CI).
"""

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from leadership_pilot.auth import AuthSettings
from leadership_pilot.config import Settings, load_settings
from leadership_pilot.db import make_engine
from leadership_pilot.llm import LLMError
from leadership_pilot.llm.anthropic_provider import AnthropicProvider
from leadership_pilot.llm.replay import (
    ReplayProvider,
    record_extraction,
    record_synthesis,
    record_update,
)
from leadership_pilot.rules import narrative_input
from leadership_pilot.status_data import StatusStore
from leadership_pilot.suite import SuiteSettings
from leadership_pilot.synthesis import SynthesisStore, synthesis_input


def demo_synthesis_input(settings: Settings) -> str:
    """The synthesis input for the demo set, built the way the app builds it offline."""
    from leadership_pilot.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        s = replace(settings, db_path=str(Path(tmp) / "demo.db"))
        engine = make_engine(s.db_path)
        app = create_app(s, ReplayProvider(s.replay_dir), engine, AuthSettings("demo"),
                         SuiteSettings())
        with TestClient(app) as client:
            client.post("/interviews/extract-all")
            client.post("/interviews/accept-all")
        text = synthesis_input(SynthesisStore(engine).items())
        engine.dispose()
    return text


def record_interviews(settings: Settings, provider: AnthropicProvider, wanted: set[str]) -> int:
    people = json.loads(settings.stakeholders_path.read_text())["stakeholders"]
    failures = 0
    for p in people:
        if not p.get("interview") or (wanted and p["interview"] not in wanted):
            continue
        notes = (settings.interviews_dir / p["interview"]).read_text().strip()
        try:
            draft = provider.extract_interview(
                notes, f"{p['name']}, {p['role']}, {p['business_unit']}")
        except LLMError as e:
            failures += 1
            print(f"FAIL {p['interview']}: {e}", file=sys.stderr)
            continue
        path = record_extraction(settings.replay_dir, notes, draft)
        print(f"ok   {p['interview']}: {len(draft.pain_points)} pain points, "
              f"{len(draft.desired_outcomes)} outcomes -> {path.name}")
    return failures


def record_demo_synthesis(settings: Settings, provider: AnthropicProvider) -> int:
    text = demo_synthesis_input(settings)
    try:
        result = provider.synthesize(text)
    except LLMError as e:
        print(f"FAIL synthesis: {e}", file=sys.stderr)
        return 1
    path = record_synthesis(settings.replay_dir, text, result)
    print(f"ok   synthesis: {len(text.splitlines())} pain points in, {len(result.themes)} themes, "
          f"{len(result.contradictions)} contradictions, {len(result.personas)} personas "
          f"-> {path.name}")
    return 0


def demo_update_input(settings: Settings) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(str(Path(tmp) / "demo.db"))
        store = StatusStore(engine)
        store.seed_if_empty(settings.demo_dir, "seed")
        text = narrative_input(store.assessment())
        engine.dispose()
    return text


def record_demo_update(settings: Settings, provider: AnthropicProvider) -> int:
    text = demo_update_input(settings)
    try:
        result = provider.narrate(text)
    except LLMError as e:
        print(f"FAIL update: {e}", file=sys.stderr)
        return 1
    path = record_update(settings.replay_dir, text, result)
    print(f"ok   update: {len(result.summary)} summary sentences, {len(result.products)} "
          f"products, {len(result.decisions)} decisions -> {path.name}")
    return 0


def main() -> int:
    settings = load_settings()
    provider = AnthropicProvider(settings.model)
    args = set(sys.argv[1:])
    files = args - {"synthesis", "update"}
    failures = 0
    if files or not args:
        failures += record_interviews(settings, provider, files)
    if "synthesis" in args or not args:
        failures += record_demo_synthesis(settings, provider)
    if "update" in args or not args:
        failures += record_demo_update(settings, provider)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
