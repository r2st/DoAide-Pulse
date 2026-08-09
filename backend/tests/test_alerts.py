"""Judging a post against this user's own normal.

The rule this file guards is that an alert is only ever raised when there is
something to compare against. Every "no alert" test below is as important as
the one that fires: a panel that cries wolf on a user's third post — or on a
platform that reports no views at all — is one they will learn to skip, and
then the one real warning goes unread too.
"""
from __future__ import annotations

from app.config import settings
from app.models.publication import Platform
from app.services import alerts
from tests.test_velocity import make_post


def _seed_normal(db, project, *, count: int = 3, views: int = 1000) -> None:
    """*count* posts that each did a normal ``views`` in their first 48 hours."""
    for index in range(count):
        make_post(
            db,
            project,
            slug=f"normal-{index}",
            published_hours_ago=200,
            readings=[(24, views // 2), (46, views)],
        )


# --------------------------------------------------------------------------- #
# Underperformance                                                            #
# --------------------------------------------------------------------------- #


def test_a_post_far_under_the_median_is_flagged(db, project, user):
    _seed_normal(db, project)
    make_post(
        db, project, slug="dud", published_hours_ago=200, readings=[(24, 8), (46, 20)]
    )

    (alert,) = [a for a in alerts.build(db, user.id) if a.kind == "underperforming"]
    assert alert.severity == "warning"
    assert alert.title == "Post dud"
    assert alert.observed == 20
    assert alert.expected == 1000
    assert alert.ratio == 0.02
    assert "2% of your usual" in alert.message


def test_a_normal_post_is_not_flagged(db, project, user):
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="fine",
        published_hours_ago=200,
        readings=[(24, 400), (46, 900)],
    )

    assert [a for a in alerts.build(db, user.id) if a.kind == "underperforming"] == []


def test_a_post_just_under_the_threshold_is_left_alone(db, project, user):
    """The bar is deliberately far out — ordinary variation is not an alert."""
    _seed_normal(db, project)
    threshold = settings.underperformance_threshold
    make_post(
        db,
        project,
        slug="middling",
        published_hours_ago=200,
        readings=[(46, int(1000 * threshold) + 10)],
    )

    assert [a for a in alerts.build(db, user.id) if a.kind == "underperforming"] == []


def test_nothing_is_judged_without_enough_comparable_posts(db, project, user):
    """A verdict from two other posts is not a verdict."""
    _seed_normal(db, project, count=2)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 1)])

    assert alerts.build(db, user.id) == []


def test_a_post_is_judged_against_its_own_platform_only(db, project, user):
    """A quiet Bluesky post is not a failed Dev.to post."""
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="social",
        published_hours_ago=200,
        readings=[(46, 12)],
        platform=Platform.BLUESKY,
    )

    assert alerts.build(db, user.id) == []


def test_a_post_still_inside_the_window_is_not_judged(db, project, user):
    _seed_normal(db, project)
    make_post(db, project, slug="fresh", published_hours_ago=6, readings=[(4, 3)])

    assert alerts.build(db, user.id) == []


def test_a_post_too_old_to_rescue_is_not_a_headline_problem(db, project, user):
    """The verdict is still true; the alert has stopped being worth raising.

    "Worth trying a different headline while it is still new" is what the alert
    says, and on a post from six months ago it is simply false — the piece is
    out of every feed it was ever in, and no headline changes that.
    """
    _seed_normal(db, project)
    make_post(db, project, slug="ancient", published_hours_ago=5000, readings=[(46, 10)])

    assert [a for a in alerts.build(db, user.id) if a.title == "Post ancient"] == []


def test_a_post_just_inside_the_bound_is_still_judged(db, project, user):
    """The cutoff is the only thing separating this from the test above."""
    _seed_normal(db, project)
    make_post(
        db,
        project,
        slug="recent",
        published_hours_ago=settings.underperformance_max_age_hours - 2,
        readings=[(46, 10)],
    )

    (alert,) = [a for a in alerts.build(db, user.id) if a.title == "Post recent"]
    assert alert.kind == "underperforming"


def test_this_week_s_dud_is_not_crowded_out_by_last_year_s(db, project, user):
    """The reason the bound matters, rather than just being tidy.

    Warnings sort worst-ratio-first and the list is truncated — three in the
    digest, five on the dashboard. An account's all-time worst posts have the
    worst ratios by definition, so they held the top of that list for ever and
    the post that went out this week, the only one still fixable, was sorted
    off the end.
    """
    _seed_normal(db, project)
    make_post(db, project, slug="ancient", published_hours_ago=5000, readings=[(46, 10)])
    make_post(db, project, slug="this-week", published_hours_ago=100, readings=[(46, 100)])

    (alert,) = alerts.build(db, user.id, limit=1)
    assert alert.title == "Post this-week"


def test_posts_past_the_bound_still_form_the_median(db, project, user):
    """Bounded as a subject, not as evidence.

    The comparison is against everything this user has ever published on the
    platform — that history is what makes the median worth anything, and a
    stricter reading of the age bound would throw it away and leave a mature
    account with too few comparables to judge anything at all.
    """
    for index in range(3):
        make_post(
            db,
            project,
            slug=f"old-normal-{index}",
            published_hours_ago=5000,
            readings=[(24, 500), (46, 1000)],
        )
    make_post(db, project, slug="fresh-dud", published_hours_ago=100, readings=[(46, 50)])

    (alert,) = alerts.build(db, user.id)
    assert alert.title == "Post fresh-dud"
    assert alert.expected == 1000, "the median came from posts too old to alert on"


def test_a_platform_where_nothing_is_read_raises_nothing(db, project, user):
    """A median of zero cannot be underperformed."""
    for index in range(4):
        make_post(
            db,
            project,
            slug=f"silent-{index}",
            published_hours_ago=200,
            readings=[(46, 0)],
        )

    assert alerts.build(db, user.id) == []


