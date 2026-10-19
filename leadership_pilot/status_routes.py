"""Routes for the status and risk narrator: status, RAID log, and the weekly update."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from leadership_pilot.delivery import CHANNELS, Deliverer, DeliveryError, DeliverySettings, pretty
from leadership_pilot.llm import LLMError, LLMProvider
from leadership_pilot.rules import DIMENSIONS, RULES, WARNINGS, d_label, narrative_input, q_label
from leadership_pilot.stakeholders import RecordError, StakeholderStore
from leadership_pilot.status_data import LEVELS, RAID_KINDS, StatusError, StatusStore
from leadership_pilot.suite import SuiteClient, SuiteError
from leadership_pilot.updates import (
    APPROVED,
    AUDIENCES,
    DETAILED,
    SUMMARY,
    Delivery,
    UpdateError,
    UpdateStore,
    all_issues,
    render,
    sections,
)


@dataclass
class Deps:
    render: Callable[..., HTMLResponse]
    restricted: Callable[[Request, str], HTMLResponse | None]
    actor: Callable[[Request], str]
    provider: LLMProvider
    ai_label: str
    people: StakeholderStore
    status: StatusStore
    updates: UpdateStore
    suite_client: SuiteClient
    delivery: DeliverySettings
    deliverer: Deliverer


def register(app: FastAPI, d: Deps) -> None:
    # ---------- status ----------

    def status_view(request: Request, message: str = "", notice: str = "") -> HTMLResponse:
        a = d.status.assessment()
        snap = d.status.snapshot()
        return d.render(request, "status.html", a=a, snap=snap, dims=DIMENSIONS, rules=RULES,
                        warning_rules=WARNINGS, message=message, notice=notice,
                        facts=a.fact_map if a else {}, budget_rows=len(d.status.budget()),
                        q_label=q_label, d_label=d_label)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        return status_view(request)

    @app.post("/status/refresh", response_class=HTMLResponse)
    def refresh_status(request: Request):
        if (denied := d.restricted(request, "status data refresh")) is not None:
            return denied
        try:
            data = d.suite_client.portfolio()
            d.status.save_snapshot(data, d.actor(request))
        except (SuiteError, StatusError, ValueError, KeyError) as e:
            return status_view(request, f"Could not refresh from Backlog Pilot: {e}")
        return RedirectResponse("/status", status_code=303)

    @app.post("/status/budget", response_class=HTMLResponse)
    async def import_budget(request: Request, file: UploadFile):
        if (denied := d.restricted(request, "budget import")) is not None:
            return denied
        try:
            text = (await file.read()).decode("utf-8-sig")
            n = d.status.import_budget(text, d.actor(request), file.filename or "")
        except UnicodeDecodeError:
            return status_view(request, "The budget file must be a UTF-8 CSV.")
        except StatusError as e:
            return status_view(request, str(e))
        return status_view(request, notice=f"Imported {n} budget rows.")

    # ---------- RAID ----------

    def raid_view(request: Request, item=None, message: str = "", form: dict | None = None,
                  status_code: int = 200) -> HTMLResponse:
        return d.render(request, "raid.html", items=d.status.raid(include_closed=True),
                        products=d.status.products(), kinds=RAID_KINDS, levels=LEVELS,
                        edit=item, form=form or {}, message=message, d_label=d_label,
                        status_code=status_code)

    @app.get("/raid", response_class=HTMLResponse)
    def raid_page(request: Request, edit: int | None = None):
        try:
            item = d.status.raid_item(edit) if edit else None
        except StatusError as e:
            return raid_view(request, message=str(e))
        return raid_view(request, item)

    def raid_fields(form) -> dict:
        due = str(form.get("due", "")).strip()
        try:
            due_date = date.fromisoformat(due) if due else None
        except ValueError as e:
            raise StatusError("Use a date like 2026-11-15.") from e
        return {"kind": str(form.get("kind", "")), "product": str(form.get("product", "")),
                "title": str(form.get("title", "")),
                "description": str(form.get("description", "")),
                "impact": str(form.get("impact", "")),
                "probability": str(form.get("probability", "")),
                "owner": str(form.get("owner", "")), "due": due_date,
                "confirmed": form.get("confirmed") == "on",
                "mitigation": str(form.get("mitigation", ""))}

    @app.post("/raid", response_class=HTMLResponse)
    async def save_raid(request: Request):
        if (denied := d.restricted(request, "RAID log edit")) is not None:
            return denied
        form = await request.form()
        item_id = int(str(form.get("id"))) if str(form.get("id", "")).isdigit() else None
        try:
            d.status.save_raid(d.actor(request), item_id, **raid_fields(form))
        except StatusError as e:
            item = d.status.raid_item(item_id) if item_id else None
            return raid_view(request, item, str(e), dict(form), status_code=422)
        return RedirectResponse("/raid", status_code=303)

    @app.post("/raid/{item_id}/close")
    def close_raid(request: Request, item_id: int, reason: str = Form("")):
        if (denied := d.restricted(request, "RAID log edit")) is not None:
            return denied
        try:
            d.status.close_raid(item_id, d.actor(request), reason)
        except StatusError as e:
            return raid_view(request, message=str(e))
        return RedirectResponse("/raid", status_code=303)

    # ---------- weekly updates ----------

    def updates_view(request: Request, message: str = "") -> HTMLResponse:
        return d.render(request, "updates.html", updates=d.updates.all(),
                        people=d.people.stakeholders(), audiences=AUDIENCES, message=message,
                        a=d.status.assessment(), d_label=d_label)

    @app.get("/updates", response_class=HTMLResponse)
    def updates_page(request: Request):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        return updates_view(request)

    @app.post("/updates/draft", response_class=HTMLResponse)
    def draft_update(request: Request):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        a, snap = d.status.assessment(), d.status.snapshot()
        if a is None or snap is None:
            return updates_view(request, "There is no status data yet.")
        try:
            draft = d.provider.narrate(narrative_input(a))
        except LLMError as e:
            return updates_view(request, str(e))
        u = d.updates.create(a, snap.id, draft, d.actor(request), d.ai_label)
        return RedirectResponse(f"/updates/{u.id}", status_code=303)

    @app.post("/stakeholders/{stakeholder_id}/distribution", response_class=HTMLResponse)
    def set_distribution(request: Request, stakeholder_id: int, email: str = Form(""),
                         audience: str = Form("")):
        if (denied := d.restricted(request, "distribution list")) is not None:
            return denied
        try:
            d.people.set_distribution(stakeholder_id, email, audience, d.actor(request))
        except RecordError as e:
            return updates_view(request, str(e))
        return RedirectResponse("/updates#distribution", status_code=303)

    def update_view(request: Request, update_id: int, message: str = "", notice: str = "",
                    audience: str = SUMMARY, show: int | None = None) -> HTMLResponse:
        u = d.updates.get(update_id)
        frozen = u.frozen
        issues = all_issues(u.body, u.facts, {p["key"] for p in frozen["products"]})
        products = {p["key"]: p for p in frozen["products"]}
        deliveries = d.updates.deliveries(u.id)
        shown = next((x for x in deliveries if x.id == show), None)
        return d.render(request, "update_detail.html", u=u, body=u.body, frozen=frozen,
                        products=products, facts={f["id"]: f for f in frozen["facts"]},
                        sections=sections(u.body), issues=issues, versions=d.updates.versions(u.id),
                        deliveries=deliveries, shown=shown, channels=CHANNELS,
                        audiences=AUDIENCES, audience=audience,
                        preview=render(u, audience),
                        recipients={a: d.people.recipients(a) for a in AUDIENCES},
                        delivery=d.delivery, message=message, notice=notice, dims=DIMENSIONS,
                        d_label=d_label, approved=u.status == APPROVED)

    @app.get("/updates/{update_id}", response_class=HTMLResponse)
    def update_page(request: Request, update_id: int, audience: str = SUMMARY,
                    show: int | None = None):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        try:
            return update_view(request, update_id, audience=audience if audience in AUDIENCES
                               else SUMMARY, show=show)
        except UpdateError as e:
            return updates_view(request, str(e))

    @app.post("/updates/{update_id}/edit", response_class=HTMLResponse)
    async def edit_update(request: Request, update_id: int):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        try:
            version = d.updates.edit(update_id, form, d.actor(request), form.get("reason", ""))
        except UpdateError as e:
            return update_view(request, update_id, str(e))
        return update_view(request, update_id, notice=f"Saved as version {version}.")

    @app.post("/updates/{update_id}/approve", response_class=HTMLResponse)
    def approve_update(request: Request, update_id: int):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        try:
            d.updates.approve(update_id, d.actor(request))
        except UpdateError as e:
            return update_view(request, update_id, str(e))
        return update_view(request, update_id, notice="Approved. It can now be delivered.")

    @app.post("/updates/{update_id}/deliver/{channel}", response_class=HTMLResponse)
    def deliver_update(request: Request, update_id: int, channel: str,
                       audience: str = Form(SUMMARY)):
        if (denied := d.restricted(request, "weekly updates")) is not None:
            return denied
        try:
            u = d.updates.get(update_id)
        except UpdateError as e:
            return updates_view(request, str(e))
        if u.status != APPROVED:
            return update_view(request, update_id, "Approve the update before delivering it.")
        if channel not in CHANNELS or audience not in AUDIENCES:
            return update_view(request, update_id, "Unknown channel or audience.")
        # Email goes to each audience's recipients; channel posts use the chosen version.
        targets = ([(a, [p.email for p in d.people.recipients(a)]) for a in (SUMMARY, DETAILED)]
                   if channel == "outlook" else [(audience, [])])
        targets = [(a, r) for a, r in targets if r or channel != "outlook"]
        if not targets:
            return update_view(request, update_id, "No one is on the distribution list.")
        sent = []
        for aud, recipients in targets:
            payload = d.deliverer.payload(channel, render(u, aud), recipients)
            target = ", ".join(recipients) if recipients else d.delivery.target(channel)
            record = Delivery(update_id=u.id, version=u.version, channel=channel, audience=aud,
                              target=target, mode="live" if d.delivery.live else "simulated",
                              status="simulated", payload=pretty(payload), actor=d.actor(request))
            if d.delivery.live:
                try:
                    result = d.deliverer.send(channel, payload)
                    record.status, record.message_id, record.detail = (
                        result.status, result.message_id, result.detail)
                except DeliveryError as e:
                    record.status, record.detail = "failed", str(e)
            else:
                record.detail = "simulated: nothing was sent (LP_DELIVERY_MODE=simulate)"
            sent.append(d.updates.record_delivery(record))
        failed = [x for x in sent if x.status == "failed"]
        if failed:
            return update_view(request, update_id, failed[0].detail, show=failed[0].id)
        verb = {"simulated": "Simulated", "drafted": "Saved as an Outlook draft",
                "sent": "Sent"}[sent[0].status]
        return update_view(request, update_id, show=sent[0].id,
                           notice=f"{verb}: {CHANNELS[channel]}, "
                                  + "; ".join(f"{AUDIENCES[x.audience].split(' (')[0].lower()}"
                                              f" version to {x.target}" for x in sent) + ".")
