"""Drawing a growth curve must not read the article behind it.

``velocity.curves`` selected the whole ``Content`` entity alongside each
publication, and used one field off it: the title. Everything else on the row
came too — the article body, the JSON keyword and metadata columns — once per
publication, on a function with nine callers including the alert pass that both
the dashboard and the weekly digest run.

The entity was also what made ``Content.publications``' ``lazy="selectin"``
fire, which an earlier fix held back with an explicit ``lazyload``. Selecting
the title column settles both: there is no entity left to load a relationship
from, so the two failures have one fix and the ``lazyload`` is gone.

Asserted on the emitted SQL, because no number here ever changed. What changed
is how much has to cross the wire to produce them — and the query count, which
is what a naive N+1 test counts, does not move either way.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import velocity

#: Long enough to be unmistakable in a statement dump if it is ever selected.
_BODY = "word " * 2000


@pytest.fixture
def published(db, project):
    """Two published posts on the account, each with a reading behind it."""
    made = []
    for index, platform in enumerate((Platform.DEVTO, Platform.HASHNODE)):
        content = Content(
            project_id=project.id,
            title=f"Piece {index}",
            slug=f"piece-{index}",
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.PUBLISHED,
            body_markdown=_BODY,
        )
        db.add(content)
        db.flush()
        publication = Publication(
            content_id=content.id,
            platform=platform,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow() - timedelta(days=5),
            external_id=f"ext-{index}",
        )
        db.add(publication)
        db.flush()
        db.add(
            ContentMetric(
                publication_id=publication.id,
                views=100 + index,
                reads=10 + index,
                captured_at=utcnow() - timedelta(days=4),
            )
        )
        made.append((content, publication))
    db.commit()
    return made


def test_building_curves_does_not_select_the_article_body(db, user, published, sql_log):
    sql_log.clear()
    built = velocity.curves(db, user.id)

    assert {curve.title for curve in built} == {"Piece 0", "Piece 1"}

    bodies = [s for s in sql_log if "content.body_markdown" in s]
    assert not bodies, (
        "the curve query carries a whole article per publication:\n"
        + "\n".join(s[:200] for s in bodies)
    )


def test_building_curves_does_not_fire_the_selectin_publications_load(
    db, user, published, sql_log
):
    """The ``lazyload`` is gone; selecting a column has to be what replaces it."""
    sql_log.clear()
    velocity.curves(db, user.id)

    selectin = [s for s in sql_log if "FROM publications WHERE publications.content_id IN" in s]
    assert not selectin, "\n".join(s[:200] for s in selectin)


def test_the_curve_still_carries_the_title_it_always_did(db, user, published):
    """The one field the entity was ever selected for."""
    by_platform = {curve.platform: curve for curve in velocity.curves(db, user.id)}

    assert by_platform[Platform.DEVTO].title == "Piece 0"
    assert by_platform[Platform.HASHNODE].title == "Piece 1"
    assert by_platform[Platform.DEVTO].points[-1].views == 100
    assert by_platform[Platform.HASHNODE].points[-1].views == 101


def test_filtering_by_project_still_works_without_the_entity(db, user, published, project):
    """``Content`` is still joined for its predicates, only not selected."""
    assert velocity.curves(db, user.id, project_id=project.id)
    assert velocity.curves(db, user.id, project_id=project.id + 999) == []
