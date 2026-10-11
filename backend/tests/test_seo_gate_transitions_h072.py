"""H072 — SEO preflight gate enforced on all approval paths.

Bug 1: ``set_content_status`` with ``{"status": "approved"}`` bypassed the SEO
preflight gate that ``approve_content`` and the PATCH endpoint both enforce.

Bug 2: ``bulk_approve_content`` bypassed the SEO preflight gate entirely,
allowing pieces with SEO issues to reach APPROVED status in bulk.

Both fixed by adding the gate check to the missing paths.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType

API = "/api/v1/content"


@pytest.fixture(autouse=True)
def _enable_quality_gate(monkeypatch):
    """Enable the SEO quality gate for every test in this module."""
    from app.routers import content as content_router

    monkeypatch.setattr(
        content_router.settings,
        "content_quality_gate_enabled",
        True,
    )
    yield


@pytest.fixture
def seo_poor_content(db, project) -> Content:
    """A piece that will fail every SEO preflight check."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Bad",
        slug="bad-seo",
        body_markdown="Short body no headings no links.",
        meta_description="Too short.",
        status=ContentStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def seo_poor_review(db, project) -> Content:
    """A piece in REVIEW that will fail SEO preflight."""
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Bad",
        slug="bad-seo-review",
        body_markdown="Short body.",
        meta_description="Short.",
        status=ContentStatus.REVIEW,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ================================================================== #
# Bug 1: set_content_status now enforces SEO gate                    #
# ================================================================== #


def test_set_status_to_approved_blocked_by_seo_gate(
    client, auth, seo_poor_content, db
):
    resp = client.post(
        f"{API}/{seo_poor_content.id}/status",
        json={"status": "approved"},
        headers=auth,
    )
    assert resp.status_code == 409, resp.text
    assert "SEO preflight" in resp.json()["detail"]
    db.refresh(seo_poor_content)
    assert seo_poor_content.status == ContentStatus.DRAFT


def test_set_status_from_review_to_approved_blocked_by_seo_gate(
    client, auth, seo_poor_review, db
):
    resp = client.post(
        f"{API}/{seo_poor_review.id}/status",
        json={"status": "approved"},
        headers=auth,
    )
    assert resp.status_code == 409, resp.text
    assert "SEO preflight" in resp.json()["detail"]
    db.refresh(seo_poor_review)
    assert seo_poor_review.status == ContentStatus.REVIEW


def test_set_status_to_draft_not_blocked_by_seo_gate(
    client, auth, seo_poor_review, db
):
    """Non-APPROVED transitions should not trigger the SEO gate."""
    resp = client.post(
        f"{API}/{seo_poor_review.id}/status",
        json={"status": "draft"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    db.refresh(seo_poor_review)
    assert seo_poor_review.status == ContentStatus.DRAFT


# ================================================================== #
# Bug 2: bulk_approve_content now enforces SEO gate                  #
# ================================================================== #


def test_bulk_approve_reports_seo_failures(
    client, auth, seo_poor_content, db
):
    resp = client.post(
        f"{API}/bulk/approve",
        json={"content_ids": [seo_poor_content.id]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    failed_ids = [f["content_id"] for f in body["failed"]]
    assert seo_poor_content.id in failed_ids
    assert any("SEO preflight" in f["reason"] for f in body["failed"])
    db.refresh(seo_poor_content)
    assert seo_poor_content.status == ContentStatus.DRAFT


def test_bulk_approve_mixed_seo_pass_and_fail(
    client, auth, seo_poor_content, db, project
):
    """A piece with good SEO succeeds while one with bad SEO fails."""
    good = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="A Perfectly Optimized Title That Hits Fifty Characters!",
        slug="good-seo-piece",
        body_markdown=(
            "## First Section\n\n"
            "Some testing body text with [link one](https://a.com) "
            "and [link two](https://b.com). Testing is important. "
            "We do testing every day. Testing keeps quality high. "
            + ("Word " * 200) + "\n\n"
            "## Second Section\n\n"
            + ("More text. " * 50)
        ),
        meta_description=(
            "This meta description is carefully crafted to fall within the "
            "ideal 150 to 160 character range for search engine results pages "
            "display purposes and more."
        ),
        keywords=["testing"],
        status=ContentStatus.DRAFT,
    )
    db.add(good)
    db.commit()
    db.refresh(good)

    resp = client.post(
        f"{API}/bulk/approve",
        json={"content_ids": [seo_poor_content.id, good.id]},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert good.id in body["succeeded"]
    failed_ids = [f["content_id"] for f in body["failed"]]
    assert seo_poor_content.id in failed_ids
