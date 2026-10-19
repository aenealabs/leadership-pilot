"""Cross-interview synthesis: themes, contradictions, ranked problems, personas, journeys,
a 30-60-90 plan, and the readout exports.

The model groups and names; code does the counting. Every pain point the model cites is a
stable label (IV-3.2 = interview 3, pain point 2), checked against the accepted record, and
the ranking (how many stakeholders, how severe, how influential) is computed here, with the
weights shown and adjustable. Only strategic fit is a judgment: the model suggests it and a
person can change it.
"""

import io
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from leadership_pilot.db import audit, utcnow
from leadership_pilot.models import Synthesis, Theme
from leadership_pilot.stakeholders import PAIN, Concern, Interview, Stakeholder

SEVERITY_WEIGHT = {"low": 1, "medium": 2, "high": 3, "critical": 4}
LEVEL_WEIGHT = {"low": 1, "medium": 2, "high": 3}
DEFAULT_WEIGHTS = {"breadth": 3, "severity": 3, "influence": 2, "fit": 2}


# ---------- labelled input ----------

@dataclass(frozen=True)
class LabelledConcern:
    label: str
    concern: Concern
    stakeholder: Stakeholder


def labelled_pain_points(s: Session) -> list[LabelledConcern]:
    """Accepted pain points with labels that depend only on what was accepted, not on the
    order it was accepted in: IV-<interview>.<position within that interview>."""
    interviews = {i.id: i for i in s.exec(select(Interview))}
    people = {p.id: p for p in s.exec(select(Stakeholder))}
    by_interview: dict[int, list[Concern]] = {}
    for c in s.exec(select(Concern).where(Concern.kind == PAIN).order_by(Concern.id)):
        by_interview.setdefault(c.interview_id, []).append(c)
    out = []
    for iid in sorted(by_interview, key=lambda i: interviews[i].key):
        for n, c in enumerate(by_interview[iid], start=1):
            out.append(LabelledConcern(f"{interviews[iid].key}.{n}", c, people[c.stakeholder_id]))
    return sorted(out, key=lambda lc: (int(lc.label[3:].split(".")[0]),
                                       int(lc.label.split(".")[1])))


def synthesis_input(items: list[LabelledConcern]) -> str:
    """What the model sees: one line per pain point, labelled; no notes, no quotes."""
    return "\n".join(
        f"{lc.label} | {lc.stakeholder.name}, {lc.stakeholder.role}, "
        f"{lc.stakeholder.business_unit} | severity {lc.concern.severity} | "
        f"{lc.concern.title}: {lc.concern.description}" for lc in items)


# ---------- verification and ranking ----------

@dataclass
class RankedProblem:
    index: int  # position of the theme in the synthesis
    theme: Theme
    concerns: list[LabelledConcern]
    unknown_ids: list[str]
    breadth: int  # distinct stakeholders
    business_units: list[str]
    max_severity: str
    influence: str
    fit: int
    fit_set_by: str
    score: float = 0.0
    score_parts: dict = field(default_factory=dict)

    @property
    def stakeholders(self) -> list[Stakeholder]:
        seen: dict[int, Stakeholder] = {}
        for lc in self.concerns:
            seen.setdefault(lc.stakeholder.id, lc.stakeholder)
        return list(seen.values())


def rank(synthesis: Synthesis, items: list[LabelledConcern], fits: dict[int, tuple[int, str]],
         weights: dict[str, int]) -> list[RankedProblem]:
    by_label = {lc.label: lc for lc in items}
    people_total = len({lc.stakeholder.id for lc in items}) or 1
    problems = []
    for i, theme in enumerate(synthesis.themes):
        found = [by_label[x] for x in dict.fromkeys(theme.concern_ids) if x in by_label]
        unknown = [x for x in theme.concern_ids if x not in by_label]
        people = {lc.stakeholder.id: lc.stakeholder for lc in found}
        severity = max((lc.concern.severity for lc in found),
                       key=lambda v: SEVERITY_WEIGHT.get(v, 0), default="low")
        influence = max((p.influence for p in people.values()),
                        key=lambda v: LEVEL_WEIGHT.get(v, 0), default="low")
        fit, set_by = fits.get(i, (theme.strategic_fit, "AI suggestion"))
        parts = {  # each part normalized to 0-1, then weighted
            "breadth": len(people) / people_total,
            "severity": SEVERITY_WEIGHT[severity] / 4,
            "influence": LEVEL_WEIGHT[influence] / 3,
            "fit": fit / 5,
        }
        total_weight = sum(weights.values()) or 1
        score = round(100 * sum(parts[k] * weights[k] for k in parts) / total_weight, 1)
        problems.append(RankedProblem(
            i, theme, found, unknown, len(people),
            sorted({p.business_unit for p in people.values()}), severity, influence, fit,
            set_by, score, parts))
    return sorted(problems, key=lambda p: (-p.score, p.index))


