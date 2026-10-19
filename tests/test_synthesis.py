import io
import json
import re
from dataclasses import replace

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

from leadership_pilot.app import create_app
from leadership_pilot.config import load_settings
from leadership_pilot.db import make_engine
from leadership_pilot.llm.replay import ReplayProvider
from leadership_pilot.models import Synthesis
from leadership_pilot.suite import SuiteClient, SuiteError, SuiteSettings
from leadership_pilot.synthesis import (
    DEFAULT_WEIGHTS,
    SynthesisStore,
    rank,
    slide_outline,
    synthesis_input,
    verify_ids,
)


def fake_synthesis() -> Synthesis:
    return Synthesis.model_validate({
        "headline": "Engineering change is the bottleneck everyone feels.",
        "themes": [
            {"title": "Engineering change is slow and opaque",
             "description": "ECOs take weeks and nobody can see where they are.",
             "concern_ids": ["IV-1.1", "IV-1.2", "IV-2.1", "IV-1.1", "IV-99.9"],
             "strategic_fit": 5, "fit_rationale": "Core to engineering productivity."},
            {"title": "A narrow problem", "description": "Only one person raised it.",
             "concern_ids": ["IV-3.1"], "strategic_fit": 2, "fit_rationale": "Peripheral."},
        ],
        "contradictions": [
            {"topic": "Speed versus control", "side_a": "Move faster.", "side_a_ids": ["IV-1.1"],
             "side_b": "Keep every check.", "side_b_ids": ["IV-2.1", "IV-42.1"],
             "why_it_matters": "The workflow redesign must pick a default."},
        ],
        "personas": [
            {"name": "design engineer", "description": "Releases parts and drawings.",
             "goals": ["Release changes fast"], "pain_points": ["ECO status is invisible"],
             "based_on": ["IV-1.1"]},
        ],
        "journeys": [
            {"persona": "design engineer", "title": "Getting a change released",
             "steps": [{"step": "Submit", "what_happens": "Fills in the ECO.",
                        "pain_ids": ["IV-1.2"]}]},
        ],
        "plan": [
            {"horizon": "30", "action": "Map the ECO workflow end to end.",
             "theme_titles": ["Engineering change is slow and opaque"]},
            {"horizon": "60", "action": "Pilot an ECO status view.", "theme_titles": []},
            {"horizon": "90", "action": "Decide the change-control default.", "theme_titles": []},
        ],
    })


class SynthStub(ReplayProvider):
    """Recorded extractions for the demo interviews; a fixed synthesis."""

    name = "replay"

    def __init__(self, replay_dir):
        super().__init__(replay_dir)
        self.inputs: list[str] = []

    def synthesize(self, input_text):
        self.inputs.append(input_text)
        return fake_synthesis()


