import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from leadership_pilot.app import create_app
from leadership_pilot.config import load_settings
from leadership_pilot.db import make_engine
from leadership_pilot.delivery import Deliverer, DeliveryError, DeliverySettings
from leadership_pilot.llm.replay import ReplayProvider
from leadership_pilot.models import CitedSentence, WeeklyUpdateDraft
from leadership_pilot.rules import RED, BudgetRow, RaidRow, assess, narrative_input
from leadership_pilot.status_data import StatusError, StatusStore, parse_budget_csv
from leadership_pilot.suite import SuiteClient, SuiteSettings
from leadership_pilot.updates import apply_edits, check_sentence

DEMO = load_settings().demo_dir


def demo_assessment():
    snap = json.loads((DEMO / "portfolio.json").read_text())
    budget = [BudgetRow(b.product, b.month, b.plan_k, b.actual_k)
              for b in parse_budget_csv((DEMO / "budget.csv").read_text())]
    raid = [RaidRow(f"RAID-{n}", r["kind"], r["product"], r["title"], r["impact"],
                    r.get("probability", ""), r.get("owner", ""),
                    date.fromisoformat(r["due"]) if r.get("due") else None, "open",
                    r.get("confirmed", False))
            for n, r in enumerate(json.loads((DEMO / "raid.json").read_text())["items"], 1)]
    return assess(snap, budget, raid)


def fact_about(a, needle):
    return next(f.id for f in a.facts if needle in f.text)


def test_rules_compute_status_and_warnings_from_the_data():
    a = demo_assessment()
    status = {p.key: p for p in a.products}
    sap = status["plm-sap"]
    assert sap.dims["schedule"].level == RED  # three moves out of Q4 in two weeks
    assert sap.dims["budget"].level == RED  # 13% over plan
    assert status["plm"].dims["schedule"].level == RED  # 26 of 21 points = 124%
    assert status["cad"].overall == "green"
    assert status["supplier"].overall == "amber"
    kinds = [w.kind for w in a.warnings]
    assert kinds == ["slip", "burn", "orphan", "orphan", "dependency"]
    burn = a.fact(a.warnings[1].fact_ids[0]).text
    assert "$411K" in burn and "$365K" in burn and "13% over plan" in burn
    text = narrative_input(a)
    assert "overall red" in text and "F-1 |" in text


def test_rules_thresholds_move_status():
    a = demo_assessment()
    snap = json.loads((DEMO / "portfolio.json").read_text())
    sap = next(p for p in snap["products"] if p["key"] == "plm-sap")
    sap["changes"] = sap["changes"][:1]  # one move: amber, not red
    one = assess(snap, [], [])
    assert next(p for p in one.products if p.key == "plm-sap").dims["schedule"].level == "amber"
    assert not [w for w in one.warnings if w.kind == "slip"]
    assert len(a.facts) > len(one.facts)


def test_sentence_checks():
    facts = {"F-1": "PLM-SAP Integration: spent $411K against a plan of $365K, 13% over plan.",
             "F-2": "BP-104 moved from Q4 2026 to Q1 2027 on Oct 6, 2026."}
    ok = CitedSentence(text="PLM-SAP spending is 13% over plan ($411K against $365K).",
                       fact_ids=["F-1"])
    assert check_sentence(ok, facts) == []
    invented = CitedSentence(text="Spend will reach $500K by December.", fact_ids=["F-1"])
    assert "500 is not in the cited facts" in " ".join(check_sentence(invented, facts))
    wrong_fact = CitedSentence(text="BP-104 slipped to Q1 2027.", fact_ids=["F-1"])
    assert "not in the cited facts" in " ".join(check_sentence(wrong_fact, facts))
    assert check_sentence(CitedSentence(text="BP-104 slipped to Q1 2027.", fact_ids=["F-2"]),
                          facts) == []
    name = CitedSentence(text="PLM / 3DEXPERIENCE and the S/4 cutover need $411K.",
                         fact_ids=["F-1"])
    assert check_sentence(name, facts) == []  # digits inside names are not numbers
    uncited = CitedSentence(text="Things are fine.", fact_ids=[])
    assert "No citation" in check_sentence(uncited, facts)[0]
    assert "not in the data" in check_sentence(
        CitedSentence(text="x", fact_ids=["F-9"]), facts)[0]
    assert "status is computed" in " ".join(check_sentence(
        CitedSentence(text="PLM-SAP is red.", fact_ids=["F-1"]), facts))


def test_budget_csv_validation():
    with pytest.raises(StatusError, match="columns"):
        parse_budget_csv("a,b\n1,2")
    with pytest.raises(StatusError, match="line 2"):
        parse_budget_csv("product,month,plan_k,actual_k\nplm,Oct,10,")
    rows = parse_budget_csv("product,month,plan_k,actual_k\nplm,2026-10,10,\n")
    assert rows[0].actual_k is None