def verify_ids(ids: list[str], items: list[LabelledConcern]) -> tuple[list[LabelledConcern],
                                                                     list[str]]:
    by_label = {lc.label: lc for lc in items}
    return [by_label[x] for x in ids if x in by_label], [x for x in ids if x not in by_label]


# ---------- storage ----------

class SynthesisRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    payload: str  # the model's Synthesis, JSON, kept as drafted
    input_text: str
    created_by: str
    drafted_by: str
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def synthesis(self) -> Synthesis:
        return Synthesis.model_validate_json(self.payload)


class FitOverride(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="synthesisrun.id", index=True)
    theme_index: int
    fit: int
    set_by: str
    reason: str = ""


class ScoringWeights(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    weights: str = json.dumps(DEFAULT_WEIGHTS)
    set_by: str = "default"


class Handoff(SQLModel, table=True):
    """A problem or persona sent to Backlog Pilot."""
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="synthesisrun.id", index=True)
    kind: str  # problem | persona
    item_index: int
    remote_id: int
    sent_by: str
    sent_at: datetime = Field(default_factory=utcnow)


class SynthesisStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def items(self) -> list[LabelledConcern]:
        with Session(self.engine) as s:
            return labelled_pain_points(s)

    def save(self, synthesis: Synthesis, input_text: str, actor: str, drafted_by: str) -> int:
        with Session(self.engine) as s:
            run = SynthesisRun(payload=synthesis.model_dump_json(), input_text=input_text,
                               created_by=actor, drafted_by=drafted_by)
            s.add(run)
            s.flush()
            audit(s, actor, "synthesis_drafted",
                  f"run {run.id}: {len(synthesis.themes)} themes, "
                  f"{len(synthesis.contradictions)} contradictions, "
                  f"{len(synthesis.personas)} personas ({drafted_by})")
            s.commit()
            return run.id

    def latest(self) -> SynthesisRun | None:
        with Session(self.engine) as s:
            return s.exec(select(SynthesisRun).order_by(SynthesisRun.id.desc())).first()

    def fits(self, run_id: int) -> dict[int, tuple[int, str]]:
        with Session(self.engine) as s:
            rows = s.exec(select(FitOverride).where(FitOverride.run_id == run_id)
                          .order_by(FitOverride.id)).all()
        return {r.theme_index: (r.fit, r.set_by) for r in rows}  # latest wins

    def set_fit(self, run_id: int, theme_index: int, fit: int, actor: str, reason: str,
                theme_title: str, current: int | None = None) -> None:
        """Override the strategic fit (the AI suggests one); a change needs a reason."""
        if not 1 <= fit <= 5:
            raise ValueError("Strategic fit is 1 to 5.")
        if fit == current:
            return
        if not reason.strip():
            raise ValueError(f"Say why the strategic fit of '{theme_title}' should be {fit}, "
                             "since it changes the ranking.")
        with Session(self.engine) as s:
            s.add(FitOverride(run_id=run_id, theme_index=theme_index, fit=fit, set_by=actor,
                              reason=reason.strip()))
            audit(s, actor, "concern_fit_set", f"run {run_id} '{theme_title}': fit {fit}"
                  + (f" | reason: {reason.strip()}" if reason.strip() else ""))
            s.commit()

    def weights(self) -> tuple[dict[str, int], str]:
        with Session(self.engine) as s:
            row = s.exec(select(ScoringWeights).order_by(ScoringWeights.id.desc())).first()
        return (json.loads(row.weights), row.set_by) if row else (dict(DEFAULT_WEIGHTS),
                                                                  "default")

    def set_weights(self, weights: dict[str, int], actor: str) -> None:
        clean = {k: max(0, min(5, int(weights.get(k, v)))) for k, v in DEFAULT_WEIGHTS.items()}
        with Session(self.engine) as s:
            s.add(ScoringWeights(weights=json.dumps(clean), set_by=actor))
            audit(s, actor, "concern_weights_set", json.dumps(clean))
            s.commit()

    def record_handoff(self, run_id: int, kind: str, index: int, remote_id: int, actor: str,
                       label: str) -> None:
        with Session(self.engine) as s:
            s.add(Handoff(run_id=run_id, kind=kind, item_index=index, remote_id=remote_id,
                          sent_by=actor))
            audit(s, actor, f"concern_sent_{kind}", f"{label} -> Backlog Pilot #{remote_id}")
            s.commit()

    def handoffs(self, run_id: int) -> dict[tuple[str, int], Handoff]:
        with Session(self.engine) as s:
            rows = s.exec(select(Handoff).where(Handoff.run_id == run_id)).all()
        return {(h.kind, h.item_index): h for h in rows}


