"""FastAPI web app: stakeholder map, listening-tour interviews, and traced extraction."""

import json
import re
from datetime import date
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from mission_control import STATIC_PATH, install, install_help
from sqlalchemy.engine import Engine
from sqlmodel import Session

from leadership_pilot import audit_search, status_routes
from leadership_pilot.auth import (
    AuthSettings,
    User,
    load_auth_settings,
    user_from_claims,
    user_from_session,
    user_to_session,
)
from leadership_pilot.config import PACKAGE_DIR, Settings, load_settings
from leadership_pilot.db import audit, make_engine
from leadership_pilot.delivery import Deliverer, DeliverySettings, load_delivery_settings
from leadership_pilot.help_reference import reference
from leadership_pilot.llm import LLMError, LLMProvider, build_provider
from leadership_pilot.models import InterviewExtraction
from leadership_pilot.stakeholders import (
    FOLLOW_UP,
    LEVELS,
    OUTCOME,
    PAIN,
    Concern,
    RecordError,
    StakeholderStore,
)
from leadership_pilot.status_data import StatusStore
from leadership_pilot.suite import (
    SuiteClient,
    SuiteError,
    SuiteSettings,
    load_suite_settings,
)
from leadership_pilot.synthesis import (
    DEFAULT_WEIGHTS,
    SynthesisStore,
    problem_key,
    rank,
    readout_docx,
    readout_markdown,
    slide_outline,
    synthesis_input,
    verify_ids,
)
from leadership_pilot.trace import coverage, highlight, trace_story
from leadership_pilot.transcripts import TranscriptError, notes_from_file
from leadership_pilot.updates import UpdateStore

USER_COOKIE = "lp_user"
# Each screen's help button opens the guide section about it (checked at start-up).
PAGE_HELP = {
    "stakeholders.html": "stakeholders#the-map",
    "stakeholder_detail.html": "stakeholders#a-stakeholder-s-record",
    "interviews.html": "interviews#adding-an-interview",
    "interview_new.html": "interviews#adding-an-interview",
    "interview_detail.html": "interviews#reviewing-the-extraction",
    "synthesis.html": "synthesis#the-ranking",
    "status.html": "status#how-status-is-computed",
    "raid.html": "status#inputs",
    "updates.html": "updates#what-is-checked",
    "update_detail.html": "updates#editing-and-approval",
    "audit.html": "audit#searching",
    "restricted.html": "overview#who-does-what",
}
SEVERITIES = ("low", "medium", "high", "critical")


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _highlight(value: str, terms: list[str]) -> Markup:
    """HTML-escape text, then mark case-insensitive matches of the search words."""
    escaped = str(escape(value))
    if not terms:
        return Markup(escaped)
    pattern = re.compile("|".join(re.escape(str(escape(t))) for t in terms), re.IGNORECASE)
    return Markup(pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped))


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def reviewed_concerns(draft: InterviewExtraction, form: dict[str, str]) -> list[Concern]:
    """The concerns the reviewer kept, with their edits. Form fields: `pain:<i>` (kept),
    `pain_title:<i>`, `pain_severity:<i>`, `outcome:<i>`, `follow:<i>`."""
    kept: list[Concern] = []
    for i, p in enumerate(draft.pain_points):
        if f"pain:{i}" not in form:
            continue
        title = form.get(f"pain_title:{i}", p.title).strip() or p.title
        severity = form.get(f"pain_severity:{i}", p.severity)
        severity = severity if severity in SEVERITIES else p.severity
        kept.append(Concern(kind=PAIN, title=title, description=p.description,
                            severity=severity, systems=json.dumps(p.systems),
                            quotes=json.dumps(p.quotes),
                            edited=(title != p.title or severity != p.severity),
                            interview_id=0, stakeholder_id=0, accepted_by=""))
    for i, o in enumerate(draft.desired_outcomes):
        if f"outcome:{i}" in form:
            kept.append(Concern(kind=OUTCOME, title=o.description, description=o.measure,
                                quotes=json.dumps(o.quotes), interview_id=0,
                                stakeholder_id=0, accepted_by=""))
    for i, q in enumerate(draft.follow_up_questions):
        if f"follow:{i}" in form:
            kept.append(Concern(kind=FOLLOW_UP, title=q, interview_id=0, stakeholder_id=0,
                                accepted_by=""))
    return kept