# ---------- app ----------

def draft_for(a) -> WeeklyUpdateDraft:
    burn = fact_about(a, "13% over plan")
    moves = [w for w in a.warnings if w.kind == "slip"][0].fact_ids
    sentence = CitedSentence
    return WeeklyUpdateDraft(
        subject="Product Engineering IT weekly update: Oct 16, 2026",
        summary=[sentence(text="PLM-SAP Integration needs attention: three features moved out "
                               "of Q4 2026 and spend is 13% over plan.",
                          fact_ids=moves + [burn])],
        products=[{"product_key": p.key, "sentences": [
            {"text": f"{p.name} is on the plan shown.", "fact_ids": p.fact_ids[:1]}]}
            for p in a.products],
        decisions=[sentence(text="Approve 2 more engineers for PLM-SAP.", fact_ids=[burn])],
        risks=[sentence(text="Spend is 13% over plan.", fact_ids=[burn])])


class Narrator(ReplayProvider):
    name = "replay"

    def narrate(self, input_text):
        assert "Facts:" in input_text
        return draft_for(demo_assessment())


class FakeServices:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "graph-token"})
        if url.endswith("/users/po@example.com/messages"):
            assert request.headers["authorization"] == "Bearer graph-token"
            return httpx.Response(201, json={"id": "AAMk-draft-1"})
        if url == "https://teams.example/webhook":
            return httpx.Response(202)
        if url == "https://hooks.slack.example/T/B/x":
            return httpx.Response(200, text="ok")
        if url == "https://slack.com/api/chat.postMessage":
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        return httpx.Response(404)


LIVE = DeliverySettings(mode="live", graph_tenant="t", graph_client_id="c", graph_secret="s",
                        outlook_sender="po@example.com", teams_webhook="https://teams.example/webhook",
                        slack_webhook="https://hooks.slack.example/T/B/x")


@pytest.fixture
def make_client(tmp_path):
    def make(delivery=None, services=None, suite_client=None):
        delivery = delivery or DeliverySettings()
        settings = replace(load_settings(), db_path=str(tmp_path / "t.db"))
        engine = make_engine(settings.db_path)
        app = create_app(settings, provider=Narrator(settings.replay_dir), engine=engine,
                         suite=SuiteSettings(), suite_client=suite_client,
                         delivery=delivery,
                         deliverer=Deliverer(delivery, httpx.MockTransport(services))
                         if services else None)
        return TestClient(app), engine
    return make


def test_status_and_raid_pages(make_client):
    client, engine = make_client()
    page = client.get("/status").text
    assert "Early warnings" in page and "3 features moved out of Q4 2026" in page
    assert "PLM-SAP Integration" in page and "rag-red" in page
    raid = client.get("/raid").text
    assert "no owner" in raid and "RAID-2" in raid
    response = client.post("/raid", data={"id": "2", "kind": "risk", "product": "data-gov",
                                          "title": "No one owns the legacy part export "
                                                   "classification backfill",
                                          "impact": "high", "probability": "medium",
                                          "owner": "Tomas Rivera", "due": "2026-11-15"},
                           follow_redirects=True)
    assert response.status_code == 200
    page = client.get("/status").text
    assert "classification backfill: no owner" not in page
    bad = client.post("/raid", data={"kind": "risk", "product": "plm", "title": "x",
                                     "impact": "high", "probability": ""})
    assert bad.status_code == 422 and "needs a probability" in bad.text
    client.post("/raid/1/close", data={"reason": "SSO shipped"})
    assert "SSO and MFA" not in StatusStore(engine).assessment().facts[3].text
    assert "raid_closed" in client.get("/audit").text


def test_budget_import_changes_status(make_client):
    client, _ = make_client()
    csv = "product,month,plan_k,actual_k\nplm-sap,2026-09,100,100\n"
    page = client.post("/status/budget", files={"file": ("b.csv", csv, "text/csv")}).text
    assert "Imported 1 budget rows" in page
    assert "spend 13% ahead" not in client.get("/status").text
    assert "columns" in client.post("/status/budget",
                                    files={"file": ("b.csv", "x,y\n1,2", "text/csv")}).text


def test_refresh_from_backlog_pilot(make_client):
    snapshot = json.loads((DEMO / "portfolio.json").read_text()) | {
        "as_of": "2026-10-23", "source": "Backlog Pilot"}

    def backlog(request):
        assert request.url.path == "/api/portfolio"
        return httpx.Response(200, json=snapshot)

    suite = SuiteSettings("http://backlog", "s3cret", "http://lp")
    client, engine = make_client(suite_client=SuiteClient(suite, httpx.MockTransport(backlog)))
    client.post("/status/refresh")
    assert StatusStore(engine).snapshot().as_of == date(2026, 10, 23)


