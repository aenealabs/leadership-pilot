"""The shared stakeholder record: people, their interviews, and the concerns they raised.

An interview's notes are extracted by the model into a draft (kept verbatim for provenance);
a person reviews the draft and accepts the pain points, outcomes, and follow-ups worth
keeping. Accepted concerns are what both tools build on.
"""

import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from leadership_pilot.db import audit, utcnow
from leadership_pilot.models import InterviewExtraction

LEVELS = ("low", "medium", "high")
PAIN, OUTCOME, FOLLOW_UP = "pain_point", "outcome", "follow_up"


class Stakeholder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    name: str
    role: str
    business_unit: str
    products: str = "[]"  # JSON list
    influence: str = "medium"
    interest: str = "medium"
    email: str = ""
    update_audience: str = ""  # weekly update version they receive: summary | detailed | ""
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def product_list(self) -> list[str]:
        return json.loads(self.products)


class Interview(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    stakeholder_id: int = Field(foreign_key="stakeholder.id", index=True)
    held_on: date
    notes: str
    status: str = "notes"  # notes | extracted | accepted
    extraction: str = ""  # the model's draft, JSON, kept for provenance
    extracted_by: str = ""
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def draft(self) -> InterviewExtraction | None:
        return InterviewExtraction.model_validate_json(self.extraction) \
            if self.extraction else None


class Concern(SQLModel, table=True):
    """An accepted pain point, outcome, or follow-up question from one interview."""
    id: int | None = Field(default=None, primary_key=True)
    interview_id: int = Field(foreign_key="interview.id", index=True)
    stakeholder_id: int = Field(foreign_key="stakeholder.id", index=True)
    kind: str  # pain_point | outcome | follow_up
    title: str
    description: str = ""
    severity: str = ""  # pain points: low | medium | high | critical
    systems: str = "[]"  # JSON list
    quotes: str = "[]"  # JSON list, verbatim from the notes
    edited: bool = False  # the reviewer changed the model's wording or severity
    accepted_by: str
    accepted_at: datetime = Field(default_factory=utcnow)

    @property
    def quote_list(self) -> list[str]:
        return json.loads(self.quotes)

    @property
    def system_list(self) -> list[str]:
        return json.loads(self.systems)


class RecordError(Exception):
    """A change was rejected; the message is user-facing."""


class StakeholderStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---------- seeding ----------

    def seed_if_empty(self, path: Path, interviews_dir: Path, actor: str) -> None:
        with Session(self.engine) as s:
            if s.exec(select(func.count()).select_from(Stakeholder)).one():
                return
        data = json.loads(path.read_text())["stakeholders"]
        for i, row in enumerate(data):
            sh = self.add_stakeholder(actor, row["name"], row["role"], row["business_unit"],
                                      row.get("products", []), row["influence"], row["interest"],
                                      row.get("email", ""), row.get("update_audience", ""))
            notes_file = interviews_dir / row.get("interview", "")
            if row.get("interview") and notes_file.exists():
                self.add_interview(actor, sh.id, date(2026, 9, 1 + i * 4),
                                   notes_file.read_text())

    # ---------- stakeholders ----------

    def add_stakeholder(self, actor: str, name: str, role: str, business_unit: str,
                        products: list[str], influence: str, interest: str, email: str = "",
                        update_audience: str = "") -> Stakeholder:
        if not name.strip() or not role.strip():
            raise RecordError("A stakeholder needs a name and a role.")
        if influence not in LEVELS or interest not in LEVELS:
            raise RecordError("Influence and interest must be low, medium, or high.")
        with Session(self.engine) as s:
            count = s.exec(select(func.count()).select_from(Stakeholder)).one()
            sh = Stakeholder(key=f"SH-{count + 1}", name=name.strip(), role=role.strip(),
                             business_unit=business_unit.strip(), products=json.dumps(products),
                             influence=influence, interest=interest, email=email.strip(),
                             update_audience=update_audience)
            s.add(sh)
            s.flush()
            audit(s, actor, "stakeholder_added", f"{sh.key} {sh.name}, {sh.role}")
            s.commit()
            s.refresh(sh)
            return sh

    def stakeholders(self) -> list[Stakeholder]:
        with Session(self.engine) as s:
            return list(s.exec(select(Stakeholder).order_by(Stakeholder.id)))

    def set_distribution(self, stakeholder_id: int, email: str, audience: str,
                         actor: str) -> None:
        if audience not in ("", "summary", "detailed"):
            raise RecordError("Choose summary, detailed, or none.")
        email = email.strip()
        if audience and "@" not in email:
            raise RecordError("Add an email address to send the weekly update.")
        with Session(self.engine) as s:
            sh = s.get(Stakeholder, stakeholder_id)
            if sh is None:
                raise RecordError("That stakeholder does not exist.")
            sh.email, sh.update_audience = email, audience
            s.add(sh)
            audit(s, actor, "distribution_changed",
                  f"{sh.key} {sh.name}: {audience or 'no'} weekly update"
                  + (f" to {email}" if audience else ""))
            s.commit()

    def recipients(self, audience: str) -> list[Stakeholder]:
        return [sh for sh in self.stakeholders() if sh.update_audience == audience and sh.email]

    def stakeholder(self, stakeholder_id: int) -> Stakeholder:
        with Session(self.engine) as s:
            sh = s.get(Stakeholder, stakeholder_id)
            if sh is None:
                raise RecordError("That stakeholder does not exist.")
            return sh

    # ---------- interviews ----------

    def add_interview(self, actor: str, stakeholder_id: int, held_on: date,
                      notes: str) -> Interview:
        if len(notes.strip()) < 40:
            raise RecordError("Add the interview notes (at least a few sentences).")
        with Session(self.engine) as s:
            if s.get(Stakeholder, stakeholder_id) is None:
                raise RecordError("Pick a stakeholder for this interview.")
            count = s.exec(select(func.count()).select_from(Interview)).one()
            iv = Interview(key=f"IV-{count + 1}", stakeholder_id=stakeholder_id,
                           held_on=held_on, notes=notes.strip(), created_by=actor)
            s.add(iv)
            s.flush()
            audit(s, actor, "interview_added", f"{iv.key} with SH-{stakeholder_id}")
            s.commit()
            s.refresh(iv)
            return iv

    def interviews(self, stakeholder_id: int | None = None) -> list[Interview]:
        with Session(self.engine) as s:
            query = select(Interview).order_by(Interview.held_on, Interview.id)
            if stakeholder_id is not None:
                query = query.where(Interview.stakeholder_id == stakeholder_id)
            return list(s.exec(query))

    def interview(self, interview_id: int) -> Interview:
        with Session(self.engine) as s:
            iv = s.get(Interview, interview_id)
            if iv is None:
                raise RecordError("That interview does not exist.")
            return iv

    def save_extraction(self, interview_id: int, extraction: InterviewExtraction,
                        provider_label: str, actor: str) -> None:
        with Session(self.engine) as s:
            iv = s.get(Interview, interview_id)
            iv.extraction = extraction.model_dump_json()
            iv.extracted_by = provider_label
            if iv.status == "notes":
                iv.status = "extracted"
            s.add(iv)
            audit(s, actor, "extraction_drafted",
                  f"{iv.key}: {len(extraction.pain_points)} pain points, "
                  f"{len(extraction.desired_outcomes)} outcomes ({provider_label})")
            s.commit()

    def accept(self, interview_id: int, actor: str, concerns: list[Concern],
               notes: list[str] | None = None) -> list[Concern]:
        """Replace the interview's accepted concerns with the reviewed set."""
        with Session(self.engine) as s:
            iv = s.get(Interview, interview_id)
            for old in s.exec(select(Concern).where(Concern.interview_id == interview_id)):
                s.delete(old)
            for c in concerns:
                c.interview_id, c.stakeholder_id, c.accepted_by = (
                    interview_id, iv.stakeholder_id, actor)
                s.add(c)
            iv.status = "accepted"
            s.add(iv)
            edited = sum(c.edited for c in concerns)
            audit(s, actor, "extraction_accepted",
                  f"{iv.key}: {sum(c.kind == PAIN for c in concerns)} pain points, "
                  f"{sum(c.kind == OUTCOME for c in concerns)} outcomes, "
                  f"{sum(c.kind == FOLLOW_UP for c in concerns)} follow-ups accepted"
                  + (f"; {edited} edited by the reviewer" if edited else "")
                  + "".join(f"; {n}" for n in notes or []))
            s.commit()
            return list(s.exec(select(Concern).where(Concern.interview_id == interview_id)))

    def concerns(self, stakeholder_id: int | None = None,
                 interview_id: int | None = None) -> list[Concern]:
        with Session(self.engine) as s:
            query = select(Concern).order_by(Concern.id)
            if stakeholder_id is not None:
                query = query.where(Concern.stakeholder_id == stakeholder_id)
            if interview_id is not None:
                query = query.where(Concern.interview_id == interview_id)
            return list(s.exec(query))

    def counts(self) -> dict[str, int]:
        with Session(self.engine) as s:
            interviews = s.exec(select(Interview)).all()
            pains = s.exec(select(func.count()).select_from(Concern)
                           .where(Concern.kind == PAIN)).one()
        return {"stakeholders": len(self.stakeholders()), "interviews": len(interviews),
                "accepted": sum(i.status == "accepted" for i in interviews),
                "pain_points": pains}
