"""Status inputs: Backlog Pilot portfolio snapshots, budget plan vs actuals, and the RAID log.

Snapshots are stored whole, so every status and update can say exactly which data it used.
The offline demo seeds a fictional snapshot; with the suite connected, a person refreshes it
from Backlog Pilot's read-only API.
"""

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from leadership_pilot.db import audit, utcnow
from leadership_pilot.rules import Assessment, BudgetRow, RaidRow, assess

RAID_KINDS = ("risk", "issue", "assumption", "dependency")
LEVELS = ("low", "medium", "high")


class StatusError(ValueError):
    """Invalid status input; the message is user-facing."""


class PortfolioSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    as_of: date
    source: str
    data: str  # the portfolio JSON as received
    fetched_by: str
    fetched_at: datetime = Field(default_factory=utcnow)

    @property
    def portfolio(self) -> dict:
        return json.loads(self.data)


class BudgetLine(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    product: str = Field(index=True)
    month: str  # YYYY-MM
    plan_k: float
    actual_k: float | None = None


class RaidItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str
    product: str = Field(index=True)
    title: str
    description: str = ""
    impact: str = "medium"
    probability: str = ""  # risks only
    owner: str = ""
    due: date | None = None
    confirmed: bool = False  # dependencies only
    mitigation: str = ""
    status: str = "open"  # open | closed
    updated_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"RAID-{self.id}"


def parse_budget_csv(text: str) -> list[BudgetLine]:
    """Columns: product, month (YYYY-MM), plan_k, actual_k (blank for future months)."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    needed = {"product", "month", "plan_k", "actual_k"}
    if not reader.fieldnames or not needed <= {h.strip() for h in reader.fieldnames}:
        raise StatusError("The budget file needs the columns product, month, plan_k, actual_k.")
    lines = []
    for n, row in enumerate(reader, start=2):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        try:
            datetime.strptime(row["month"], "%Y-%m")
            lines.append(BudgetLine(product=row["product"], month=row["month"],
                                    plan_k=float(row["plan_k"]),
                                    actual_k=float(row["actual_k"]) if row["actual_k"] else None))
        except ValueError as e:
            raise StatusError(f"Budget file line {n}: month must be YYYY-MM and amounts must "
                              "be numbers.") from e
    if not lines:
        raise StatusError("The budget file has no rows.")
    return lines


class StatusStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def seed_if_empty(self, demo_dir: Path, actor: str) -> None:
        with Session(self.engine) as s:
            if s.exec(select(PortfolioSnapshot)).first() is not None:
                return
        data = json.loads((demo_dir / "portfolio.json").read_text())
        self.save_snapshot(data, actor)
        self.import_budget((demo_dir / "budget.csv").read_text(), actor, "demo budget.csv")
        with Session(self.engine) as s:
            for r in json.loads((demo_dir / "raid.json").read_text())["items"]:
                s.add(RaidItem(kind=r["kind"], product=r["product"], title=r["title"],
                               description=r.get("description", ""), impact=r["impact"],
                               probability=r.get("probability", ""), owner=r.get("owner", ""),
                               due=date.fromisoformat(r["due"]) if r.get("due") else None,
                               confirmed=r.get("confirmed", False),
                               mitigation=r.get("mitigation", ""), updated_by=actor))
            s.commit()

    # ---------- snapshots ----------

    def save_snapshot(self, data: dict, actor: str) -> PortfolioSnapshot:
        if not data.get("products") or not data.get("quarters"):
            raise StatusError("The portfolio data has no products or quarters.")
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        snap = PortfolioSnapshot(as_of=date.fromisoformat(data["as_of"]),
                                 source=data.get("source", "Backlog Pilot"),
                                 data=json.dumps(data), fetched_by=actor)
        with Session(self.engine) as s:
            s.add(snap)
            audit(s, actor, "portfolio_snapshot",
                  f"{snap.source} as of {snap.as_of}: {len(data['products'])} products")
            s.commit()
            s.refresh(snap)
        return snap

    def snapshot(self) -> PortfolioSnapshot | None:
        with Session(self.engine) as s:
            return s.exec(select(PortfolioSnapshot)
                          .order_by(PortfolioSnapshot.id.desc())).first()

    def products(self) -> list[tuple[str, str]]:
        snap = self.snapshot()
        return [(p["key"], p["name"]) for p in snap.portfolio["products"]] if snap else []

    # ---------- budget ----------

    def import_budget(self, text: str, actor: str, filename: str = "") -> int:
        lines = parse_budget_csv(text)
        with Session(self.engine) as s:
            for old in s.exec(select(BudgetLine)).all():
                s.delete(old)
            s.add_all(lines)
            months = sorted({b.month for b in lines})
            audit(s, actor, "budget_imported",
                  f"{filename or 'budget file'}: {len(lines)} rows, {months[0]} to {months[-1]}")
            s.commit()
        return len(lines)

    def budget(self) -> list[BudgetLine]:
        with Session(self.engine) as s:
            return list(s.exec(select(BudgetLine).order_by(BudgetLine.product,
                                                           BudgetLine.month)))

    # ---------- RAID ----------

    def raid(self, include_closed: bool = False) -> list[RaidItem]:
        with Session(self.engine) as s:
            query = select(RaidItem).order_by(RaidItem.id)
            if not include_closed:
                query = query.where(RaidItem.status == "open")
            return list(s.exec(query))

    def raid_item(self, item_id: int) -> RaidItem:
        with Session(self.engine) as s:
            item = s.get(RaidItem, item_id)
        if item is None:
            raise StatusError("That RAID item does not exist.")
        return item

    def save_raid(self, actor: str, item_id: int | None, **fields) -> RaidItem:
        kind, title = fields.get("kind", ""), fields.get("title", "").strip()
        if kind not in RAID_KINDS:
            raise StatusError("Choose risk, issue, assumption, or dependency.")
        if not title:
            raise StatusError("Give the item a title.")
        if fields.get("impact") not in LEVELS:
            raise StatusError("Impact must be low, medium, or high.")
        if kind == "risk" and fields.get("probability") not in LEVELS:
            raise StatusError("A risk needs a probability: low, medium, or high.")
        if kind != "risk":
            fields["probability"] = ""
        with Session(self.engine) as s:
            item = s.get(RaidItem, item_id) if item_id else RaidItem(kind=kind, product="",
                                                                      title=title)
            if item is None:
                raise StatusError("That RAID item does not exist.")
            changed = [k for k, v in fields.items() if getattr(item, k, None) != v]
            for k, v in fields.items():
                setattr(item, k, v.strip() if isinstance(v, str) else v)
            item.updated_by, item.updated_at = actor, utcnow()
            s.add(item)
            s.flush()
            audit(s, actor, "raid_updated" if item_id else "raid_added",
                  f"RAID-{item.id} {item.kind}: {item.title}"
                  + (f" (changed {', '.join(changed)})" if item_id and changed else ""))
            s.commit()
            s.refresh(item)
        return item

    def close_raid(self, item_id: int, actor: str, reason: str) -> None:
        with Session(self.engine) as s:
            item = s.get(RaidItem, item_id)
            if item is None:
                raise StatusError("That RAID item does not exist.")
            item.status, item.updated_by, item.updated_at = "closed", actor, utcnow()
            s.add(item)
            audit(s, actor, "raid_closed", f"RAID-{item.id} {item.title}"
                  + (f": {reason}" if reason else ""))
            s.commit()

    # ---------- assessment ----------

    def assessment(self) -> Assessment | None:
        snap = self.snapshot()
        if snap is None:
            return None
        budget = [BudgetRow(b.product, b.month, b.plan_k, b.actual_k) for b in self.budget()]
        raid = [RaidRow(i.key, i.kind, i.product, i.title, i.impact, i.probability, i.owner,
                        i.due, i.status, i.confirmed) for i in self.raid()]
        return assess(snap.portfolio, budget, raid)
