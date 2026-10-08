"""An in-memory stand-in for the Pulse API, for the campaign runner's tests.

Deliberately not a mock. The bugs this suite exists to catch are all about what
the runner does with what the API *actually sends back* — a server-generated
excerpt, a keyword list capped at eight, a piece that is already published — and
a mock that returns whatever it was told to return cannot express any of them.

So this reimplements the handful of behaviours the runner depends on, and the
tests assert against the state it ends up in. Where it copies a rule from the
backend (keyword normalisation, the published-content lock) the rule is named in
a comment so the two can be kept in step.
"""
from __future__ import annotations

import re
from typing import Any


class FakeHeraldError(RuntimeError):
    """Stands in for ``herald_client.HeraldError``."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


#: Pulse caps stored keywords at eight — ``app.services.seo.KEYWORD_MAX``.
KEYWORD_MAX = 8


def normalize_keywords(keywords: list[str]) -> list[str]:
    """The backend's rule: lowercase, collapse space, dedupe, drop noise, cap."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in keywords:
        keyword = re.sub(r"\s+", " ", str(raw)).strip().lower().strip("#,.")
        if len(keyword) < 2 or keyword.isdigit() or keyword in seen:
            continue
        seen.add(keyword)
        out.append(keyword)
    return out[:KEYWORD_MAX]


def _excerpt_from(body: str) -> str:
    """What Pulse writes into an empty excerpt: the first real paragraph."""
    for block in body.split("\n\n"):
        text = block.strip().lstrip("#").strip()
        if text:
            return text[:220]
    return ""


class FakePulse:
    """Enough of Pulse to run a campaign against, plus a call log."""

    def __init__(self, *, connected: list[str] | None = None) -> None:
        self.projects: list[dict] = []
        self.content: list[dict] = []
        self.connected = connected if connected is not None else ["devto", "bluesky"]
        #: Every method call, in order. The N+1 tests read this.
        self.calls: list[str] = []
        self._next_id = 1
        self.link_results: dict[int, dict] = {}

    def _id(self) -> int:
        value, self._next_id = self._next_id, self._next_id + 1
        return value

    # -- projects ---------------------------------------------------------- #

    def add_project(self, **fields: Any) -> dict:
        project = {"id": self._id(), "utm_enabled": False, **fields}
        self.projects.append(project)
        return project

    def list_projects(self) -> list[dict]:
        self.calls.append("list_projects")
        return [dict(p) for p in self.projects]

    def create_project(self, payload: dict) -> dict:
        self.calls.append("create_project")
        return self.add_project(**payload)

    def update_project(self, project_id: int, payload: dict) -> dict:
        self.calls.append("update_project")
        project = next(p for p in self.projects if p["id"] == project_id)
        project.update(payload)
        return dict(project)

    # -- content ----------------------------------------------------------- #

    def list_content(self, project_id: int | None = None, limit: int = 500) -> list[dict]:
        self.calls.append(f"list_content:{project_id}")
        rows = [
            {k: v for k, v in row.items() if k != "body_markdown"}
            for row in self.content
            if project_id is None or row["project_id"] == project_id
        ]
        return rows[:limit]

    def get_content(self, content_id: int) -> dict:
        self.calls.append(f"get_content:{content_id}")
        return dict(self._row(content_id))

    def create_content(self, payload: dict) -> dict:
        self.calls.append("create_content")
        body = payload.get("body_markdown", "")
        keywords = normalize_keywords(payload.get("keywords") or [])
        source: dict = {"kind": "manual", "user_id": 1}
        if payload.get("campaign_key"):
            source["campaign_key"] = payload["campaign_key"]
        row = {
            "id": self._id(),
            "project_id": payload["project_id"],
            "content_type": payload.get("content_type", "feature_spotlight"),
            "status": "draft",
            "title": payload["title"],
            "body_markdown": body,
            # Pulse fills these in from the body when the caller sends nothing.
            "excerpt": payload.get("excerpt") or _excerpt_from(body),
            "meta_description": payload.get("meta_description") or _excerpt_from(body)[:160],
            "keywords": keywords,
            "tags": payload.get("tags") or [],
            "focus_keyword": payload.get("focus_keyword") or (keywords[0] if keywords else ""),
            "source": source,
            "word_count": len(body.split()),
            "publications": [],
        }
        self.content.append(row)
        return dict(row)

    def update_content(self, content_id: int, payload: dict) -> dict:
        self.calls.append(f"update_content:{content_id}")
        row = self._row(content_id)
        # The backend refuses edits to a published piece — 409 on anything but
        # status and scheduled_for.
        if row["status"] == "published" and set(payload) - {"status", "scheduled_for"}:
            raise FakeHeraldError(409, "This piece is already published.")
        for key, value in payload.items():
            row[key] = normalize_keywords(value) if key == "keywords" else value
        row["word_count"] = len(row["body_markdown"].split())
        return dict(row)

    def check_links(self, content_id: int) -> dict:
        self.calls.append(f"check_links:{content_id}")
        return self.link_results.get(
            content_id, {"links": [], "broken_count": 0, "checked": 0}
        )

    def schedule_content(
        self, content_id: int, *, platforms: list[str] | None = None, **kwargs: Any
    ) -> list[dict]:
        self.calls.append(f"schedule_content:{content_id}")
        row = self._row(content_id)
        when = kwargs.get("scheduled_for") or "herald-chosen"
        publications = [
            {
                "id": self._id(),
                "content_id": content_id,
                "platform": platform,
                "status": "scheduled",
                "scheduled_for": when,
            }
            for platform in (platforms or [])
        ]
        row["publications"] = publications
        return publications

    # -- settings ---------------------------------------------------------- #

    def connected_platforms(self) -> list[str]:
        self.calls.append("connected_platforms")
        return list(self.connected)

    # -- helpers ----------------------------------------------------------- #

    def _row(self, content_id: int) -> dict:
        row = next((c for c in self.content if c["id"] == content_id), None)
        if row is None:
            raise FakeHeraldError(404, "Content not found")
        return row

    def by_title(self, title: str) -> dict:
        return next(c for c in self.content if c["title"] == title)

    def count(self, name: str) -> int:
        """How many calls started with *name* — for the N+1 assertions."""
        return sum(1 for call in self.calls if call.split(":")[0] == name)
