"""A thin client for the Pulse HTTP API.

Pulse disables ``/openapi.json`` and ``/docs`` in production, so this module
doubles as the written-down contract for the endpoints the campaign runner
uses. Everything here maps one-to-one onto a route under
``backend/app/routers/`` — no logic lives in this file that is not about
speaking HTTP.

Two things about the API are easy to get wrong and are handled here:

* ``POST /auth/login`` takes **form-encoded** ``username``/``password``. A JSON
  body returns 422 "Field required", which reads like a bad password and is not.
* Every other endpoint takes and returns JSON, bearer-authenticated.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://herald.doaide.com/api/v1"


class HeraldError(RuntimeError):
    """An API call that came back with a non-2xx status.

    Carries the parsed ``detail`` where Pulse sent one, because that string is
    almost always the actionable part — "Not connected to: devto" is a fix, and
    "400 Bad Request" is not.
    """

    def __init__(self, method: str, url: str, status_code: int, detail: str):
        super().__init__(f"{method} {url} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HeraldClient:
    """Authenticated session against one Pulse instance."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """*transport* exists so the tests can answer requests without a network.

        Nothing in production passes it. It is the only injection point in this
        module, and it is here rather than a mocked-out ``httpx.request`` so the
        tests exercise the real client — the form encoding on login, the header
        handling, the 204 case — instead of a stand-in for it.
        """
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._token: str | None = None

    # ----------------------------------------------------------------- #
    # Plumbing                                                          #
    # ----------------------------------------------------------------- #

    def __enter__(self) -> HeraldClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise HeraldError("GET", self.base_url, 401, "not logged in")
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        response = self._http.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise HeraldError(method, url, response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ----------------------------------------------------------------- #
    # Auth                                                              #
    # ----------------------------------------------------------------- #

    def login(self, email: str, password: str) -> None:
        """Exchange credentials for a bearer token. Form-encoded, not JSON."""
        url = f"{self.base_url}/auth/login"
        response = self._http.post(url, data={"username": email, "password": password})
        if response.status_code >= 400:
            raise HeraldError("POST", url, response.status_code, _detail(response))
        self._token = response.json()["access_token"]

    def login_from_env(self, base_url_env: str = "HERALD_BASE_URL") -> None:
        """Log in from ``HERALD_EMAIL`` / ``HERALD_PASSWORD``.

        Kept separate from :meth:`login` so credentials never have to appear in
        a campaign file or on a command line.
        """
        email = os.environ.get("HERALD_EMAIL")
        password = os.environ.get("HERALD_PASSWORD")
        if not email or not password:
            raise SystemExit(
                "Set HERALD_EMAIL and HERALD_PASSWORD (and optionally "
                f"{base_url_env}) before running."
            )
        self.login(email, password)

    # ----------------------------------------------------------------- #
    # Projects                                                          #
    # ----------------------------------------------------------------- #

    def list_projects(self) -> list[dict]:
        return self._request("GET", "/projects")

    def create_project(self, payload: dict) -> dict:
        return self._request("POST", "/projects", json=payload)

    def update_project(self, project_id: int, payload: dict) -> dict:
        return self._request("PATCH", f"/projects/{project_id}", json=payload)

    # ----------------------------------------------------------------- #
    # Content                                                           #
    # ----------------------------------------------------------------- #

    def list_content(self, project_id: int | None = None, limit: int = 500) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if project_id is not None:
            params["project_id"] = project_id
        return self._request("GET", "/content", params=params)

    def get_content(self, content_id: int) -> dict:
        return self._request("GET", f"/content/{content_id}")

    def create_content(self, payload: dict) -> dict:
        return self._request("POST", "/content", json=payload)

    def update_content(self, content_id: int, payload: dict) -> dict:
        return self._request("PATCH", f"/content/{content_id}", json=payload)

    def generate_content(self, payload: dict) -> dict:
        """Draft a piece with the AI engine. Runs inline; allow ~20s."""
        return self._request("POST", "/content/generate", json=payload)

    def approve_content(self, content_id: int) -> dict:
        return self._request("POST", f"/content/{content_id}/approve")

    def check_links(self, content_id: int) -> dict:
        return self._request("GET", f"/content/{content_id}/links")

    def social_cards(self, content_id: int) -> dict:
        return self._request("GET", f"/content/{content_id}/social")

    def schedule_suggestions(self, content_id: int, platforms: list[str]) -> list[dict]:
        return self._request(
            "GET",
            f"/content/{content_id}/schedule/suggestions",
            params={"platforms": platforms},
        )

    def schedule_content(
        self,
        content_id: int,
        *,
        platforms: list[str] | None = None,
        scheduled_for: str | None = None,
        optimize: bool = False,
        as_draft: bool = False,
    ) -> list[dict]:
        """Put a piece on the calendar.

        ``optimize`` and ``scheduled_for`` are mutually exclusive — Pulse
        rejects both together. With ``optimize`` each platform gets its own
        slot, which is what staggers a cross-post instead of firing every copy
        into every feed in the same second.
        """
        payload: dict[str, Any] = {"optimize": optimize, "as_draft": as_draft}
        if platforms:
            payload["platforms"] = platforms
        if scheduled_for:
            payload["scheduled_for"] = scheduled_for
        return self._request("POST", f"/content/{content_id}/schedule", json=payload)

    def unschedule_content(self, content_id: int) -> list[dict]:
        return self._request("DELETE", f"/content/{content_id}/schedule")

    def publish_content(
        self,
        content_id: int,
        platforms: list[str],
        *,
        scheduled_for: str | None = None,
        as_draft: bool = False,
        allow_broken_links: bool = False,
    ) -> list[dict]:
        """Queue a piece to go out now (or at ``scheduled_for``).

        The response is the queue state, not the outcome — poll the
        publications to find out what the platforms said.
        """
        payload: dict[str, Any] = {
            "platforms": platforms,
            "as_draft": as_draft,
            "allow_broken_links": allow_broken_links,
        }
        if scheduled_for:
            payload["scheduled_for"] = scheduled_for
        return self._request("POST", f"/content/{content_id}/publish", json=payload)

    # ----------------------------------------------------------------- #
    # Settings, calendar, analytics                                     #
    # ----------------------------------------------------------------- #

    def platforms(self) -> list[dict]:
        """Every destination, whether an adapter exists, and the live connection."""
        return self._request("GET", "/settings/platforms")

    def connected_platforms(self) -> list[str]:
        return [
            p["platform"]
            for p in self.platforms()
            if (p.get("connection") or {}).get("status") == "connected"
        ]

    def calendar(self, start: str | None = None, end: str | None = None) -> dict:
        params = {k: v for k, v in (("start", start), ("end", end)) if v}
        return self._request("GET", "/calendar", params=params)

    def analytics_dashboard(self) -> dict:
        return self._request("GET", "/analytics/dashboard")


def _detail(response: httpx.Response) -> str:
    """The ``detail`` string Pulse sends, or the raw body if it sent none."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:500]


__all__ = ["HeraldClient", "HeraldError", "DEFAULT_BASE_URL"]
