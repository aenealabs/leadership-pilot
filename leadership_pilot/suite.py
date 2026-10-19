"""Mission Control suite: Leadership Pilot's side of the link to Backlog Pilot.

Sends ranked problems to Backlog Pilot's intake inbox and proposes personas to its library.
Neither acts on its own there: a product owner drafts from an inbox item, and an approver
accepts or rejects each persona. Server to server, with a shared bearer token
(LP_SUITE_TOKEN, the same value as Backlog Pilot's BP_SUITE_TOKEN).
"""

import os
from dataclasses import dataclass

import httpx


class SuiteError(Exception):
    """Backlog Pilot could not be reached or refused the request; message is user-facing."""


@dataclass(frozen=True)
class SuiteSettings:
    backlog_url: str = ""  # where Backlog Pilot runs
    token: str = ""
    public_url: str = ""  # where this app runs, for links back from Backlog Pilot

    @property
    def connected(self) -> bool:
        return bool(self.backlog_url and self.token)


def load_suite_settings() -> SuiteSettings:
    return SuiteSettings(os.getenv("LP_BACKLOG_URL", "").rstrip("/"),
                         os.getenv("LP_SUITE_TOKEN", ""),
                         os.getenv("LP_PUBLIC_URL", "http://127.0.0.1:8010").rstrip("/"))


class SuiteClient:
    def __init__(self, settings: SuiteSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http = httpx.Client(base_url=settings.backlog_url or "http://unset", timeout=15.0,
                                 headers={"Authorization": f"Bearer {settings.token}"},
                                 transport=transport)

    def _post(self, path: str, body: dict) -> dict:
        if not self.settings.connected:
            raise SuiteError("Backlog Pilot is not connected. Set LP_BACKLOG_URL and "
                             "LP_SUITE_TOKEN.")
        try:
            response = self.http.post(path, json=body)
        except httpx.HTTPError as e:
            raise SuiteError(f"Could not reach Backlog Pilot: {e}") from e
        if response.status_code == 409:
            raise SuiteError(response.json().get("error", "Already exists in Backlog Pilot."))
        if not response.is_success:
            raise SuiteError(f"Backlog Pilot returned HTTP {response.status_code}.")
        return response.json()

    def send_problem(self, title: str, text: str, ref: str, stakeholders: list[str],
                     submitted_by: str) -> int:
        body = {"title": title, "text": text, "source_ref": ref,
                "source_url": f"{self.settings.public_url}/synthesis#{ref}",
                "stakeholders": stakeholders, "submitted_by": submitted_by,
                "source_app": "Leadership Pilot"}
        return self._post("/api/inbox", body)["id"]

    def propose_persona(self, name: str, description: str, goals: list[str],
                        pain_points: list[str], submitted_by: str) -> int:
        body = {"name": name, "description": description, "goals": goals,
                "pain_points": pain_points, "submitted_by": submitted_by}
        return self._post("/api/personas/proposals", body)["id"]

    def portfolio(self) -> dict:
        """Backlog Pilot's read-only roadmap view (plan, capacity, change log)."""
        if not self.settings.connected:
            raise SuiteError("Backlog Pilot is not connected. Set LP_BACKLOG_URL and "
                             "LP_SUITE_TOKEN.")
        try:
            response = self.http.get("/api/portfolio")
        except httpx.HTTPError as e:
            raise SuiteError(f"Could not reach Backlog Pilot: {e}") from e
        if not response.is_success:
            raise SuiteError(f"Backlog Pilot returned HTTP {response.status_code}.")
        return response.json()

    def inbox_status(self, item_id: int) -> dict | None:
        if not self.settings.connected:
            return None
        try:
            response = self.http.get(f"/api/inbox/{item_id}")
        except httpx.HTTPError:
            return None
        return response.json() if response.is_success else None
