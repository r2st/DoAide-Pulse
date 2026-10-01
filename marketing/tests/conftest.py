"""Shared fixtures for the campaign runner's tests.

``marketing/`` is a pair of scripts rather than an installed package — the
runner adds its own directory to ``sys.path`` when run directly — so the tests
do the same thing here rather than relying on the working directory pytest
happened to start in.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MARKETING = Path(__file__).resolve().parent.parent
if str(MARKETING) not in sys.path:
    sys.path.insert(0, str(MARKETING))


#: The smallest plan that is still a real one: two projects, three articles,
#: one complete two-part series.
PLAN = {
    "base_url": "https://herald.example.com/api/v1",
    "projects": [
        {
            "key": "gstbot",
            "name": "GSTBot",
            "description": "GST compliance for Indian SMBs.",
            "live_url": "https://gstbot.example.com",
            "auto_canonical": True,
            "canonical_platform": "devto",
            "utm_enabled": True,
            "utm_campaign": "gstbot-2026q3",
            "tone": "technical",
        },
        {
            "key": "herald",
            "name": "Pulse",
            "description": "Marketing automation for developers.",
            "live_url": "https://herald.example.com",
            "utm_enabled": True,
            "utm_campaign": "herald-2026q3",
        },
    ],
    "series": [{"key": "gst-guide", "title": "The GST Guide"}],
    "articles": [
        {
            "key": "reconciliation",
            "project": "gstbot",
            "series": "gst-guide",
            "part": 1,
            "content_type": "tutorial",
            "title": "Where ITC Leaks",
            "focus_keyword": "gstr-2b reconciliation",
            "keywords": ["gstr-2b reconciliation", "input tax credit"],
            "tags": ["india", "tax"],
            "excerpt": "Reconciliation is four problems wearing one name.",
            "meta_description": "A practical guide to GSTR-2B reconciliation.",
            "body_file": "reconciliation.md",
            "platforms": ["devto", "bluesky"],
            "status": "review",
        },
        {
            "key": "matching",
            "project": "gstbot",
            "series": "gst-guide",
            "part": 2,
            "title": "Matching With Tolerances",
            "keywords": ["invoice matching"],
            "tags": ["india"],
            "body_file": "matching.md",
            "platforms": ["devto"],
        },
        {
            "key": "launch",
            "project": "herald",
            "title": "Pulse Ships",
            "body_file": "launch.md",
            "platforms": ["bluesky"],
        },
    ],
}


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A campaign file on disk, with its article bodies beside it.

    Mirrors the real layout: bodies live in ``../content`` relative to the
    campaign file, because they are shared between campaigns.
    """
    content = tmp_path / "content"
    content.mkdir()
    for name, body in (
        ("reconciliation.md", "# Where ITC Leaks\n\nFour problems, one name.\n"),
        ("matching.md", "# Matching\n\nExact string matching does not work.\n"),
        ("launch.md", "# Pulse\n\nIt writes the posts.\n"),
    ):
        (content / name).write_text(body, encoding="utf-8")

    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    (campaigns / "q3.json").write_text(json.dumps(PLAN), encoding="utf-8")
    return tmp_path


@pytest.fixture
def plan_path(plan_dir: Path) -> Path:
    return plan_dir / "campaigns" / "q3.json"


def write_plan(plan_dir: Path, plan: dict, name: str = "q3.json") -> Path:
    """Drop a modified plan next to the fixture's bodies."""
    path = plan_dir / "campaigns" / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path