class FakeBacklog:
    """Stands in for Backlog Pilot's suite API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer s3cret":
            return httpx.Response(401)
        if request.method == "POST" and request.url.path == "/api/inbox":
            return httpx.Response(201, json={"id": 7})
        if request.method == "POST" and request.url.path == "/api/personas/proposals":
            body = json.loads(request.content)
            if body["name"] == "existing":
                return httpx.Response(409, json={"error": "A persona with that name exists."})
            return httpx.Response(201, json={"id": 3})
        if request.url.path == "/api/inbox/7":
            return httpx.Response(200, json={"status": "drafted", "intake_key": "INT-12",
                                             "intake_path": "/intakes/12"})
        return httpx.Response(404)


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), db_path=str(tmp_path / "t.db"))


@pytest.fixture
def backlog():
    return FakeBacklog()


@pytest.fixture
def app_parts(settings, backlog):
    engine = make_engine(settings.db_path)
    suite = SuiteSettings("http://backlog", "s3cret", "http://leadership")
    provider = SynthStub(settings.replay_dir)
    app = create_app(settings, provider=provider, engine=engine, suite=suite,
                     suite_client=SuiteClient(suite, httpx.MockTransport(backlog)))
    return TestClient(app), engine, provider


@pytest.fixture
def client(app_parts):
    return app_parts[0]


def accept_demo_set(client):
    client.post("/interviews/extract-all")
    client.post("/interviews/accept-all")


def test_labels_and_input_cover_the_demo_set(client, app_parts):
    accept_demo_set(client)
    items = SynthesisStore(app_parts[1]).items()
    assert len(items) == 40
    assert items[0].label == "IV-1.1" and len({lc.label for lc in items}) == 40
    text = synthesis_input(items)
    assert text.splitlines()[0].startswith("IV-1.1 | Priya Raman, Director of Engineering")
    assert '"' not in text.splitlines()[0]  # quotes are not sent to the model


def test_rank_counts_people_not_mentions_and_flags_unknown_labels(client, app_parts):
    accept_demo_set(client)
    items = SynthesisStore(app_parts[1]).items()
    problems = rank(fake_synthesis(), items, {}, DEFAULT_WEIGHTS)
    top = problems[0]
    assert top.index == 0
    assert [lc.label for lc in top.concerns] == ["IV-1.1", "IV-1.2", "IV-2.1"]  # deduped
    assert top.unknown_ids == ["IV-99.9"]
    assert top.breadth == 2  # two people, three pain points
    assert top.fit == 5 and top.fit_set_by == "AI suggestion"
    assert 0 < problems[1].score < top.score <= 100
    # a person's fit override changes the order when fit is all that counts
    flipped = rank(fake_synthesis(), items, {0: (1, "Jordan Lee"), 1: (5, "Jordan Lee")},
                   {"breadth": 0, "severity": 0, "influence": 0, "fit": 1})
    assert [p.index for p in flipped] == [1, 0] and flipped[0].fit_set_by == "Jordan Lee"


def test_verify_ids_splits_known_and_unknown(client, app_parts):
    accept_demo_set(client)
    ok, bad = verify_ids(["IV-1.1", "IV-0.0"], SynthesisStore(app_parts[1]).items())
    assert [lc.label for lc in ok] == ["IV-1.1"] and bad == ["IV-0.0"]


def test_synthesis_needs_two_stakeholders(client):
    assert "No synthesis yet" in client.get("/synthesis").text
    assert "at least two interviews" in client.post("/synthesis/run").text


def test_synthesis_page_fit_weights_and_staleness(client, app_parts):
    accept_demo_set(client)
    response = client.post("/synthesis/run", follow_redirects=True)
    page = response.text
    assert response.status_code == 200
    assert "Engineering change is slow and opaque" in page and "PR-1.1" in page
    assert "IV-99.9 · not found" in page and "IV-42.1 · not found" in page
    assert "Speed versus control" in page and "Getting a change released" in page
    assert "Map the ECO workflow end to end." in page
    assert app_parts[2].inputs[0].count("\n") == 39

    fit_form = client.get("/synthesis").text.split('action="/synthesis/1/fit/1"')[1]
    current = int(re.search(r"<option selected>(\d)</option>", fit_form).group(1))
    new = 1 if current != 1 else 2
    refused = client.post("/synthesis/1/fit/1", data={"fit": str(new)}).text
    assert "Say why the strategic fit" in refused
    client.post("/synthesis/1/fit/1", data={"fit": str(new), "reason": "Plant priority for FY27"})
    client.post("/synthesis/weights", data={"breadth": "0", "severity": "0",
                                            "influence": "0", "fit": "4"})
    page = client.get("/synthesis").text
    assert "set by Jordan Lee" in page
    audit = client.get("/audit").text
    assert "strategic fit" in audit.lower() or "fit" in audit

    client.post("/stakeholders", data={"name": "New Person", "role": "Engineer",
                                       "business_unit": "Aerospace", "influence": "low",
                                       "interest": "low"})
    assert "changed since this synthesis" not in client.get("/synthesis").text  # no new pains


def test_send_problem_and_propose_persona(client, backlog):
    accept_demo_set(client)
    client.post("/synthesis/run")
    page = client.post("/synthesis/1/problems/0/send").text
    assert "Sent PR-1.1 to Backlog Pilot" in page
    sent = json.loads(backlog.requests[0].content)
    assert sent["source_ref"] == "PR-1.1"
    assert sent["source_url"] == "http://leadership/synthesis#PR-1.1"
    assert sent["submitted_by"] == "Jordan Lee"
    assert "Priya Raman" in sent["stakeholders"]
    assert "Priya Raman" not in sent["text"]  # quotes travel unattributed
    assert "INT-12" in client.get("/synthesis").text  # live status from Backlog Pilot

    page = client.post("/synthesis/1/personas/0/propose").text
    assert "persona library for approval" in page and "Proposed</span>" in page
    assert "no longer current" in client.post("/synthesis/9/problems/0/send").text


def test_suite_client_errors():
    backlog = FakeBacklog()
    client = SuiteClient(SuiteSettings("http://backlog", "s3cret", "http://lp"),
                         httpx.MockTransport(backlog))
    with pytest.raises(SuiteError, match="persona with that name"):
        client.propose_persona("existing", "", [], [], "me")
    wrong = SuiteClient(SuiteSettings("http://backlog", "nope", "http://lp"),
                        httpx.MockTransport(backlog))
    with pytest.raises(SuiteError, match="HTTP 401"):
        wrong.send_problem("t", "x", "PR-1.1", [], "me")
    with pytest.raises(SuiteError, match="not connected"):
        SuiteClient(SuiteSettings()).send_problem("t", "x", "PR-1.1", [], "me")
    assert SuiteClient(SuiteSettings()).inbox_status(1) is None


def test_exports(client, app_parts):
    assert client.get("/synthesis/readout.md", follow_redirects=False).status_code == 303
    accept_demo_set(client)
    client.post("/synthesis/run")
    md = client.get("/synthesis/readout.md")
    assert md.headers["content-type"].startswith("text/markdown")
    assert "# Listening tour readout" in md.text and "**First 30 days**" in md.text
    assert "Priya Raman" not in md.text  # quotes are not attributed by name
    doc = Document(io.BytesIO(client.get("/synthesis/readout.docx").content))
    assert any("Engineering change is slow" in p.text for p in doc.paragraphs)
    outline = client.get("/synthesis/outline.txt").text
    lines = outline.splitlines()
    assert lines[0] == "What I heard: listening tour readout"
    assert all(line.startswith("\t") or not line[:1].isspace() for line in lines)
    store = SynthesisStore(app_parts[1])
    assert outline == slide_outline(store.latest(), rank(store.latest().synthesis, store.items(),
                                                         {}, DEFAULT_WEIGHTS))
    assert "Markdown" in client.get("/audit").text


def test_synthesis_is_restricted(client):
    client.cookies.set("lp_user", "stake")
    for path in ["/synthesis", "/synthesis/readout.md", "/synthesis/readout.docx",
                 "/synthesis/outline.txt"]:
        assert client.get(path).status_code == 403, path
    for path in ["/synthesis/run", "/synthesis/1/problems/0/send", "/interviews/extract-all",
                 "/interviews/accept-all"]:
        assert client.post(path).status_code == 403, path


def test_demo_synthesis_is_recorded_for_the_canonical_set(client, app_parts, settings):
    """Offline demo: the recorded synthesis must match what Extract all + Accept all produce."""
    accept_demo_set(client)
    text = synthesis_input(SynthesisStore(app_parts[1]).items())
    recorded = ReplayProvider(settings.replay_dir).synthesize(text)
    labels = {line.split(" | ")[0] for line in text.splitlines()}
    cited = {x for t in recorded.themes for x in t.concern_ids}
    assert cited <= labels, cited - labels
    assert labels <= cited  # every pain point lands in a theme
    for c in recorded.contradictions:
        assert set(c.side_a_ids) | set(c.side_b_ids) <= labels
