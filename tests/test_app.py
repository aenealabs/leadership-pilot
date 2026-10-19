import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from leadership_pilot.app import create_app, reviewed_concerns
from leadership_pilot.config import load_settings
from leadership_pilot.db import make_engine
from leadership_pilot.llm.replay import ReplayProvider, record_extraction
from leadership_pilot.models import InterviewExtraction
from leadership_pilot.stakeholders import RecordError, StakeholderStore


def extraction_for(notes: str) -> InterviewExtraction:
    return InterviewExtraction.model_validate({
        "summary": "Director focused on ECO cycle time.",
        "pain_points": [
            {"title": "ECO status is invisible", "description": "Nobody can see where ECOs are.",
             "severity": "high", "systems": ["PLM"],
             "quotes": ["Our ECOs take three weeks and nobody can tell me where they are."]},
            {"title": "Duplicate parts", "description": "Engineers recreate parts.",
             "severity": "medium", "systems": [],
             "quotes": ["search takes forever, so we draw it again"]},  # paraphrase: not found
            {"title": "Training gaps", "description": "Inferred.", "severity": "low",
             "systems": [], "quotes": []},
        ],
        "desired_outcomes": [{"description": "ECO cycle time under 10 days",
                              "measure": "median days", "quotes": []}],
        "systems_named": ["PLM", "SAP"],
        "urgency": "high",
        "follow_up_questions": ["Who owns ECO workflow configuration today?"],
    })


class Stub:
    name = "stub"

    def extract_interview(self, notes, stakeholder):
        assert "Priya Raman" in stakeholder
        return extraction_for(notes)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_LLM_PROVIDER", "replay")
    return replace(load_settings(), db_path=str(tmp_path / "t.db"), replay_dir=tmp_path / "r")


@pytest.fixture
def engine(settings):
    return make_engine(settings.db_path)


@pytest.fixture
def client(settings, engine):
    return TestClient(create_app(settings, provider=Stub(), engine=engine))


def test_seed_loads_stakeholders_and_interviews(engine, settings):
    store = StakeholderStore(engine)
    store.seed_if_empty(settings.stakeholders_path, settings.interviews_dir, "seed")
    assert len(store.stakeholders()) == 8 and len(store.interviews()) == 8
    store.seed_if_empty(settings.stakeholders_path, settings.interviews_dir, "seed")
    assert len(store.stakeholders()) == 8  # seeding runs once


def test_stakeholder_validation(engine):
    store = StakeholderStore(engine)
    with pytest.raises(RecordError, match="name and a role"):
        store.add_stakeholder("x", "", "", "", [], "low", "low")
    with pytest.raises(RecordError, match="low, medium, or high"):
        store.add_stakeholder("x", "A", "B", "C", [], "huge", "low")


def test_pages_render(client):
    for path in ["/stakeholders", "/stakeholders/1", "/interviews", "/interviews/1",
                 "/interviews/new", "/audit"]:
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Leadership Pilot" in page.text
    assert "Influence and interest" in client.get("/stakeholders").text


def test_extract_traces_quotes_and_accept_keeps_reviewed_items(client):
    page = client.post("/interviews/1/extract", follow_redirects=True).text
    assert "Review the extraction" in page
    assert "1 of 3 pain points traced" in page
    assert "quote not found" in page and "AI inference" in page
    assert '<mark data-paths="p0">' in page

    form = {"pain:0": "on", "pain_title:0": "ECO status is invisible to managers",
            "pain_severity:0": "critical", "outcome:0": "on", "follow:0": "on"}
    refused = client.post("/interviews/1/accept", data=form).text
    assert "You changed the AI&#39;s severity" in refused  # a changed rating needs a reason
    with_severity = form | {"pain_severity_reason:0": "Blocks plant releases weekly"}
    dropped = client.post("/interviews/1/accept", data=with_severity).text
    assert "You dropped 2 items the AI extracted" in dropped  # dropping needs a reason too
    result = client.post("/interviews/1/accept", data=with_severity | {
        "drop_reason": "Duplicate parts belongs to Kenji's interview"}).text
    assert "Accepted 1 pain point, 1 outcome, and 1 follow-up" in " ".join(result.split())
    detail = client.get("/stakeholders/1").text
    assert "ECO status is invisible to managers" in detail and "Duplicate parts" not in detail
    audit = client.get("/audit").text
    assert "extraction_drafted" in audit and "1 edited by the reviewer" in audit
    assert "reason: Blocks plant releases weekly" in audit
    assert "reason: Duplicate parts belongs to Kenji" in audit and "reworded" in audit