# ---------- readout exports ----------

def problem_key(run_id: int, index: int) -> str:
    return f"PR-{run_id}.{index + 1}"


def readout_markdown(run: SynthesisRun, problems: list[RankedProblem],
                     items: list[LabelledConcern], interviews: int) -> str:
    syn = run.synthesis
    lines = ["# Listening tour readout: what I heard", "",
             f"*{len({lc.stakeholder.id for lc in items})} stakeholders, {interviews} "
             f"interviews, {len(items)} pain points.*", "", f"**{syn.headline}**", "",
             "## The top problems", ""]
    for n, p in enumerate(problems, start=1):
        lines.append(f"{n}. **{p.theme.title}** ({p.breadth} stakeholders across "
                     f"{', '.join(p.business_units)}; up to {p.max_severity} severity)  ")
        lines.append(f"   {p.theme.description}")
        quote = next((q for lc in p.concerns for q in json.loads(lc.concern.quotes)), None)
        if quote:
            lines.append(f"   > \"{quote}\"")
        lines.append("")
    if syn.contradictions:
        lines += ["## Where people disagree", ""]
        for c in syn.contradictions:
            lines += [f"- **{c.topic}.** {c.side_a} / {c.side_b} {c.why_it_matters}"]
        lines.append("")
    lines += ["## What I'll do", ""]
    for horizon in ("30", "60", "90"):
        actions = [p for p in syn.plan if p.horizon == horizon]
        if actions:
            lines.append(f"**First {horizon} days**")
            lines += [f"- {a.action}" for a in actions]
            lines.append("")
    lines += ["---", "*Prepared with Leadership Pilot. Every problem traces to accepted pain "
              "points and verbatim interview quotes; quotes are not attributed by name.*"]
    return "\n".join(lines) + "\n"


def slide_outline(run: SynthesisRun, problems: list[RankedProblem]) -> str:
    """Paste into PowerPoint's Outline view: unindented lines become slide titles and
    tab-indented lines become bullets. Speaker notes follow as 'Notes:' bullets."""
    syn = run.synthesis
    out = ["What I heard: listening tour readout", f"\t{syn.headline}"]
    out += ["Top problems"] + [f"\t{n}. {p.theme.title} ({p.breadth} stakeholders)"
                               for n, p in enumerate(problems[:6], start=1)]
    for n, p in enumerate(problems[:5], start=1):
        out.append(f"Problem {n}: {p.theme.title}")
        out.append(f"\t{p.theme.description}")
        out.append(f"\tRaised by {p.breadth} stakeholders: {', '.join(p.business_units)}")
        out.append(f"\tUp to {p.max_severity} severity; strategic fit {p.fit}/5")
        quotes = [q for lc in p.concerns for q in json.loads(lc.concern.quotes)][:2]
        out += [f"\tNotes: \"{q}\"" for q in quotes]
    if syn.contradictions:
        out.append("Where people disagree")
        out += [f"\t{c.topic}: {c.side_a} vs. {c.side_b}" for c in syn.contradictions[:4]]
    out.append("My 30-60-90 day plan")
    for horizon in ("30", "60", "90"):
        out += [f"\t{horizon} days: {a.action}" for a in syn.plan if a.horizon == horizon]
    return "\n".join(out) + "\n"


def readout_docx(markdown: str) -> bytes:
    """A Word version of the Markdown readout (headings, numbered items, bullets, quotes)."""
    from docx import Document

    doc = Document()
    for raw in markdown.splitlines():
        line = raw.rstrip()
        text = line.strip().replace("**", "").replace("*", "")
        if not text or text == "---":
            continue
        if line.startswith("# "):
            doc.add_heading(text[2:], level=0)
        elif line.startswith("## "):
            doc.add_heading(text[3:], level=1)
        elif line.strip().startswith(">"):
            doc.add_paragraph(text.lstrip("> ").strip(), style="Intense Quote")
        elif line.startswith("- "):
            doc.add_paragraph(text[2:], style="List Bullet")
        elif line[:1].isdigit() and ". " in line[:4]:
            doc.add_paragraph(text.split(". ", 1)[1], style="List Number")
        else:
            doc.add_paragraph(text)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
