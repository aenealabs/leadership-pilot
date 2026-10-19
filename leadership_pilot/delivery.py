"""Deliver an approved update to Outlook, Microsoft Teams, or Slack.

Delivery is simulated unless LP_DELIVERY_MODE=live: the exact payload is recorded and shown,
and nothing leaves the machine. In live mode each channel needs its own configuration:

- Outlook (Microsoft Graph, app registration with Mail.ReadWrite or Mail.Send):
  LP_GRAPH_TENANT_ID, LP_GRAPH_CLIENT_ID, LP_GRAPH_CLIENT_SECRET, LP_OUTLOOK_SENDER, and
  LP_OUTLOOK_MODE=draft (default: saved to the sender's Drafts to review and send in
  Outlook) or send.
- Teams: LP_TEAMS_WEBHOOK_URL, a Teams Workflows "post to a channel when a webhook request
  is received" URL. The post is an Adaptive Card.
- Slack: LP_SLACK_WEBHOOK_URL (incoming webhook), or LP_SLACK_BOT_TOKEN with
  LP_SLACK_CHANNEL (chat.postMessage).
"""

import json
import os
from dataclasses import dataclass

import httpx

from leadership_pilot.updates import Rendered

CHANNELS = {"outlook": "Outlook email", "teams": "Teams channel", "slack": "Slack channel"}
GRAPH = "https://graph.microsoft.com/v1.0"


class DeliveryError(Exception):
    """Delivery failed or is not configured; the message is user-facing."""


@dataclass(frozen=True)
class DeliverySettings:
    mode: str = "simulate"  # simulate | live
    graph_tenant: str = ""
    graph_client_id: str = ""
    graph_secret: str = ""
    outlook_sender: str = ""
    outlook_mode: str = "draft"  # draft | send
    teams_webhook: str = ""
    slack_webhook: str = ""
    slack_token: str = ""
    slack_channel: str = ""

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def configured(self, channel: str) -> bool:
        if channel == "outlook":
            return all((self.graph_tenant, self.graph_client_id, self.graph_secret,
                        self.outlook_sender))
        if channel == "teams":
            return bool(self.teams_webhook)
        if channel == "slack":
            return bool(self.slack_webhook or (self.slack_token and self.slack_channel))
        return False

    def target(self, channel: str) -> str:
        if channel == "teams":
            return "Teams channel (Workflows webhook)"
        if channel == "slack":
            return self.slack_channel or "Slack channel (incoming webhook)"
        return self.outlook_sender


def load_delivery_settings() -> DeliverySettings:
    return DeliverySettings(
        mode=os.getenv("LP_DELIVERY_MODE", "simulate").strip().lower(),
        graph_tenant=os.getenv("LP_GRAPH_TENANT_ID", ""),
        graph_client_id=os.getenv("LP_GRAPH_CLIENT_ID", ""),
        graph_secret=os.getenv("LP_GRAPH_CLIENT_SECRET", ""),
        outlook_sender=os.getenv("LP_OUTLOOK_SENDER", ""),
        outlook_mode=os.getenv("LP_OUTLOOK_MODE", "draft").strip().lower(),
        teams_webhook=os.getenv("LP_TEAMS_WEBHOOK_URL", ""),
        slack_webhook=os.getenv("LP_SLACK_WEBHOOK_URL", ""),
        slack_token=os.getenv("LP_SLACK_BOT_TOKEN", ""),
        slack_channel=os.getenv("LP_SLACK_CHANNEL", ""),
    )


# ---------- payloads (pure, so the simulated record is exactly what live would send) ----------

def outlook_message(r: Rendered, recipients: list[str]) -> dict:
    return {"subject": r.subject, "body": {"contentType": "HTML", "content": r.html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recipients]}


def teams_card(r: Rendered) -> dict:
    blocks = [{"type": "TextBlock", "text": r.subject, "weight": "Bolder", "size": "Medium",
               "wrap": True}]
    for para in r.text.split("\n\n")[1:]:
        blocks.append({"type": "TextBlock", "text": para.replace("\n", "\n\n"), "wrap": True,
                       "spacing": "Medium"})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": blocks}}]}


