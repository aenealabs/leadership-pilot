"""Structured output for interview extraction (also what the review screen edits)."""

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]


class PainPoint(BaseModel):
    title: str = Field(description="Short name for the problem, e.g. 'ECO status is invisible'.")
    description: str = Field(description="One or two sentences on the problem and its impact.")
    severity: Severity = Field(description="Business impact as the stakeholder describes it.")
    systems: list[str] = Field(description="Systems or tools involved, as named (PLM, SAP, "
                               "CAD vault...). Empty if none were named.")
    quotes: list[str] = Field(description="Exact, verbatim excerpts of the notes that support "
                              "this pain point, copied character for character. Never "
                              "paraphrase. Empty only if the point is your inference.")


class Outcome(BaseModel):
    description: str = Field(description="What the stakeholder wants to be true.")
    measure: str = Field(description="How success would be measured, if they said; else empty.")
    quotes: list[str] = Field(description="Exact, verbatim supporting excerpts of the notes.")


class InterviewExtraction(BaseModel):
    summary: str = Field(description="Two or three sentences: who this is and what matters "
                         "most to them.")
    pain_points: list[PainPoint]
    desired_outcomes: list[Outcome]
    systems_named: list[str] = Field(description="Every system, tool, or platform mentioned.")
    urgency: Literal["low", "medium", "high"] = Field(
        description="How urgent the stakeholder's needs are overall.")
    follow_up_questions: list[str] = Field(
        description="Questions to ask this stakeholder next time, where notes were unclear.")


# ---------- cross-interview synthesis ----------

class Theme(BaseModel):
    title: str = Field(description="Short problem statement, e.g. 'Engineering change is slow "
                       "and opaque'.")
    description: str = Field(description="Two sentences: the problem and why it matters.")
    concern_ids: list[str] = Field(description="Labels of every pain point in this theme, "
                                   "exactly as given (e.g. IV-3.2).")
    strategic_fit: int = Field(ge=1, le=5, description="1-5: how directly solving this serves "
                               "Product Engineering & R&D IT priorities (modernization, "
                               "compliance, engineering productivity).")
    fit_rationale: str = Field(description="One sentence explaining the strategic fit score.")


class Contradiction(BaseModel):
    topic: str = Field(description="What stakeholders disagree about.")
    side_a: str = Field(description="One position, in a sentence.")
    side_a_ids: list[str] = Field(description="Pain point labels supporting side A.")
    side_b: str = Field(description="The opposing position, in a sentence.")
    side_b_ids: list[str] = Field(description="Pain point labels supporting side B.")
    why_it_matters: str = Field(description="One sentence: what a product owner must resolve.")


class PersonaDraft(BaseModel):
    name: str = Field(description="Role name in lower case, e.g. 'manufacturing engineer'.")
    description: str
    goals: list[str]
    pain_points: list[str]
    based_on: list[str] = Field(description="Pain point labels this persona is grounded in.")


class JourneyStep(BaseModel):
    step: str = Field(description="Stage name, e.g. 'Submit the change'.")
    what_happens: str
    pain_ids: list[str] = Field(description="Pain point labels felt at this step, if any.")


class Journey(BaseModel):
    persona: str
    title: str = Field(description="e.g. 'Getting an engineering change released'.")
    steps: list[JourneyStep]


class PlanItem(BaseModel):
    horizon: Literal["30", "60", "90"] = Field(description="Days: 30, 60, or 90.")
    action: str = Field(description="A concrete action the new leader will take.")
    theme_titles: list[str] = Field(description="Titles of the themes this action addresses.")


class Synthesis(BaseModel):
    themes: list[Theme]
    contradictions: list[Contradiction]
    personas: list[PersonaDraft]
    journeys: list[Journey]
    plan: list[PlanItem]
    headline: str = Field(description="One sentence: the single most important thing heard.")


# ---------- weekly status update (M3) ----------

class CitedSentence(BaseModel):
    text: str = Field(description="One sentence. Every number in it must appear in a cited "
                      "fact.")
    fact_ids: list[str] = Field(description="Fact ids that support the sentence, e.g. F-3.")


class ProductNarrative(BaseModel):
    product_key: str = Field(description="The product key exactly as given, e.g. plm-sap.")
    sentences: list[CitedSentence] = Field(description="2-4 sentences: what changed, why, "
                                           "and what happens next. Lead with the most "
                                           "important point.")


class WeeklyUpdateDraft(BaseModel):
    subject: str = Field(description="Email subject line, e.g. 'Product Engineering IT "
                         "weekly update: Oct 16, 2026'.")
    summary: list[CitedSentence] = Field(description="2-4 sentences for a VP: the overall "
                                         "picture and the one or two things that matter most.")
    products: list[ProductNarrative] = Field(description="One entry per product, in the "
                                             "order given.")
    decisions: list[CitedSentence] = Field(description="Decisions or help needed from "
                                           "leadership, each naming who must decide and by "
                                           "when if the facts give a date. Empty if none.")
    risks: list[CitedSentence] = Field(description="Up to 3 risks leadership should watch, "
                                       "most serious first.")