def test_the_subject_does_not_lower_its_own_bar(db, project, user):
    """Leave-one-out, so a bad post cannot make itself look less bad.

    With the subject included the median of [5, 5, 1000, 1000] is 502 and the
    post sits at 1% of it; excluded, the bar is 1000. Both flag here — what is
    pinned is that the *number quoted to the user* is the honest one.
    """
    _seed_normal(db, project, count=2, views=1000)
    make_post(db, project, slug="dud-a", published_hours_ago=200, readings=[(46, 5)])
    make_post(db, project, slug="dud-b", published_hours_ago=200, readings=[(46, 5)])

    flagged = {a.title: a for a in alerts.build(db, user.id)}
    assert flagged["Post dud-a"].expected == 1000


# --------------------------------------------------------------------------- #
# Stalling                                                                    #
# --------------------------------------------------------------------------- #


def test_a_post_that_stopped_growing_is_a_notice_not_a_warning(db, project, user):
    readings = [(0, 0), (24, 900), (48, 1000)]
    readings += [(48 + 24 * n, 1000 + n) for n in range(1, 16)]
    make_post(db, project, slug="evergreen", published_hours_ago=600, readings=readings)

    (alert,) = alerts.build(db, user.id)
    assert alert.kind == "stalled"
    assert alert.severity == "info"
    assert "flattened" in alert.message


def test_one_alert_per_publication(db, project, user):
    """A post that underperformed and then stalled is one problem, not two.

    At 600 hours the piece matches both tests on the numbers. It gets one
    alert, and it is the stalled notice: a post twenty-five days old is not
    going to be rescued by a new headline, and the re-share :func:`_stalled`
    suggests is the only remedy still on the table.
    """
    _seed_normal(db, project)
    flat = [(0, 0), (24, 2), (46, 3)]
    flat += [(48 + 24 * n, 3) for n in range(1, 16)]
    make_post(db, project, slug="both", published_hours_ago=600, readings=flat)

    for_post = [a for a in alerts.build(db, user.id) if a.title == "Post both"]
    assert len(for_post) == 1
    assert for_post[0].kind == "stalled", "the remedy that still applies wins"


# --------------------------------------------------------------------------- #
# Ordering, scoping and the API                                               #
# --------------------------------------------------------------------------- #


def test_warnings_sort_above_notices_and_worst_first(db, project, user):
    _seed_normal(db, project)
    make_post(db, project, slug="bad", published_hours_ago=200, readings=[(46, 100)])
    make_post(db, project, slug="worse", published_hours_ago=200, readings=[(46, 5)])
    stalling = [(0, 0), (24, 900), (48, 1000)]
    stalling += [(48 + 24 * n, 1000 + n) for n in range(1, 16)]
    make_post(db, project, slug="flat", published_hours_ago=600, readings=stalling)

    found = alerts.build(db, user.id)
    assert [a.title for a in found[:2]] == ["Post worse", "Post bad"]
    assert found[-1].kind == "stalled"


def test_limit_is_honoured(db, project, user):
    _seed_normal(db, project, count=8)
    for index in range(5):
        make_post(
            db, project, slug=f"dud-{index}", published_hours_ago=200, readings=[(46, 2)]
        )

    assert len(alerts.build(db, user.id, limit=2)) == 2


def test_when_most_posts_do_badly_that_becomes_normal(db, project, user):
    """The comparison is against the user's own median, and means it.

    Three good posts and five poor ones make the poor result the median, so
    none of them is flagged. That is the intended reading: the panel answers
    "is this post unusual *for you*", not "is this number good". Telling
    somebody five separate times that their typical post is typical is how a
    dashboard gets ignored.
    """
    _seed_normal(db, project, count=3)
    for index in range(5):
        make_post(
            db, project, slug=f"quiet-{index}", published_hours_ago=200, readings=[(46, 2)]
        )

    assert alerts.build(db, user.id) == []


def test_alerts_are_scoped_to_the_user(db, project, user):
    _seed_normal(db, project)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 2)])

    assert alerts.build(db, user.id + 999) == []


def test_alerts_endpoint_counts_by_severity(client, auth, db, project):
    _seed_normal(db, project)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 2)])

    resp = client.get("/api/v1/analytics/alerts", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["warnings"] == 1
    assert body["notices"] == 0
    assert body["alerts"][0]["kind"] == "underperforming"


def test_alerts_endpoint_is_empty_with_no_data(client, auth):
    resp = client.get("/api/v1/analytics/alerts", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"alerts": [], "warnings": 0, "notices": 0}


def test_dashboard_carries_the_alerts(client, auth, db, project):
    _seed_normal(db, project)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 2)])

    body = client.get("/api/v1/analytics/dashboard", headers=auth).json()
    assert [a["kind"] for a in body["alerts"]] == ["underperforming"]


def test_digest_carries_the_alerts(db, project, user):
    from app.services import digest

    _seed_normal(db, project)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 2)])

    built = digest.build(db, user)
    assert len(built.attention) == 1
    assert "Post dud" in digest.render_text(built)
    assert "Worth a look" in digest.render_html(built)


def test_alerts_alone_do_not_make_a_quiet_week_worth_an_email(db, project, user):
    """An alert about a post from March is not news, and does not send mail."""
    from app.services import digest

    _seed_normal(db, project)
    make_post(db, project, slug="dud", published_hours_ago=200, readings=[(46, 2)])

    # Everything above is older than the digest window, so nothing moved.
    built = digest.build(db, user, days=1)
    assert built.attention, "the alert is still reported"
    assert built.is_empty is True, "but it does not by itself justify an email"