def review_changes(draft: InterviewExtraction, form: dict[str, str]) -> list[str]:
    """Audit notes for what the reviewer changed in the AI's extraction.

    Rewording is free (the AI's wording is recorded beside it), but a changed severity needs
    its own reason, since severity feeds the problem ranking, and dropping anything the AI
    extracted needs `drop_reason`. Raises ValueError when a reason is missing.
    """
    notes, missing, dropped = [], [], []
    for i, p in enumerate(draft.pain_points):
        if f"pain:{i}" not in form:
            dropped.append(f"pain point '{p.title}'")
            continue
        title = form.get(f"pain_title:{i}", p.title).strip() or p.title
        if title != p.title:
            notes.append(f"reworded '{p.title}' -> '{title}'")
        new = form.get(f"pain_severity:{i}", p.severity)
        if new not in SEVERITIES or new == p.severity:
            continue
        reason = form.get(f"pain_severity_reason:{i}", "").strip()
        if not reason:
            missing.append(p.title)
        notes.append(f"'{p.title}' severity {p.severity} -> {new} (reason: {reason})")
    dropped += [f"outcome '{o.description}'" for i, o in enumerate(draft.desired_outcomes)
                if f"outcome:{i}" not in form]
    dropped += [f"follow-up '{q}'" for i, q in enumerate(draft.follow_up_questions)
                if f"follow:{i}" not in form]
    if missing:
        raise ValueError("You changed the AI's severity for " + ", ".join(
            f"'{t}'" for t in missing) + ". Say why, since severity feeds the ranking.")
    reason = form.get("drop_reason", "").strip()
    if dropped and not reason:
        raise ValueError(f"You dropped {len(dropped)} item{'' if len(dropped) == 1 else 's'} "
                         "the AI extracted (" + "; ".join(dropped[:3])
                         + ("; ..." if len(dropped) > 3 else "") + "). Say why under 'Why you "
                         "dropped items', since they came from what the stakeholder said.")
    if dropped:
        notes.append("dropped " + "; ".join(dropped) + f" (reason: {reason})")
    return notes


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None,
               engine: Engine | None = None, auth: AuthSettings | None = None,
               suite: SuiteSettings | None = None, suite_client: SuiteClient | None = None,
               delivery: DeliverySettings | None = None, deliverer: Deliverer | None = None
               ) -> FastAPI:
    settings = settings or load_settings()
    suite = suite or load_suite_settings()
    suite_client = suite_client or SuiteClient(suite)
    provider = provider or build_provider(settings)
    engine = engine or make_engine(settings.db_path)
    auth = auth or load_auth_settings()
    store = StakeholderStore(engine)
    synth = SynthesisStore(engine)
    store.seed_if_empty(settings.stakeholders_path, settings.interviews_dir,
                        "Listening tour (seed)")
    status = StatusStore(engine)
    status.seed_if_empty(settings.demo_dir, "Status data (demo seed)")
    delivery = delivery or load_delivery_settings()
    deliverer = deliverer or Deliverer(delivery)
    users = [User(u["id"], u["name"], u["role"], u.get("approver", False))
             for u in json.loads(settings.users_path.read_text())["users"]]
    ai_label = ("AI draft (recorded demo)" if provider.name == "replay"
                else f"AI draft ({settings.model})")

    app = FastAPI(title="Leadership Pilot")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=()")
        return response

    if auth.mode == "oidc":
        _enable_oidc(app, auth)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.globals.update(provider_name=provider.name, users=users, auth_mode=auth.mode,
                                 severities=SEVERITIES, levels=LEVELS,
                                 backlog_url=suite.backlog_url,
                                 suite_connected=suite.connected,
                                 delivery_live=delivery.live)
    install(app, templates, "Leadership Pilot", [("Backlog Pilot", suite.backlog_url)])
    templates.env.filters.update(
        plural=plural, highlight_terms=_highlight, from_json=json.loads,
        dict_without=lambda params, key: {k: v for k, v in params.items() if k != key})

    def current_user(request: Request) -> User:
        if auth.mode == "oidc":
            user = user_from_session(request.session.get("user"))
            return user or User("anonymous", "Signed out", "", False)
        wanted = request.cookies.get(USER_COOKIE)
        return next((u for u in users if u.id == wanted), users[0])

    def actor(request: Request) -> str:
        return current_user(request).actor

    def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"nav": name, "user": current_user(request), **context},
            status_code=status_code)

    install_help(app, templates, render, PACKAGE_DIR / "help", reference=reference,
                 page_help=PAGE_HELP)

    def restricted(request: Request, what: str) -> HTMLResponse | None:
        """Interview notes are candid: only the tour owner and approvers may see them."""
        if current_user(request).approver:
            return None
        with Session(engine) as s:
            audit(s, actor(request), "access_denied", what)
            s.commit()
        return render(request, "restricted.html", status_code=403, what=what)

    # ---------- stakeholders ----------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return RedirectResponse("/stakeholders", status_code=303)

    @app.get("/stakeholders", response_class=HTMLResponse)
    def stakeholder_map(request: Request, message: str = ""):
        people = store.stakeholders()
        interviews = store.interviews()
        status = {sh.id: [i for i in interviews if i.stakeholder_id == sh.id] for sh in people}
        return render(request, "stakeholders.html", people=people, status=status,
                      counts=store.counts(), message=message)

    @app.post("/stakeholders", response_class=HTMLResponse)
    def add_stakeholder(request: Request, name: str = Form(""), role: str = Form(""),
                        business_unit: str = Form(""), products: str = Form(""),
                        influence: str = Form("medium"), interest: str = Form("medium")):
        if (denied := restricted(request, "add a stakeholder")) is not None:
            return denied
        try:
            store.add_stakeholder(actor(request), name, role, business_unit,
                                  [p.strip() for p in products.split(",") if p.strip()],
                                  influence, interest)
        except RecordError as e:
            return stakeholder_map(request, message=str(e))
        return RedirectResponse("/stakeholders", status_code=303)

    @app.get("/stakeholders/{stakeholder_id}", response_class=HTMLResponse)
    def stakeholder_detail(request: Request, stakeholder_id: int):
        sh = store.stakeholder(stakeholder_id)
        if (denied := restricted(request, f"{sh.key} interviews and concerns")) is not None:
            return denied
        return render(request, "stakeholder_detail.html", sh=sh,
                      interviews=store.interviews(stakeholder_id),
                      concerns=store.concerns(stakeholder_id=stakeholder_id))

    # ---------- interviews ----------

    @app.get("/interviews", response_class=HTMLResponse)
    def interview_list(request: Request):
        if (denied := restricted(request, "interview list")) is not None:
            return denied
        names = {sh.id: sh for sh in store.stakeholders()}
        return render(request, "interviews.html", interviews=store.interviews(), people=names,
                      counts=store.counts())

    @app.get("/interviews/new", response_class=HTMLResponse)
    def new_interview(request: Request, stakeholder: int | None = None, message: str = ""):
        if (denied := restricted(request, "new interview")) is not None:
            return denied
        return render(request, "interview_new.html", people=store.stakeholders(),
                      selected=stakeholder, message=message, today=date.today())

    @app.post("/interviews", response_class=HTMLResponse)
    async def create_interview(request: Request, stakeholder_id: int = Form(0),
                               held_on: str = Form(""), notes: str = Form(""),
                               file: UploadFile | None = None):
        if (denied := restricted(request, "new interview")) is not None:
            return denied
        if file is not None and file.filename:
            try:
                uploaded = notes_from_file(file.filename, await file.read())
            except TranscriptError as e:
                return new_interview(request, stakeholder_id or None, str(e))
            notes = f"{notes.strip()}\n\n{uploaded}" if notes.strip() else uploaded
        try:
            iv = store.add_interview(actor(request), stakeholder_id,
                                     _parse_date(held_on) or date.today(), notes)
        except RecordError as e:
            return new_interview(request, stakeholder_id or None, str(e))
        return RedirectResponse(f"/interviews/{iv.id}", status_code=303)

    def interview_page(request: Request, interview_id: int, message: str = "",
                       accepted_now: bool = False) -> HTMLResponse:
        iv = store.interview(interview_id)
        sh = store.stakeholder(iv.stakeholder_id)
        draft = iv.draft
        traces, segments, cov = {}, None, None
        if draft:
            quotes = {f"p{i}": p.quotes for i, p in enumerate(draft.pain_points)}
            quotes |= {f"o{i}": o.quotes for i, o in enumerate(draft.desired_outcomes)}
            traces = {k: trace_story(iv.notes, q) for k, q in quotes.items()}
            segments = highlight(iv.notes, quotes)
            cov = coverage({k: t for k, t in traces.items() if k.startswith("p")})
        concerns = store.concerns(interview_id=interview_id)
        return render(request, "interview_detail.html", iv=iv, sh=sh, draft=draft,
                      traces=traces, segments=segments, coverage=cov, concerns=concerns,
                      accepted_keys=_accepted_keys(draft, concerns), message=message,
                      accepted_now=accepted_now)

    @app.get("/interviews/{interview_id}", response_class=HTMLResponse)
    def interview_detail(request: Request, interview_id: int):
        if (denied := restricted(request, f"interview {interview_id}")) is not None:
            return denied
        try:
            return interview_page(request, interview_id)
        except RecordError as e:
            return render(request, "partials/error.html", message=str(e), status_code=404)

    @app.post("/interviews/{interview_id}/extract", response_class=HTMLResponse)
    def extract(request: Request, interview_id: int):
        if (denied := restricted(request, f"interview {interview_id}")) is not None:
            return denied
        iv = store.interview(interview_id)
        sh = store.stakeholder(iv.stakeholder_id)
        try:
            draft = provider.extract_interview(iv.notes, f"{sh.name}, {sh.role}, "
                                                         f"{sh.business_unit}")
        except LLMError as e:
            return interview_page(request, interview_id, message=str(e))
        store.save_extraction(interview_id, draft, ai_label, actor(request))
        return RedirectResponse(f"/interviews/{interview_id}", status_code=303)

    @app.post("/interviews/{interview_id}/accept", response_class=HTMLResponse)
    async def accept(request: Request, interview_id: int):
        if (denied := restricted(request, f"interview {interview_id}")) is not None:
            return denied
        iv = store.interview(interview_id)
        if iv.draft is None:
            return interview_page(request, interview_id, message="Extract the notes first.")
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        try:
            notes = review_changes(iv.draft, form)
        except ValueError as e:
            return interview_page(request, interview_id, message=str(e))
        store.accept(interview_id, actor(request), reviewed_concerns(iv.draft, form), notes)
        return interview_page(request, interview_id, accepted_now=True)

    @app.post("/interviews/extract-all")
    def extract_all(request: Request):
        if (denied := restricted(request, "bulk extraction")) is not None:
            return denied
        people = {sh.id: sh for sh in store.stakeholders()}
        for iv in store.interviews():
            if iv.status == "notes":
                sh = people[iv.stakeholder_id]
                try:
                    draft = provider.extract_interview(iv.notes, f"{sh.name}, {sh.role}, "
                                                                 f"{sh.business_unit}")
                except LLMError:
                    continue
                store.save_extraction(iv.id, draft, ai_label, actor(request))
        return RedirectResponse("/interviews", status_code=303)

    @app.post("/interviews/accept-all")
    def accept_all(request: Request):
        """Accept every extracted-but-unreviewed interview exactly as drafted (logged as such)."""
        if (denied := restricted(request, "bulk acceptance")) is not None:
            return denied
        for iv in store.interviews():
            if iv.status == "extracted" and iv.draft is not None:
                d = iv.draft
                form = {f"pain:{i}": "on" for i in range(len(d.pain_points))}
                form |= {f"outcome:{i}": "on" for i in range(len(d.desired_outcomes))}
                form |= {f"follow:{i}": "on" for i in range(len(d.follow_up_questions))}
                store.accept(iv.id, actor(request) + " [accepted as drafted]",
                             reviewed_concerns(d, form))
        return RedirectResponse("/interviews", status_code=303)

    # ---------- synthesis ----------

    def synthesis_view(request: Request, message: str = "", notice: str = "") -> HTMLResponse:
        items = synth.items()
        run = synth.latest()
        context = {"items": items, "run": run, "message": message, "notice": notice,
                   "counts": store.counts(), "weights": synth.weights()[0],
                   "weights_set_by": synth.weights()[1], "default_weights": DEFAULT_WEIGHTS}
        if run is not None:
            syn = run.synthesis
            problems = rank(syn, items, synth.fits(run.id), synth.weights()[0])
            contradictions = [(c, *verify_ids(c.side_a_ids, items),
                               *verify_ids(c.side_b_ids, items)) for c in syn.contradictions]
            personas = [(pd, verify_ids(pd.based_on, items)[0]) for pd in syn.personas]
            handoffs = synth.handoffs(run.id)
            status = {k: suite_client.inbox_status(h.remote_id)
                      for k, h in handoffs.items() if k[0] == "problem"}
            context |= {"syn": syn, "problems": problems, "contradictions": contradictions,
                        "personas": personas, "handoffs": handoffs, "handoff_status": status,
                        "stale": run.input_text != synthesis_input(items),
                        "labels": {lc.label: lc for lc in items},
                        "outline": slide_outline(run, problems)}
        return render(request, "synthesis.html", **context)

    @app.get("/synthesis", response_class=HTMLResponse)
    def synthesis_page(request: Request):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        return synthesis_view(request)

    @app.post("/synthesis/run", response_class=HTMLResponse)
    def run_synthesis(request: Request):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        items = synth.items()
        if len({lc.stakeholder.id for lc in items}) < 2:
            return synthesis_view(request, "Accept at least two interviews first.")
        text = synthesis_input(items)
        try:
            result = provider.synthesize(text)
        except LLMError as e:
            return synthesis_view(request, str(e))
        synth.save(result, text, actor(request), ai_label)
        return RedirectResponse("/synthesis", status_code=303)

    @app.post("/synthesis/{run_id}/fit/{index}", response_class=HTMLResponse)
    def set_fit(request: Request, run_id: int, index: int, fit: int = Form(...),
                reason: str = Form("")):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        run = synth.latest()
        if run is None or run.id != run_id or not 0 <= index < len(run.synthesis.themes):
            return synthesis_view(request, "That synthesis is no longer current.")
        theme = run.synthesis.themes[index]
        current = synth.fits(run.id).get(index, (theme.strategic_fit, ""))[0]
        try:
            synth.set_fit(run_id, index, fit, actor(request), reason, theme.title, current)
        except ValueError as e:
            return synthesis_view(request, str(e))
        return RedirectResponse("/synthesis#ranking", status_code=303)

    @app.post("/synthesis/weights", response_class=HTMLResponse)
    async def set_weights(request: Request):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        form = await request.form()
        synth.set_weights({k: int(str(form.get(k, v))) if str(form.get(k, v)).isdigit() else v
                           for k, v in DEFAULT_WEIGHTS.items()}, actor(request))
        return RedirectResponse("/synthesis#ranking", status_code=303)

    @app.post("/synthesis/{run_id}/problems/{index}/send", response_class=HTMLResponse)
    def send_problem(request: Request, run_id: int, index: int):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        run = synth.latest()
        if run is None or run.id != run_id:
            return synthesis_view(request, "That synthesis is no longer current.")
        problem = next((p for p in rank(run.synthesis, synth.items(), synth.fits(run.id),
                                        synth.weights()[0]) if p.index == index), None)
        if problem is None:
            return synthesis_view(request, "That problem is not in the current synthesis.")
        ref = problem_key(run_id, index)
        quotes = [q for lc in problem.concerns for q in json.loads(lc.concern.quotes)]
        text = (f"{problem.theme.description}\n\nWhat stakeholders said (from listening-tour "
                f"interviews; quotes are not attributed):\n"
                + "\n".join(f'- "{q}"' for q in quotes[:12]))
        try:
            remote = suite_client.send_problem(problem.theme.title, text, ref,
                                               [p.name for p in problem.stakeholders],
                                               current_user(request).name)
        except SuiteError as e:
            return synthesis_view(request, str(e))
        synth.record_handoff(run_id, "problem", index, remote, actor(request),
                             f"{ref} {problem.theme.title}")
        return synthesis_view(request, notice=f"Sent {ref} to Backlog Pilot's intake inbox.")

    @app.post("/synthesis/{run_id}/personas/{index}/propose", response_class=HTMLResponse)
    def propose_persona(request: Request, run_id: int, index: int):
        if (denied := restricted(request, "the synthesis")) is not None:
            return denied
        run = synth.latest()
        if run is None or run.id != run_id or not 0 <= index < len(run.synthesis.personas):
            return synthesis_view(request, "That synthesis is no longer current.")
        pd = run.synthesis.personas[index]
        try:
            remote = suite_client.propose_persona(pd.name, pd.description, pd.goals,
                                                  pd.pain_points, current_user(request).name)
        except SuiteError as e:
            return synthesis_view(request, str(e))
        synth.record_handoff(run_id, "persona", index, remote, actor(request), pd.name)
        return synthesis_view(request, notice=f"Proposed '{pd.name}' to Backlog Pilot's "
                                              "persona library for approval.")

    def readout_parts(request: Request):
        run = synth.latest()
        if run is None:
            raise LookupError("No synthesis yet.")
        items = synth.items()
        problems = rank(run.synthesis, items, synth.fits(run.id), synth.weights()[0])
        return run, problems, items

    @app.exception_handler(LookupError)
    def no_synthesis(request: Request, exc: LookupError):
        return RedirectResponse("/synthesis", status_code=303)

    @app.get("/synthesis/readout.md")
    def readout_md(request: Request):
        if (denied := restricted(request, "the readout")) is not None:
            return denied
        run, problems, items = readout_parts(request)
        log_export(request, "Markdown")
        return PlainTextResponse(readout_markdown(run, problems, items,
                                                  store.counts()["accepted"]),
                                 media_type="text/markdown",
                                 headers={"Content-Disposition":
                                          'attachment; filename="listening-tour-readout.md"'})

    @app.get("/synthesis/readout.docx")
    def readout_word(request: Request):
        if (denied := restricted(request, "the readout")) is not None:
            return denied
        run, problems, items = readout_parts(request)
        log_export(request, "Word")
        body = readout_docx(readout_markdown(run, problems, items, store.counts()["accepted"]))
        return Response(body, media_type="application/vnd.openxmlformats-officedocument."
                                         "wordprocessingml.document",
                        headers={"Content-Disposition":
                                 'attachment; filename="listening-tour-readout.docx"'})

    @app.get("/synthesis/outline.txt")
    def readout_outline(request: Request):
        if (denied := restricted(request, "the readout")) is not None:
            return denied
        run, problems, _ = readout_parts(request)
        log_export(request, "slide outline")
        return PlainTextResponse(slide_outline(run, problems))

    def log_export(request: Request, kind: str) -> None:
        with Session(engine) as s:
            audit(s, actor(request), "concern_readout_exported", kind)
            s.commit()

    status_routes.register(app, status_routes.Deps(
        render=render, restricted=restricted, actor=actor, provider=provider,
        ai_label=ai_label, people=store, status=status, updates=UpdateStore(engine),
        suite_client=suite_client, delivery=delivery, deliverer=deliverer))

    # ---------- audit, user ----------

    @app.get("/audit", response_class=HTMLResponse)
    def audit_log(request: Request, q: str = "", actor_filter: str = "", category: str = "",
                  item: str = "", since: str = "", until: str = "", page: int = 1):
        if (denied := restricted(request, "audit log")) is not None:
            return denied
        page_size = 50
        query = audit_search.AuditQuery(
            text=q, actor=actor_filter, categories=[category] if category else [],
            item_keys=[k.upper() for k in re.findall(r"(?:SH|IV|WU|RAID)-\d+", item, re.I)],
            since=_parse_date(since), until=_parse_date(until))
        results = audit_search.search(engine, query, limit=page_size,
                                      offset=(max(page, 1) - 1) * page_size)
        return render(request, "audit.html", events=results.events, total=results.total,
                      page=max(page, 1), page_size=page_size, query=query, ask="",
                      plan=None, answer=None, cited=[], unverified=[], summary={},
                      message="", questions=[], people=audit_search.actors(engine),
                      categories=audit_search.CATEGORY_LABELS,
                      terms=[t for t in re.findall(r"\w+", query.text) if len(t) > 1])

    @app.post("/user")
    def switch_user(request: Request, user_id: str = Form(...)):
        back = urlparse(request.headers.get("referer", "")).path
        if not back.startswith("/") or back.startswith("//"):  # same-site paths only
            back = "/"
        response = RedirectResponse(back, status_code=303)
        if any(u.id == user_id for u in users):
            response.set_cookie(USER_COOKIE, user_id, httponly=True, samesite="lax")
        return response

    return app


