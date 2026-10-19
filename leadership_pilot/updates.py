"""The weekly leadership update: an AI draft over computed status, checked, edited, approved.

The model writes sentences; code checks each one. A sentence must cite facts that exist,
every number in it must appear in a fact it cites, and it may not state a status color
(status is computed by rules and shown beside the text). Flagged sentences must be fixed or
removed before approval. Every edit is a new version; the AI draft stays as version 1.
"""

import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from leadership_pilot.db import audit, utcnow
from leadership_pilot.models import CitedSentence, WeeklyUpdateDraft
from leadership_pilot.rules import DIMENSIONS, Assessment, d_label, narrative_input

DRAFT, APPROVED = "draft", "approved"
SUMMARY, DETAILED = "summary", "detailed"
AUDIENCES = {SUMMARY: "Summary (VP and business leaders)",
             DETAILED: "Detailed (product and engineering leads)"}
# A number standing on its own ("13%", "$411K", "Oct 9", "BP-104"), not digits inside a
# word or name ("3DEXPERIENCE", "S/4", "Q4"); a single unit letter may follow ("411K").
NUMBER = re.compile(r"(?<![\w/])\d[\d,]*(?:\.\d+)?(?![A-Za-z]{2})")
STATUS_WORDS = re.compile(r"\b(red|amber|green|yellow)\b", re.IGNORECASE)


class UpdateError(ValueError):
    """The update cannot be changed this way; the message is user-facing."""


# ---------- checks ----------

def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(text)}


def check_sentence(sentence: CitedSentence, facts: dict[str, str]) -> list[str]:
    """Problems with one sentence, empty if it is supported by its citations."""
    issues = []
    known = [i for i in sentence.fact_ids if i in facts]
    unknown = [i for i in sentence.fact_ids if i not in facts]
    if not sentence.fact_ids:
        issues.append("No citation: every sentence must cite the data behind it.")
    if unknown:
        issues.append(f"Cites {', '.join(unknown)}, which is not in the data.")
    cited_numbers = set().union(*(numbers_in(facts[i]) for i in known)) if known else set()
    missing = sorted(numbers_in(sentence.text) - cited_numbers, key=lambda n: float(n))
    if missing and sentence.fact_ids:
        issues.append(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in "
                      "the cited facts.")
    if (word := STATUS_WORDS.search(sentence.text)) is not None:
        issues.append(f"States a status ('{word.group(0)}'); status is computed by rules and "
                      "shown beside the text.")
    return issues


def sections(content: WeeklyUpdateDraft) -> list[tuple[str, str, list[CitedSentence]]]:
    """(field prefix, heading, sentences) for every editable part, in reading order."""
    out = [("summary", "Summary", content.summary)]
    out += [(f"products.{p.product_key}", p.product_key, p.sentences) for p in content.products]
    out += [("decisions", "Decisions needed", content.decisions),
            ("risks", "Risks to watch", content.risks)]
    return out


def all_issues(content: WeeklyUpdateDraft, facts: dict[str, str],
               product_keys: set[str]) -> dict[str, list[str]]:
    """Problems keyed by sentence field name (e.g. 'products.plm.1')."""
    issues = {}
    for prefix, _, sentences in sections(content):
        for i, s in enumerate(sentences):
            if found := check_sentence(s, facts):
                issues[f"{prefix}.{i}"] = found
    for p in content.products:
        if p.product_key not in product_keys:
            issues[f"products.{p.product_key}"] = [f"'{p.product_key}' is not a product in the "
                                                   "data."]
    return issues


def apply_edits(content: WeeklyUpdateDraft, form: dict[str, str]) -> WeeklyUpdateDraft:
    """Form fields 'text:<field>' replace a sentence; 'drop:<field>' removes it."""
    data = content.model_copy(deep=True)
    for prefix, _, sentences in sections(data):
        kept = []
        for i, s in enumerate(sentences):
            name = f"{prefix}.{i}"
            if form.get(f"drop:{name}"):
                continue
            text = " ".join(form.get(f"text:{name}", s.text).split())
            if text:
                kept.append(CitedSentence(text=text, fact_ids=s.fact_ids))
        sentences[:] = kept
    return data


# ---------- storage ----------

