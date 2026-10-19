"""The Help reference: rules, checks, and settings, built from the code that uses them.

Status thresholds come from rules.py, ranking weights from synthesis.py, and the update
checks from updates.py, so the reference cannot say something the app does not do. SETTINGS
documents every environment setting; a test fails if the code reads one not listed here.
"""

from mission_control import ReferenceSection

from leadership_pilot import rules as r
from leadership_pilot.delivery import CHANNELS
from leadership_pilot.synthesis import DEFAULT_WEIGHTS, LEVEL_WEIGHT, SEVERITY_WEIGHT
from leadership_pilot.transcripts import MAX_BYTES

SETTINGS = {
    "LP_DB_PATH": "Where the app keeps its database (SQLite file).",
    "LP_LLM_PROVIDER": "replay (offline demo, recorded responses), anthropic (Claude API), or "
                       "foundry (Claude on Microsoft Foundry).",
    "LP_MODEL": "The Claude model (or Foundry deployment).",
    "LP_RECORD": "true to save live responses as recordings for the offline demo.",
    "ANTHROPIC_API_KEY": "Claude API key, when LP_LLM_PROVIDER=anthropic.",
    "ANTHROPIC_FOUNDRY_RESOURCE": "The Microsoft Foundry resource, when LP_LLM_PROVIDER=foundry.",
    "ANTHROPIC_FOUNDRY_API_KEY": "Key for Claude on Microsoft Foundry; without it, Azure sign-in "
                                 "(managed identity) is used.",
    "LP_AUTH": "demo (pick a fictional person) or oidc (single sign-on).",
    "LP_OIDC_METADATA_URL": "Your identity provider's OpenID Connect metadata address.",
    "LP_OIDC_CLIENT_ID": "The app registration's client ID.",
    "LP_OIDC_CLIENT_SECRET": "The app registration's client secret.",
    "LP_SESSION_SECRET": "A long random value that signs the sign-in cookie.",
    "LP_SESSION_HTTPS_ONLY": "false only for local testing over plain http.",
    "LP_APPROVER_ROLE": "The app role for the tour owner and approvers.",
    "LP_APPROVER_EMAILS": "Comma-separated emails that are approvers even without the role.",
    "LP_BACKLOG_URL": "Backlog Pilot's address, to send problems and personas and read its "
                      "roadmap.",
    "LP_SUITE_TOKEN": "The shared Mission Control token; the same value as BP_SUITE_TOKEN.",
    "LP_PUBLIC_URL": "This app's own address, so items sent to Backlog Pilot link back here.",
    "LP_DELIVERY_MODE": "simulate (default: record the exact payload, send nothing) or live.",
    "LP_OUTLOOK_MODE": "draft (save in the sender's Drafts) or send.",
    "LP_OUTLOOK_SENDER": "The mailbox the update is sent from.",
    "LP_GRAPH_TENANT_ID": "Microsoft Entra tenant, for Outlook through Microsoft Graph.",
    "LP_GRAPH_CLIENT_ID": "App registration with Mail.ReadWrite (drafts) or Mail.Send, for "
                          "Outlook.",
    "LP_GRAPH_CLIENT_SECRET": "That registration's client secret.",
    "LP_TEAMS_WEBHOOK_URL": "A Teams Workflows webhook for the channel.",
    "LP_SLACK_WEBHOOK_URL": "A Slack incoming webhook (or use the bot token and channel).",
    "LP_SLACK_BOT_TOKEN": "A Slack bot token, with LP_SLACK_CHANNEL.",
    "LP_SLACK_CHANNEL": "The Slack channel for the bot.",
    "MISSION_CONTROL_APPS": "The suite's apps and addresses, the same in every app.",
}


def reference() -> list[ReferenceSection]:
    w = DEFAULT_WEIGHTS
    return [
        ReferenceSection("Status rules", [(k.capitalize(), v + ".") for k, v in r.RULES.items()],
                         intro="Red, amber, and green are computed by these rules, never by the "
                               "AI. Each status opens to show the rule and the numbered facts "
                               "that triggered it. A product's overall status is its worst "
                               "dimension."),
        ReferenceSection("Early warnings", [(k.capitalize(), v + ".")
                                            for k, v in r.WARNINGS.items()],
                         columns=("Warning", "Raised when")),
        ReferenceSection("Problem ranking", [
            ("Breadth", f"Share of interviewed stakeholders who raised it (weight {w['breadth']})"),
            ("Severity", "Worst severity among its pain points: " + ", ".join(
                f"{k} {v}" for k, v in SEVERITY_WEIGHT.items()) + f" (weight {w['severity']})"),
            ("Influence", "Highest influence among those who raised it: " + ", ".join(
                f"{k} {v}" for k, v in LEVEL_WEIGHT.items()) + f" (weight {w['influence']})"),
            ("Strategic fit", f"1 to 5; the AI suggests it with a reason, a person can override "
                              f"it with a reason (weight {w['fit']})"),
            ("Score", "Each part scaled to 0-1, weighted, as 0-100; weights are on screen and "
                      "adjustable"),
        ], columns=("Part", "How it is counted"),
            intro="The AI groups pain points into themes; code counts who raised what and "
                  "computes the ranking."),
        ReferenceSection("Weekly update checks", [
            ("Citation", "Every sentence cites at least one fact"),
            ("Known facts", "Every cited fact exists in this week's data"),
            ("Numbers", "Every number in a sentence appears in the facts it cites"),
            ("No status words", "A sentence never states red, amber, green, or yellow; the "
                                "rules show status beside the text"),
            ("Approval", "A sentence with a problem blocks approval; every edit is a new "
                         "version"),
        ], columns=("Check", "Passes when")),
        ReferenceSection("Interviews", [
            ("Quotes", "Every pain point and outcome quotes the notes; code finds each quote, "
                       "ignoring case, spacing, and surrounding punctuation"),
            ("Changing a severity", "Needs a reason, since severity feeds the ranking"),
            ("Dropping extracted items", "Needs a reason; they came from what the stakeholder "
                                         "said"),
            ("Rewording", "No reason; the AI's extraction is kept and the change is logged"),
            ("Uploads", f"Text, Word (.docx), or a Teams transcript (.vtt), up to "
                        f"{MAX_BYTES // 1_000_000} MB"),
            ("Access", "Notes, interviews, synthesis, updates, and the audit log are for the "
                       "tour owner and approvers only"),
        ], columns=("Rule", "How it works")),
        ReferenceSection("Delivery", [(name, "After approval only; simulated unless "
                                             "LP_DELIVERY_MODE=live")
                                      for name in CHANNELS.values()],
                         columns=("Channel", "When")),
        ReferenceSection("Settings", sorted(SETTINGS.items()),
                         columns=("Setting", "What it does"),
                         intro="Environment settings (or a .env file next to the app)."),
    ]