def test_reaccept_replaces_the_reviewed_set(client):
    client.post("/interviews/1/extract")
    why = {"drop_reason": "Covered elsewhere"}
    client.post("/interviews/1/accept", data={"pain:0": "on", "pain:1": "on"} | why)
    client.post("/interviews/1/accept", data={"pain:1": "on"} | why)
    detail = client.get("/stakeholders/1").text
    assert "Duplicate parts" in detail and "ECO status is invisible" not in detail


def test_reviewed_concerns_rejects_bad_severity():
    draft = extraction_for("x")
    kept = reviewed_concerns(draft, {"pain:0": "on", "pain_severity:0": "apocalyptic"})
    assert kept[0].severity == "high" and not kept[0].edited


def test_interview_notes_are_restricted_and_denials_logged(client):
    client.cookies.set("lp_user", "stake")  # Director of Engineering: not an approver
    for path in ["/interviews", "/interviews/1", "/stakeholders/1", "/audit"]:
        response = client.get(path)
        assert response.status_code == 403 and "Restricted" in response.text, path
    assert client.post("/interviews/1/extract").status_code == 403
    assert client.get("/stakeholders").status_code == 200  # the map itself is not sensitive
    client.cookies.set("lp_user", "po")
    assert "access_denied" in client.get("/audit").text


def test_new_interview_and_validation(client):
    page = client.post("/interviews", data={"stakeholder_id": "2", "held_on": "2026-10-01",
                                            "notes": "too short"}).text
    assert "Add the interview notes" in page
    response = client.post("/interviews", data={
        "stakeholder_id": "2", "held_on": "2026-10-01",
        "notes": "Marcus wants supplier change notices with acknowledgement, and export "
                 "control checks before any drawing is shared."}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/interviews/")


def test_replay_provider_round_trip(tmp_path):
    notes = "Some interview notes that are long enough to count as notes."
    record_extraction(tmp_path, notes, extraction_for(notes))
    replayed = ReplayProvider(tmp_path).extract_interview("  " + notes + "\n", "x")
    assert replayed.pain_points[0].title == "ECO status is invisible"
    data = json.loads(next((tmp_path / "interviews").iterdir()).read_text())
    assert data["notes"] == notes


def test_every_demo_interview_has_a_recording(tmp_path, monkeypatch):
    """Offline demo: every seeded interview extracts from its recording, fully traced."""
    from leadership_pilot.trace import trace_story

    monkeypatch.setenv("LP_LLM_PROVIDER", "replay")
    s = replace(load_settings(), db_path=str(tmp_path / "d.db"))
    store = StakeholderStore(make_engine(s.db_path))
    store.seed_if_empty(s.stakeholders_path, s.interviews_dir, "seed")
    provider = ReplayProvider(s.replay_dir)
    for iv in store.interviews():
        draft = provider.extract_interview(iv.notes, "x")
        assert draft.pain_points
        for p in draft.pain_points:
            assert trace_story(iv.notes, p.quotes).status == "traced", p.title


def test_recorded_extractions_never_assume_gender():
    """The notes never state anyone's gender, so the AI's own words must not either."""
    import re

    gendered = re.compile(r"\b(she|her|hers|herself|he|him|his|himself)\b", re.I)
    settings = load_settings()
    for path in (settings.replay_dir / "interviews").glob("*.json"):
        ex = InterviewExtraction.model_validate(json.loads(path.read_text())["extraction"])
        text = " ".join([ex.summary, *ex.follow_up_questions,
                         *(p.description for p in ex.pain_points),
                         *(o.description for o in ex.desired_outcomes)])
        assert not gendered.search(text), path.name


def test_new_interview_from_a_teams_transcript(client):
    vtt = ("WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\n<v Marcus Bell>Suppliers build to the "
           "old revision because change notices go out by email.</v>\n")
    response = client.post("/interviews", data={"stakeholder_id": "2", "held_on": "2026-10-01"},
                           files={"file": ("call.vtt", vtt, "text/vtt")}, follow_redirects=True)
    assert response.status_code == 200
    assert "Marcus Bell: Suppliers build to the old revision" in response.text
    bad = client.post("/interviews", data={"stakeholder_id": "2"},
                      files={"file": ("deck.pptx", b"PK", "application/octet-stream")})
    assert "Upload a .txt, .md, .docx" in bad.text


def test_security_headers(client):
    headers = client.get("/static/css/app.css").headers
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"


SUITE = ("Backlog Pilot=http://bp, Leadership Pilot=http://lp, PLM Insight=http://pi, "
         "Future App=http://sp")


def test_launcher_menu_renders(settings, engine, monkeypatch):
    monkeypatch.setenv("MISSION_CONTROL_APPS", SUITE)
    app = create_app(settings, provider=Stub(), engine=engine)
    page = TestClient(app).get("/stakeholders").text
    assert 'class="suite-menu"' in page and 'href="http://sp"' in page
    assert 'aria-current=page' in page