class WeeklyUpdate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    as_of: date
    snapshot_id: int
    input_text: str
    assessment: str  # the computed status, warnings, and facts, frozen as drafted
    draft: str  # the model's output, JSON, kept for provenance
    content: str  # current text, JSON
    version: int = 1
    status: str = DRAFT
    drafted_by: str
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    approved_by: str = ""
    approved_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"WU-{self.id}"

    @property
    def body(self) -> WeeklyUpdateDraft:
        return WeeklyUpdateDraft.model_validate_json(self.content)

    @property
    def frozen(self) -> dict:
        return json.loads(self.assessment)

    @property
    def facts(self) -> dict[str, str]:
        return {f["id"]: f["text"] for f in self.frozen["facts"]}


class UpdateVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    update_id: int = Field(foreign_key="weeklyupdate.id", index=True)
    version: int
    content: str
    actor: str
    reason: str
    at: datetime = Field(default_factory=utcnow)


class Delivery(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    update_id: int = Field(foreign_key="weeklyupdate.id", index=True)
    version: int
    channel: str  # outlook | teams | slack
    audience: str
    target: str  # recipients or channel
    mode: str  # simulated | live
    status: str  # sent | drafted | failed | simulated
    message_id: str = ""
    payload: str  # exactly what was (or would have been) sent
    detail: str = ""
    actor: str
    at: datetime = Field(default_factory=utcnow)


def freeze(a: Assessment) -> str:
    data = asdict(a)
    data["as_of"] = a.as_of.isoformat()
    for p in data["products"]:
        p["overall"] = next(x.overall for x in a.products if x.key == p["key"])
    return json.dumps(data)


class UpdateStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, a: Assessment, snapshot_id: int, draft: WeeklyUpdateDraft, actor: str,
               drafted_by: str) -> WeeklyUpdate:
        body = draft.model_dump_json()
        with Session(self.engine) as s:
            u = WeeklyUpdate(as_of=a.as_of, snapshot_id=snapshot_id,
                             input_text=narrative_input(a), assessment=freeze(a), draft=body,
                             content=body, drafted_by=drafted_by, created_by=actor)
            s.add(u)
            s.flush()
            s.add(UpdateVersion(update_id=u.id, version=1, content=body, actor=drafted_by,
                                reason=f"AI draft requested by {actor}"))
            issues = all_issues(draft, u.facts, {p["key"] for p in u.frozen["products"]})
            audit(s, actor, "update_drafted",
                  f"WU-{u.id} for {d_label(a.as_of)} ({drafted_by}); "
                  f"{len(issues)} sentence(s) flagged")
            s.commit()
            s.refresh(u)
        return u

    def get(self, update_id: int) -> WeeklyUpdate:
        with Session(self.engine) as s:
            u = s.get(WeeklyUpdate, update_id)
        if u is None:
            raise UpdateError("That update does not exist.")
        return u

    def all(self) -> list[WeeklyUpdate]:
        with Session(self.engine) as s:
            return list(s.exec(select(WeeklyUpdate).order_by(WeeklyUpdate.id.desc())))

    def versions(self, update_id: int) -> list[UpdateVersion]:
        with Session(self.engine) as s:
            return list(s.exec(select(UpdateVersion).where(UpdateVersion.update_id == update_id)
                               .order_by(UpdateVersion.version.desc())))

    def edit(self, update_id: int, form: dict[str, str], actor: str, reason: str) -> int:
        """Apply edits as a new version. Returns the new version number (or the current one
        if nothing changed)."""
        with Session(self.engine) as s:
            u = s.get(WeeklyUpdate, update_id)
            if u is None:
                raise UpdateError("That update does not exist.")
            if u.status != DRAFT:
                raise UpdateError("This update is approved; its text can no longer change.")
            new = apply_edits(u.body, form).model_dump_json()
            if new == u.content:
                return u.version
            u.content, u.version = new, u.version + 1
            s.add(UpdateVersion(update_id=u.id, version=u.version, content=new, actor=actor,
                                reason=reason.strip() or "Edited"))
            audit(s, actor, "update_edited", f"WU-{u.id} version {u.version}"
                  + (f": {reason.strip()}" if reason.strip() else ""))
            s.add(u)
            s.commit()
            return u.version

    def approve(self, update_id: int, actor: str) -> None:
        with Session(self.engine) as s:
            u = s.get(WeeklyUpdate, update_id)
            if u is None:
                raise UpdateError("That update does not exist.")
            if u.status == APPROVED:
                return
            issues = all_issues(u.body, u.facts, {p["key"] for p in u.frozen["products"]})
            if issues:
                raise UpdateError(f"Fix or remove the {len(issues)} flagged sentence"
                                  f"{'' if len(issues) == 1 else 's'} before approving.")
            u.status, u.approved_by, u.approved_at = APPROVED, actor, utcnow()
            s.add(u)
            audit(s, actor, "update_approved", f"WU-{u.id} version {u.version}")
            s.commit()

    def record_delivery(self, d: Delivery) -> Delivery:
        with Session(self.engine) as s:
            s.add(d)
            audit(s, d.actor, f"update_{d.status}",
                  f"WU-{d.update_id} v{d.version} via {d.channel} ({d.audience}) to {d.target}"
                  + (f"; message {d.message_id}" if d.message_id else "")
                  + (f"; {d.detail}" if d.detail else ""))
            s.commit()
            s.refresh(d)
        return d

    def deliveries(self, update_id: int) -> list[Delivery]:
        with Session(self.engine) as s:
            return list(s.exec(select(Delivery).where(Delivery.update_id == update_id)
                               .order_by(Delivery.id.desc())))


