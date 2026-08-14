"""The preview-link listing had no ceiling of any kind.

Every other listing in the content router bounds itself and reports the total
in ``X-Total-Count``. This one returned every row, and the rows are the one set
here that genuinely grows without limit: issuing does not prune, and
``revoke`` deliberately keeps the row because the view count is the only record
that a share ever happened. A draft passed round a team for a few months
accumulates links indefinitely.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.services import preview_links


@pytest.fixture
def draft(db, project):
    content = Content(
        project_id=project.id,
        title="A draft that gets shared a lot",
        slug="a-draft-that-gets-shared-a-lot",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.REVIEW,
        body_markdown="body",
    )
    db.add(content)
    db.commit()
    return content


@pytest.fixture
def many_links(db, draft):
    """More links than a single page, all minted in the same second."""
    return [preview_links.issue(db, draft)[0] for _ in range(12)]


def test_the_listing_pages_and_reports_the_total(client, auth, draft, many_links):
    resp = client.get(
        f"/api/v1/content/{draft.id}/preview-links?limit=5", headers=auth
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 5
    assert resp.headers["X-Total-Count"] == "12"


def test_paging_covers_every_row_exactly_once(client, auth, draft, many_links):
    """The tie-break in the sort is what this is really pinning.

    All twelve links are created in the same second, so ordering by
    ``created_at`` alone leaves the rows inside a tied group in whatever order
    the database feels like — and a page boundary landing inside that group
    then drops some rows and repeats others.
    """
    seen: list[int] = []
    for offset in (0, 5, 10):
        resp = client.get(
            f"/api/v1/content/{draft.id}/preview-links?limit=5&offset={offset}",
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        seen.extend(row["id"] for row in resp.json())

    assert len(seen) == 12
    assert len(set(seen)) == 12, "a page boundary dropped or repeated a row"
    assert set(seen) == {link.id for link in many_links}


def test_the_page_is_newest_first(client, auth, draft, many_links):
    resp = client.get(f"/api/v1/content/{draft.id}/preview-links", headers=auth)
    assert resp.status_code == 200, resp.text
    ids = [row["id"] for row in resp.json()]
    assert ids == sorted(ids, reverse=True)


def test_the_default_page_is_bounded(client, auth, db, draft):
    """The caller that matters opts into nothing.

    The frontend calls this with no query string at all, so a ceiling that only
    applies when ``limit`` is passed is not a ceiling. Fifty-one rows, one over
    the default, is the cheapest way to show the default is doing the work.
    """
    for _ in range(51):
        preview_links.issue(db, draft)

    resp = client.get(f"/api/v1/content/{draft.id}/preview-links", headers=auth)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 50
    assert resp.headers["X-Total-Count"] == "51"


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=-1", "limit=201", "offset=-1"],
    ids=["zero", "negative-limit", "over-the-cap", "negative-offset"],
)
def test_the_bounds_are_enforced_at_both_ends(client, auth, draft, query):
    """``ge=1`` as well as ``le``, for the reason ``list_content`` documents.

    A negative limit reaches ``.limit()`` verbatim, and SQLite reads ``LIMIT
    -1`` as "no limit" — which would hand back the whole table through the
    very parameter added to bound it.
    """
    resp = client.get(
        f"/api/v1/content/{draft.id}/preview-links?{query}", headers=auth
    )
    assert resp.status_code == 422, resp.text


def test_a_revoked_link_still_occupies_a_row_in_the_count(client, auth, db, draft):
    """The reason the underlying set is unbounded, stated as a test."""
    link, _ = preview_links.issue(db, draft)
    preview_links.revoke(db, link)

    resp = client.get(f"/api/v1/content/{draft.id}/preview-links", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Total-Count"] == "1"
    assert resp.json()[0]["revoked_at"] is not None


def test_the_count_is_per_draft_not_per_account(client, auth, db, project, draft):
    other = Content(
        project_id=project.id,
        title="A different draft",
        slug="a-different-draft",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.REVIEW,
        body_markdown="body",
    )
    db.add(other)
    db.commit()
    preview_links.issue(db, draft)
    preview_links.issue(db, other)
    preview_links.issue(db, other)

    resp = client.get(f"/api/v1/content/{draft.id}/preview-links", headers=auth)
    assert resp.headers["X-Total-Count"] == "1"
    resp = client.get(f"/api/v1/content/{other.id}/preview-links", headers=auth)
    assert resp.headers["X-Total-Count"] == "2"


def test_a_draft_with_no_links_reports_zero(client, auth, draft):
    resp = client.get(f"/api/v1/content/{draft.id}/preview-links", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    assert resp.headers["X-Total-Count"] == "0"