def slack_message(r: Rendered, channel: str = "") -> dict:
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": r.subject[:150]}}]
    for para in r.text.split("\n\n")[1:]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": para[:3000]}})
    msg = {"text": r.subject, "blocks": blocks}
    return msg | ({"channel": channel} if channel else {})


# ---------- live senders ----------

@dataclass
class Sent:
    status: str  # sent | drafted
    message_id: str
    detail: str = ""


class Deliverer:
    def __init__(self, settings: DeliverySettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(timeout=20.0, transport=transport)

    def payload(self, channel: str, r: Rendered, recipients: list[str]) -> dict:
        if channel == "outlook":
            return outlook_message(r, recipients)
        if channel == "teams":
            return teams_card(r)
        if channel == "slack":
            return slack_message(r, self.settings.slack_channel)
        raise DeliveryError(f"Unknown channel {channel!r}.")

    def send(self, channel: str, payload: dict) -> Sent:
        if not self.settings.configured(channel):
            raise DeliveryError(f"{CHANNELS[channel]} is not configured; see .env.example.")
        try:
            if channel == "outlook":
                return self._outlook(payload)
            if channel == "teams":
                return self._teams(payload)
            return self._slack(payload)
        except httpx.HTTPError as e:
            raise DeliveryError(f"Could not reach {CHANNELS[channel]}: {e}") from e

    def _graph_token(self) -> str:
        s = self.settings
        response = self.http.post(
            f"https://login.microsoftonline.com/{s.graph_tenant}/oauth2/v2.0/token",
            data={"client_id": s.graph_client_id, "client_secret": s.graph_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"})
        if not response.is_success:
            raise DeliveryError(f"Microsoft sign-in failed (HTTP {response.status_code}). "
                                "Check the Graph app registration settings.")
        return response.json()["access_token"]

    def _outlook(self, message: dict) -> Sent:
        s = self.settings
        headers = {"Authorization": f"Bearer {self._graph_token()}"}
        base = f"{GRAPH}/users/{s.outlook_sender}"
        if s.outlook_mode == "send":
            response = self.http.post(f"{base}/sendMail", headers=headers,
                                      json={"message": message, "saveToSentItems": True})
            if response.status_code != 202:
                raise DeliveryError(f"Outlook did not send the message (HTTP "
                                    f"{response.status_code}).")
            return Sent("sent", "", "sent with Graph sendMail")
        response = self.http.post(f"{base}/messages", headers=headers, json=message)
        if response.status_code != 201:
            raise DeliveryError(f"Outlook did not save the draft (HTTP "
                                f"{response.status_code}).")
        return Sent("drafted", response.json().get("id", ""),
                    f"saved to {s.outlook_sender}'s Drafts for review in Outlook")

    def _teams(self, card: dict) -> Sent:
        response = self.http.post(self.settings.teams_webhook, json=card)
        if response.status_code not in (200, 202):
            raise DeliveryError(f"Teams rejected the post (HTTP {response.status_code}).")
        return Sent("sent", response.headers.get("x-ms-workflow-run-id", ""))

    def _slack(self, message: dict) -> Sent:
        s = self.settings
        if s.slack_webhook:
            body = {k: v for k, v in message.items() if k != "channel"}
            response = self.http.post(s.slack_webhook, json=body)
            if response.status_code != 200 or response.text.strip() != "ok":
                raise DeliveryError(f"Slack rejected the post (HTTP {response.status_code}: "
                                    f"{response.text.strip()[:80]}).")
            return Sent("sent", "")
        response = self.http.post("https://slack.com/api/chat.postMessage", json=message,
                                  headers={"Authorization": f"Bearer {s.slack_token}"})
        data = response.json() if response.is_success else {}
        if not data.get("ok"):
            reason = data.get("error", response.status_code)
            raise DeliveryError(f"Slack rejected the post: {reason}.")
        return Sent("sent", data.get("ts", ""), f"channel {data.get('channel', '')}")


def pretty(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)