# ---------- rendering ----------

DOT = {"red": "🔴", "amber": "🟠", "green": "🟢"}
COLOR = {"red": "#ca3c3d", "amber": "#f47721", "green": "#39b620"}


@dataclass
class Rendered:
    subject: str
    text: str  # plain text / Markdown-ish, also used for Teams and Slack
    html: str  # email body


def render(u: WeeklyUpdate, audience: str) -> Rendered:
    """The update for one audience. Citations become numbered sources at the end."""
    body, frozen = u.body, u.frozen
    facts = u.facts
    names = {p["key"]: p for p in frozen["products"]}
    order: list[str] = []

    def refs(s: CitedSentence) -> list[int]:
        out = []
        for fid in s.fact_ids:
            if fid in facts:
                if fid not in order:
                    order.append(fid)
                out.append(order.index(fid) + 1)
        return out

    def sent_txt(s: CitedSentence) -> str:
        return s.text + "".join(f"[{n}]" for n in refs(s))

    def sent_html(s: CitedSentence) -> str:
        sup = ",".join(str(n) for n in refs(s))
        return html.escape(s.text) + (f"<sup>{sup}</sup>" if sup else "")

    detailed = audience == DETAILED
    risks = body.risks if detailed else body.risks[:2]
    t = [body.subject, "", *[sent_txt(s) for s in body.summary], ""]
    h = [f"<p>{' '.join(sent_html(s) for s in body.summary)}</p>",
         '<table cellpadding="6" style="border-collapse:collapse;font-size:14px">',
         "<tr><th align=left>Product</th><th align=left>Status</th>"
         + ("".join(f"<th align=left>{d.title()}</th>" for d in DIMENSIONS) if detailed else "")
         + "<th align=left>Headline</th></tr>"]
    t.append("Status by product (computed from the data):")
    for p in body.products:
        info = names.get(p.product_key)
        if info is None or not p.sentences:
            continue
        overall = info["overall"]
        shown = p.sentences if detailed else p.sentences[:1]
        t.append(f"{DOT[overall]} {info['name']} ({overall}): "
                 + " ".join(sent_txt(s) for s in shown))
        cells = "".join(f'<td style="color:{COLOR[info["dims"][d]["level"]]}">'
                        f'{info["dims"][d]["level"].title()}</td>' for d in DIMENSIONS)
        h.append(f"<tr><td><strong>{html.escape(info['name'])}</strong></td>"
                 f'<td style="color:{COLOR[overall]}"><strong>{overall.title()}</strong></td>'
                 + (cells if detailed else "")
                 + f"<td>{' '.join(sent_html(s) for s in shown)}</td></tr>")
    h.append("</table>")
    for heading, items in (("Decisions needed", body.decisions), ("Risks to watch", risks)):
        if items:
            t += ["", f"{heading}:", *[f"- {sent_txt(s)}" for s in items]]
            h.append(f"<h3>{heading}</h3><ul>"
                     + "".join(f"<li>{sent_html(s)}</li>" for s in items) + "</ul>")
    sources = {f["id"]: f for f in frozen["facts"]}
    t += ["", "Sources:"] + [f"[{n}] {sources[fid]['text']} ({sources[fid]['source']})"
                             for n, fid in enumerate(order, start=1)]
    h.append('<p style="font-size:12px;color:#555"><strong>Sources</strong><br>'
             + "<br>".join(f"[{n}] {html.escape(sources[fid]['text'])} "
                           f"<em>({html.escape(sources[fid]['source'])})</em>"
                           for n, fid in enumerate(order, start=1))
             + f"<br>Status is computed by rules from data as of {frozen['as_of']}; "
               f"prepared with Leadership Pilot, approved by {html.escape(u.approved_by or '-')}"
               ".</p>")
    return Rendered(body.subject, "\n".join(t), "\n".join(h))