def _accepted_keys(draft: InterviewExtraction | None, concerns: list[Concern]) -> set[str]:
    """Which draft items were accepted, so the review form re-opens with them ticked."""
    if draft is None or not concerns:
        return set()
    quotes = {(c.kind, c.quotes) for c in concerns}
    keys = {f"pain:{i}" for i, p in enumerate(draft.pain_points)
            if (PAIN, json.dumps(p.quotes)) in quotes}
    keys |= {f"outcome:{i}" for i, o in enumerate(draft.desired_outcomes)
             if any(c.kind == OUTCOME and c.title == o.description for c in concerns)}
    keys |= {f"follow:{i}" for i, q in enumerate(draft.follow_up_questions)
             if any(c.kind == FOLLOW_UP and c.title == q for c in concerns)}
    return keys


PUBLIC_PATHS = ("/static", STATIC_PATH, "/login", "/auth/callback", "/logout", "/healthz")


def _enable_oidc(app: FastAPI, auth: AuthSettings) -> None:
    """Sign-in with Microsoft Entra ID (or any OpenID Connect provider)."""
    from authlib.integrations.starlette_client import OAuth
    from starlette.middleware.sessions import SessionMiddleware

    oauth = OAuth()
    oauth.register("oidc", server_metadata_url=auth.metadata_url, client_id=auth.client_id,
                   client_secret=auth.client_secret,
                   client_kwargs={"scope": "openid profile email"})

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        if request.url.path.startswith(PUBLIC_PATHS) or request.session.get("user"):
            return await call_next(request)
        if request.method == "GET" and "hx-request" not in request.headers:
            request.session["next"] = request.url.path  # a sign-in prompt, not a refusal
            return RedirectResponse("/login", status_code=303)
        with Session(request.app.state.engine) as s:  # an action tried without signing in
            audit(s, "not signed in", "access_denied",
                  f"{request.method} {request.url.path} from "
                  f"{request.client.host if request.client else 'unknown'}")
            s.commit()
        return HTMLResponse("Sign in required.", status_code=401)

    app.add_middleware(SessionMiddleware, secret_key=auth.session_secret, same_site="lax",
                       https_only=auth.https_only, max_age=8 * 3600)

    @app.get("/login")
    async def login(request: Request):
        return await oauth.oidc.authorize_redirect(request, request.url_for("auth_callback"))

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)  # verifies the ID token
            user = user_from_claims(token["userinfo"], auth)
        except Exception as e:  # any failure (state, token, claims) is refused and audited
            with Session(request.app.state.engine) as s:
                audit(s, "not signed in", "sign_in_failed",
                      f"{type(e).__name__} from "
                      f"{request.client.host if request.client else 'unknown'}")
                s.commit()
            return HTMLResponse('Sign-in failed. <a href="/login">Try again</a>.',
                                status_code=401)
        request.session["user"] = user_to_session(user)
        with Session(request.app.state.engine) as s:
            audit(s, user.actor, "signed_in", user.email or user.id)
            s.commit()
        return RedirectResponse(request.session.pop("next", "/"), status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)


def main() -> None:
    import uvicorn

    uvicorn.run("leadership_pilot.app:create_app", factory=True, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