def test_update_draft_flag_edit_approve_simulated_delivery(make_client):
    client, _ = make_client()
    response = client.post("/updates/draft", follow_redirects=True)
    page = response.text
    assert "1 sentence flagged" in page and "2 is not in the cited facts" in page
    assert 'disabled' in page.split("Approve version 1")[0].rsplit("<button", 1)[1]
    assert "Approve the update" in client.post("/updates/1/deliver/slack").text
    assert "Fix or remove the 1 flagged sentence" in client.post("/updates/1/approve").text

    page = client.post("/updates/1/edit", data={"drop:decisions.0": "on",
                                                "reason": "no headcount data"}).text
    assert "Saved as version 2" in page and "flagged" not in page.split("Review and edit")[0]
    page = client.post("/updates/1/approve").text
    assert "Approved" in page and "Simulated" in page
    assert "can no longer change" in client.post("/updates/1/edit", data={}).text

    page = client.post("/updates/1/deliver/outlook").text
    assert "Simulated: Outlook email" in page
    assert "grace.whitfield@example.com" in page and "hannah.liu@example.com" in page
    assert "kenji.watanabe" not in page  # not on the list
    page = client.post("/updates/1/deliver/slack", data={"audience": "detailed"}).text
    assert "&#34;blocks&#34;" in page or '"blocks"' in page
    audit = client.get("/audit?category=updates").text
    assert "update_approved" in audit and "update_simulated" in audit

    preview = client.get("/updates/1?audience=detailed").text
    assert "Sources" in preview and "<sup>" in preview and "13% over plan" in preview


def test_live_delivery_adapters(make_client):
    services = FakeServices()
    client, _ = make_client(delivery=LIVE, services=services)
    client.post("/updates/draft")
    client.post("/updates/1/edit", data={"drop:decisions.0": "on"})
    client.post("/updates/1/approve")
    page = client.post("/updates/1/deliver/outlook").text
    assert "Saved as an Outlook draft" in page
    messages = [r for r in services.requests if r.url.path.endswith("/messages")]
    assert len(messages) == 2  # one per audience
    body = json.loads(messages[0].content)
    assert body["body"]["contentType"] == "HTML" and body["toRecipients"]
    assert "Sent: Teams channel" in client.post("/updates/1/deliver/teams").text
    card = json.loads(services.requests[-1].content)
    assert card["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert "Sent: Slack channel" in client.post("/updates/1/deliver/slack").text


def test_slack_bot_token_errors_are_reported():
    d = Deliverer(DeliverySettings(mode="live", slack_token="xoxb", slack_channel="#eng-it"),
                  httpx.MockTransport(FakeServices()))
    with pytest.raises(DeliveryError, match="channel_not_found"):
        d.send("slack", {"text": "hi", "channel": "#eng-it"})
    with pytest.raises(DeliveryError, match="not configured"):
        Deliverer(DeliverySettings(mode="live")).send("teams", {})


def test_updates_are_restricted(make_client):
    client, _ = make_client()
    client.cookies.set("lp_user", "stake")
    for path in ["/updates", "/updates/1"]:
        assert client.get(path).status_code == 403
    for path in ["/updates/draft", "/updates/1/approve", "/updates/1/deliver/slack",
                 "/status/refresh", "/raid", "/raid/1/close"]:
        assert client.post(path).status_code == 403, path
    assert client.get("/status").status_code == 200  # status itself is not candid


def test_apply_edits_keeps_citations():
    a = demo_assessment()
    d = draft_for(a)
    edited = apply_edits(d, {"text:summary.0": "  PLM-SAP needs   attention. ",
                             "drop:risks.0": "on"})
    assert edited.summary[0].text == "PLM-SAP needs attention."
    assert edited.summary[0].fact_ids == d.summary[0].fact_ids and edited.risks == []


def test_demo_update_is_recorded_for_the_seeded_data(tmp_path):
    """Offline demo: the recorded draft must match the seeded status data and pass checks."""
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"))
    store = StatusStore(make_engine(settings.db_path))
    store.seed_if_empty(settings.demo_dir, "seed")
    a = store.assessment()
    draft = ReplayProvider(settings.replay_dir).narrate(narrative_input(a))
    from leadership_pilot.updates import all_issues
    issues = all_issues(draft, {f.id: f.text for f in a.facts}, {p.key for p in a.products})
    assert issues == {}, issues
    assert [p.product_key for p in draft.products] == [p.key for p in a.products]
