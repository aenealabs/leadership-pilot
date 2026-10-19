# Leadership Pilot

Two tools for IT product leadership that share one stakeholder record:

1. **Listening-tour synthesizer** — turns interview notes into traced pain points, outcomes,
   and follow-ups; then themes, a ranked problem list, personas, and a 30-60-90 readout (M2).
2. **Status & risk narrator** — rule-based product status and early warnings, and a weekly
   leadership update with every sentence cited, delivered to Outlook, Teams, or Slack after
   approval (M3).

Part of **Mission Control**, a suite with [Backlog Pilot](https://github.com/aenealabs/backlog-pilot): same stack,
Brightlayer UI styling, and governance rules (AI drafts, people decide, every decision on
record; rules where trust matters, citations checked where the model speaks).

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m leadership_pilot.app      # http://127.0.0.1:8010
```

The demo seeds eight fictional stakeholders and their interview notes. Offline demo mode
replays recorded responses; live mode (`LP_LLM_PROVIDER=anthropic` or `foundry`) works on
any notes. For the offline synthesis, open Interviews and use **Extract all**, then
**Accept all extracted**, then **Synthesize** on the Synthesis page.

To connect to Backlog Pilot, run both apps with the same shared token
(`LP_SUITE_TOKEN` here, `BP_SUITE_TOKEN` there) and point each at the other
(`LP_BACKLOG_URL`, `BP_LEADERSHIP_URL`). Without them, each app runs standalone.

## Built-in Help

**Help** in the side menu explains every screen, and the **?** in the app bar opens the
section about the screen you are on. The **rules and settings reference** is generated from
the same constants the rules run on, so it always matches what the app does; tests fail if a
screen has no help section or a setting is undocumented. Every app also serves the shared
*How Mission Control works* guide (from `mission-control-common`).

Across the suite, overriding a proposed score, rating, placement, or fix, rejecting a draft,
or dropping anything the AI drafted needs a reason; rewording a draft keeps the AI's original
beside it. Every refused attempt is audited, including actions tried without signing in and
failed sign-ins.

## What M1 does

- **Stakeholder map:** name, role, business unit, products, and an influence/interest grid
  showing whose interview has been reviewed.
- **Interviews:** paste notes or a transcript. The model extracts pain points (with
  severity and systems), desired outcomes, systems named, urgency, and follow-up questions.
- **Traced to their words:** every pain point and outcome quotes the notes; code checks each
  quote against the notes and highlights it, flagging misquotes and AI inferences.
- **Review before keeping:** untick, retitle, or change severity, then accept into the
  stakeholder's record. The model's draft is kept for provenance; edits are counted and
  logged.
- **Sensitive by design:** interview notes, the interview list, and the audit log are
  restricted to the tour owner and approvers; denied attempts are logged.
- **Audit log** with full-text search and filters.

## What M2 adds

- **Synthesis across interviews:** the model groups every accepted pain point into themes,
  names where stakeholders disagree, and drafts personas, journeys, and a 30-60-90 plan.
  It sees labelled pain points only (`IV-3.2` = interview 3, pain point 2), never the notes
  or quotes, and every label it cites is checked against the record. Unknown labels are flagged.
- **Ranking is computed, not generated:** breadth (how many stakeholders, not how many
  mentions), worst severity, stakeholder influence, and strategic fit, each weighted, with the
  weights on screen and adjustable. Fit is the one judgment: the model suggests it with a
  reason, a person can override it, and overrides are logged.
- **Readout:** export as Markdown or Word, or copy an outline that pastes into PowerPoint's
  Outline view as slides. Quotes are included but not attributed by name.
- **Handoffs to Backlog Pilot:** send a problem to its intake inbox (with its quotes,
  unattributed) and see its live status there, or propose a persona to its library, where
  an approver accepts or rejects it.

## What M3 adds

- **Status computed by rules, not AI:** per product, schedule (roadmap moves, capacity load,
  blocked work), scope (new work added), budget (spend vs plan), and risk (RAID log). Each
  red or amber opens to show the rule and the numbered facts that triggered it.
- **Early warnings:** three or more moves out of one quarter in two weeks, spend more than
  10% ahead of plan, a high risk with no owner or past its date, an unconfirmed dependency
  due within 14 days.
- **Inputs:** Backlog Pilot's roadmap and change log (refreshed through the suite API, or a
  demo snapshot offline), a RAID log you edit here, and a budget CSV (plan vs actuals).
- **Weekly update with every sentence checked:** the model drafts over the facts; code
  checks that each sentence cites real facts, uses only numbers found in them, and never
  states a status (that comes from the rules). Flagged sentences block approval; every edit
  is a new version.
- **Delivery after approval:** Outlook (Microsoft Graph: saved as a draft by default, or
  sent), a Teams channel (Workflows webhook, Adaptive Card), or Slack (webhook or bot), in a
  summary version for executives and a detailed one for product leads, set per person on the
  distribution list. Simulated by default: the exact payload is recorded and nothing leaves
  the machine until `LP_DELIVERY_MODE=live`.

## What M4 adds

- **Upload interviews:** a text file, a Word document, or a Teams meeting transcript (.vtt).
  Transcripts lose their timestamps and become one line per turn ("Speaker: what they
  said"), so quote tracing works on the words.
- **Phone layout:** a menu button opens the navigation; pages fit a phone screen.
- **Hardening:** security headers on every response; sessions are HTTP-only, SameSite=Lax,
  and HTTPS-only in production.

## Sign-in across Mission Control

Both apps sign in with OpenID Connect (`LP_AUTH=oidc` here, `BP_AUTH=oidc` in Backlog Pilot).
For one sign-in across the suite, register both in the same Microsoft Entra ID tenant: one
app registration with both redirect URIs (`https://<leadership-host>/auth/callback` and
`https://<backlog-host>/auth/callback`), or one registration each. Each app keeps its own
approver role (`LeadershipPilot.Approver`, `BacklogPilot.Approver`), so a product owner can
approve backlog work without seeing candid interview notes. Server-to-server suite calls use
the shared `LP_SUITE_TOKEN`/`BP_SUITE_TOKEN`; in production, replace it with Entra
app-to-app tokens.

## Roadmap

- **M1 (done):** stakeholders, interviews, traced extraction, review, access control.
- **M2 (done):** themes, contradictions, ranked problems, personas and journeys,
  30-60-90 readout export (Markdown, Word, slide outline), handoffs to Backlog Pilot.
- **M3 (done):** status & risk narrator: Backlog Pilot read API, RAID log, budget
  import, rule-based status and early warnings, cited weekly update, delivery to Outlook,
  Teams, or Slack.
- **M4 (this release):** interview upload (text, Word, Teams transcript), phone layout,
  security headers, suite sign-in guidance. The demo video stays with Backlog Pilot.

## Data and branding

All people, interviews, and figures are fictional. The UI uses the open-source Brightlayer UI
color and theme values (BSD-3-Clause); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
This is an independent project, not affiliated with or endorsed by Eaton.

## License

Copyright (c) 2026 aenealabs. All rights reserved.
