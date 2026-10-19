"""Rule-based status and early warnings: red/amber/green is computed here, never by the model.

Inputs are a Backlog Pilot portfolio snapshot (roadmap plan and change log), the RAID log,
and budget plan vs actuals. Every status and warning carries the facts that triggered it, and
each fact is a numbered, plain-language statement (F-1, F-2, ...) that the narrative must cite.
The thresholds live in RULES so the rule text shown next to a status is the rule applied.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

GREEN, AMBER, RED = "green", "amber", "red"
LEVEL_ORDER = {GREEN: 0, AMBER: 1, RED: 2}
DIMENSIONS = ("schedule", "scope", "budget", "risk")

# Every threshold the status rules use. The rule text below, the status screen, and the Help
# reference are built from these, so changing one changes what the rules do and say.
MOVES_WINDOW_DAYS = 14  # features moved later within this many days count
MOVES_RED = 3  # this many moves make schedule red (fewer: amber); also the slip warning
LOAD_AMBER_PCT, LOAD_RED_PCT = 100, 120  # current-quarter load as a share of capacity
SCOPE_WINDOW_DAYS = 28  # new features added within this many days count as scope growth
SCOPE_AMBER_PCT, SCOPE_RED_PCT = 25, 50  # growth as a share of current-quarter capacity
BUDGET_AMBER_PCT, BUDGET_RED_PCT = 5, 10  # spend over plan
DEPENDENCY_DAYS = 14  # an unconfirmed dependency due within this many days is amber

_WORDS = {3: "Three", 14: "two weeks", 28: "four weeks"}


def _span(days: int) -> str:
    return _WORDS.get(days, f"{days} days")


RULES = {
    "moves": f"Features moved later out of a quarter in the last {MOVES_WINDOW_DAYS} days: "
             f"1-{MOVES_RED - 1} amber, {MOVES_RED} or more red",
    "load": f"Current-quarter load over capacity: over {LOAD_AMBER_PCT}% amber, over "
            f"{LOAD_RED_PCT}% red",
    "blocked": "A blocked feature planned for the current quarter: amber",
    "scope": f"Features added in the last {SCOPE_WINDOW_DAYS} days, as a share of "
             f"current-quarter capacity: over {SCOPE_AMBER_PCT}% amber, over {SCOPE_RED_PCT}% red",
    "budget": f"Spend to date against plan for the same months: over {BUDGET_AMBER_PCT}% amber, "
              f"over {BUDGET_RED_PCT}% red",
    "risk": "Open high risk (high impact, medium or high probability) or high-impact issue: "
            "amber; with no owner or past its date: red",
    "dependency": f"A dependency due within {DEPENDENCY_DAYS} days and not confirmed: amber",
}
WARNINGS = {
    "slip": f"{_WORDS.get(MOVES_RED, MOVES_RED)} or more features moved out of the same quarter "
            f"in {_span(MOVES_WINDOW_DAYS)}",
    "burn": f"Budget burn more than {BUDGET_RED_PCT}% ahead of plan",
    "orphan": "High risk with no owner or past its date",
    "dependency": f"Dependency due within {DEPENDENCY_DAYS} days and not confirmed",
}


@dataclass
class Fact:
    id: str
    product: str  # product key, or "" for portfolio-wide
    text: str
    source: str


@dataclass
class Dimension:
    level: str = GREEN
    reasons: list[tuple[str, list[str]]] = field(default_factory=list)  # (rule text, fact ids)

    def raise_to(self, level: str, rule: str, fact_ids: list[str]) -> None:
        if LEVEL_ORDER[level] > LEVEL_ORDER[self.level]:
            self.level = level
        self.reasons.append((rule, fact_ids))


@dataclass
class ProductStatus:
    key: str
    name: str
    dims: dict[str, Dimension]
    fact_ids: list[str]

    @property
    def overall(self) -> str:
        return max((d.level for d in self.dims.values()), key=LEVEL_ORDER.__getitem__)


@dataclass
class Warning:
    kind: str
    product: str
    title: str
    fact_ids: list[str]


@dataclass
class Assessment:
    as_of: date
    quarter: str
    source: str
    products: list[ProductStatus]
    warnings: list[Warning]
    facts: list[Fact]

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    @property
    def fact_map(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}


@dataclass
class BudgetRow:
    product: str
    month: str  # YYYY-MM
    plan_k: float
    actual_k: float | None


@dataclass
class RaidRow:
    key: str
    kind: str  # risk | issue | assumption | dependency
    product: str
    title: str
    impact: str
    probability: str
    owner: str
    due: date | None
    status: str  # open | closed
    confirmed: bool = False


# ---------- formatting (facts must carry the exact numbers a sentence may use) ----------

def q_label(quarter: str) -> str:
    """'2026-Q4' -> 'Q4 2026'; '' -> 'unplanned'."""
    if not quarter:
        return "unplanned"
    year, q = quarter.split("-")
    return f"{q} {year}"


def d_label(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def money(k: float) -> str:
    return f"${k:,.0f}K"


def pct(x: float) -> str:
    return f"{x:.0f}%"


# ---------- the assessment ----------

def assess(snapshot: dict, budget: list[BudgetRow], raid: list[RaidRow]) -> Assessment:
    as_of = date.fromisoformat(snapshot["as_of"])
    quarter = snapshot["quarters"][0]
    facts: list[Fact] = []

    def fact(product: str, text: str, source: str) -> str:
        f = Fact(f"F-{len(facts) + 1}", product, text, source)
        facts.append(f)
        return f.id

    statuses, warnings = [], []
    for p in snapshot["products"]:
        key, dims = p["key"], {d: Dimension() for d in DIMENSIONS}
        mine: list[str] = []

        # schedule: plan for the current quarter
        current = [f for f in p["features"] if f["quarter"] == quarter]
        cap, load = p["capacity"].get(quarter, 0), p["load"].get(quarter, 0)
        in_progress = sum(f["status"] == "In Progress" for f in current)
        share = 100 * load / cap if cap else 0
        n = len(current)
        plan_id = fact(key, f"{p['name']}: {n} feature{'' if n == 1 else 's'} planned for "
                            f"{q_label(quarter)}"
                            f" ({in_progress} in progress); load {load} of {cap} capacity points"
                            f" ({pct(share)}).", "Backlog Pilot roadmap")
        mine.append(plan_id)
        if share > LOAD_RED_PCT:
            dims["schedule"].raise_to(RED, RULES["load"], [plan_id])
        elif share > LOAD_AMBER_PCT:
            dims["schedule"].raise_to(AMBER, RULES["load"], [plan_id])
        for f in current:
            if f["status"] == "Blocked":
                fid = fact(key, f"{f['key']} {f['title']} is blocked and planned for "
                                f"{q_label(quarter)}.", "Backlog Pilot roadmap")
                mine.append(fid)
                dims["schedule"].raise_to(AMBER, RULES["blocked"], [fid])

        # schedule: moves later in the last MOVES_WINDOW_DAYS days
        later_by_quarter: dict[str, list[str]] = {}
        added: list[tuple[str, int]] = []
        for c in p["changes"]:
            at = datetime.fromisoformat(c["changed_at"]).date()
            age = (as_of - at).days
            if 0 <= age <= MOVES_WINDOW_DAYS and c["from"] not in ("", "new") and \
                    (c["to"] == "" or (c["to"] not in ("removed",) and c["to"] > c["from"])):
                fid = fact(key, f"{c['feature_key']} {c['feature_title']} moved from "
                                f"{q_label(c['from'])} to {q_label(c['to'])} on {d_label(at)}"
                                f" ({c['cause']}; by {c['changed_by']}).",
                           "Backlog Pilot roadmap change log")
                mine.append(fid)
                later_by_quarter.setdefault(c["from"], []).append(fid)
            if 0 <= age <= SCOPE_WINDOW_DAYS and c["from"] == "new":
                size = next((f["size"] for f in p["features"] if f["key"] == c["feature_key"]),
                            None) or 0
                fid = fact(key, f"{c['feature_key']} {c['feature_title']} ({size} points) was "
                                f"added on {d_label(at)} ({c['cause']}).",
                           "Backlog Pilot roadmap change log")
                mine.append(fid)
                added.append((fid, size))
        moved_ids = [i for ids in later_by_quarter.values() for i in ids]
        if len(moved_ids) >= MOVES_RED:
            dims["schedule"].raise_to(RED, RULES["moves"], moved_ids)
        elif moved_ids:
            dims["schedule"].raise_to(AMBER, RULES["moves"], moved_ids)
        for q, ids in later_by_quarter.items():
            if len(ids) >= MOVES_RED:
                title = (f"{p['name']}: {len(ids)} features moved out of {q_label(q)} in "
                         f"{_span(MOVES_WINDOW_DAYS)}")
                warnings.append(Warning("slip", key, title, ids))

        # scope: growth in the last SCOPE_WINDOW_DAYS days
        if added and cap:
            points = sum(s for _, s in added)
            growth = 100 * points / cap
            gid = fact(key, f"{p['name']}: {points} points of new features added in the last "
                            f"{SCOPE_WINDOW_DAYS} days, {pct(growth)} of {q_label(quarter)} "
                            f"capacity ({cap} points).",
                       "Backlog Pilot roadmap change log")
            mine.append(gid)
            ids = [gid] + [i for i, _ in added]
            if growth > SCOPE_RED_PCT:
                dims["scope"].raise_to(RED, RULES["scope"], ids)
            elif growth > SCOPE_AMBER_PCT:
                dims["scope"].raise_to(AMBER, RULES["scope"], ids)

        # budget: spend to date against plan for the months with actuals
        rows = [b for b in budget if b.product == key and b.actual_k is not None]
        if rows:
            plan_k = sum(b.plan_k for b in rows)
            actual_k = sum(b.actual_k or 0 for b in rows)
            over = 100 * (actual_k - plan_k) / plan_k if plan_k else 0
            months = sorted(b.month for b in rows)
            span = f"{_month(months[0])} to {_month(months[-1])}"
            word = "over" if over >= 0 else "under"
            bid = fact(key, f"{p['name']}: spent {money(actual_k)} against a plan of "
                            f"{money(plan_k)} for {span}, {pct(abs(over))} {word} plan.",
                       "Budget plan vs actuals")
            mine.append(bid)
            if over > BUDGET_RED_PCT:
                dims["budget"].raise_to(RED, RULES["budget"], [bid])
                warnings.append(Warning("burn", key, f"{p['name']}: spend {pct(over)} ahead "
                                                     "of plan", [bid]))
            elif over > BUDGET_AMBER_PCT:
                dims["budget"].raise_to(AMBER, RULES["budget"], [bid])

        # risk: RAID log
        for r in raid:
            if r.product != key or r.status != "open":
                continue
            owner = r.owner or "no owner"
            due = f"due {d_label(r.due)}" if r.due else "no date"
            late = r.due is not None and r.due < as_of
            if r.kind == "risk":
                rid = fact(key, f"Risk {r.key}: {r.title} (impact {r.impact}, probability "
                                f"{r.probability}; {owner}; mitigation {due}"
                                f"{', overdue' if late else ''}).", "RAID log")
                mine.append(rid)
                if r.impact == "high" and r.probability in ("medium", "high"):
                    if not r.owner or late:
                        dims["risk"].raise_to(RED, RULES["risk"], [rid])
                        warnings.append(Warning("orphan", key, f"{r.key} {r.title}: "
                                                + ("no owner" if not r.owner else
                                                   f"mitigation overdue since {d_label(r.due)}"),
                                                [rid]))
                    else:
                        dims["risk"].raise_to(AMBER, RULES["risk"], [rid])
            elif r.kind == "issue":
                iid = fact(key, f"Issue {r.key}: {r.title} (impact {r.impact}; {owner}; {due}).",
                           "RAID log")
                mine.append(iid)
                if r.impact == "high":
                    dims["risk"].raise_to(AMBER, RULES["risk"], [iid])
            elif r.kind == "dependency":
                state = "confirmed" if r.confirmed else "not confirmed"
                did = fact(key, f"Dependency {r.key}: {r.title} ({due}, {state}; {owner}).",
                           "RAID log")
                mine.append(did)
                soon = r.due is not None and (r.due - as_of) <= timedelta(days=DEPENDENCY_DAYS)
                if soon and not r.confirmed:
                    dims["risk"].raise_to(AMBER, RULES["dependency"], [did])
                    warnings.append(Warning("dependency", key, f"{r.key} {r.title}: due "
                                                               f"{d_label(r.due)}, not confirmed",
                                            [did]))
            elif r.kind == "assumption":
                mine.append(fact(key, f"Assumption {r.key}: {r.title} ({owner}; check by "
                                      f"{d_label(r.due) if r.due else 'no date'}).", "RAID log"))
        statuses.append(ProductStatus(key, p["name"], dims, mine))

    order = {"slip": 0, "burn": 1, "orphan": 2, "dependency": 3}
    warnings.sort(key=lambda w: order[w.kind])
    return Assessment(as_of, quarter, snapshot.get("source", "Backlog Pilot"), statuses,
                      warnings, facts)


def _month(ym: str) -> str:
    return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def narrative_input(a: Assessment) -> str:
    """What the model sees: computed statuses, warnings, and the numbered facts."""
    lines = [f"As of {d_label(a.as_of)}. Current quarter: {q_label(a.quarter)}.", "",
             "Computed status (rules, not yours to change):"]
    for p in a.products:
        dims = ", ".join(f"{d} {p.dims[d].level}" for d in DIMENSIONS)
        lines.append(f"- {p.key} | {p.name} | overall {p.overall} | {dims}")
    lines += ["", "Early warnings:"]
    lines += [f"- {w.product} | {w.title} | {', '.join(w.fact_ids)}" for w in a.warnings] or \
        ["- none"]
    lines += ["", "Facts:"]
    lines += [f"{f.id} | {f.product or 'portfolio'} | {f.text}" for f in a.facts]
    return "\n".join(lines)
