"""``extra="allow"`` on a velocity curve now means two names, not any name.

``VelocityCurveOut`` carries two fields it cannot declare. Their keys are
``views_first_{hours}h`` and the hours come from ``VELOCITY_EARLY_WINDOW_HOURS``
and ``VELOCITY_BENCHMARK_WINDOW_HOURS``, so the class does not know them until
an install is configured. Allowing extras is what keeps them; the problem was
that it kept *everything*, in both directions:

* **Nothing described them.** They are the entire content of the velocity panel
  — the two view counts every benchmark and every "fastest start" is computed
  from — and OpenAPI said the object had some unspecified additional properties
  of unspecified type. A generated client got nothing to call.
* **Nothing bounded them.** Any key the service started emitting would reach
  clients undocumented, and a typo in a key name would be forwarded rather than
  raised. The existing key-diff tests could not catch either: they compare the
  service's keys against the endpoint's, and an open model passes that by
  construction, since it copies whatever it is handed.

Both names now come from one function, :func:`app.services.velocity
.window_count_key`, so the code that produces them and the schema that
documents and bounds them cannot drift. That is asserted here rather than
assumed, because the failure mode of a drift is two silently-missing fields.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import settings
from app.schemas.analytics import VelocityCurveDetailOut, VelocityCurveOut
from app.services.velocity import window_count_key


def _early() -> str:
    return window_count_key(int(settings.velocity_early_window_hours))


def _benchmark() -> str:
    return window_count_key(int(settings.velocity_benchmark_window_hours))


def _curve(**overrides) -> dict:
    """A payload shaped exactly as ``Curve.as_dict`` shapes one."""
    return {
        "publication_id": 1,
        "content_id": 2,
        "platform": "devto",
        "title": "A post",
        "published_at": None,
        "age_hours": 50.0,
        "snapshots": 3,
        "views": 300,
        "engagement": 12,
        "early_window_hours": int(settings.velocity_early_window_hours),
        "benchmark_window_hours": int(settings.velocity_benchmark_window_hours),
        _early(): 100,
        _benchmark(): 200,
        "views_per_day": 144.0,
        "stalled": False,
        **overrides,
    }


# ---- The two names are still kept ----------------------------------------- #


def test_the_two_configuration_named_counts_survive_the_model():
    """The reason ``extra="allow"`` is there at all. A strict model drops these."""
    model = VelocityCurveOut.model_validate(_curve())

    dumped = model.model_dump()

    assert dumped[_early()] == 100
    assert dumped[_benchmark()] == 200


def test_a_curve_too_young_for_a_window_keeps_the_key_with_null_in_it():
    """``None`` is unknown, not nought — the distinction the whole aggregation
    exists to make, and it has to survive serialisation."""
    model = VelocityCurveOut.model_validate(_curve(**{_benchmark(): None}))

    assert model.model_dump()[_benchmark()] is None


# ---- But only those two --------------------------------------------------- #


def test_an_unexpected_extra_is_refused_rather_than_forwarded():
    """An open response model is a hole: a key the service starts producing
    reaches clients with nothing documenting it. Two names are allowed; a third
    is a bug in whatever built the payload."""
    with pytest.raises(ValidationError) as caught:
        VelocityCurveOut.model_validate(_curve(views_first_9999h=5))

    assert "views_first_9999h" in str(caught.value)


def test_a_typo_in_a_declared_field_is_caught_rather_than_silently_added():
    """The failure this actually protects against. ``vews_per_day`` used to be
    accepted as an extra and forwarded, so the real field fell back to its
    default and the payload carried both — a chart reading ``views_per_day``
    would show nothing, with no error anywhere."""
    payload = _curve()
    payload["vews_per_day"] = payload.pop("views_per_day")

    with pytest.raises(ValidationError) as caught:
        VelocityCurveOut.model_validate(payload)

    assert "vews_per_day" in str(caught.value)


def test_the_detail_model_inherits_the_bound():
    """``VelocityCurveDetailOut`` subclasses the curve, so it has to inherit the
    validator as well as the fields."""
    with pytest.raises(ValidationError):
        VelocityCurveDetailOut.model_validate(_curve(points=[], surprise=1))


def test_the_detail_model_still_takes_its_points():
    model = VelocityCurveDetailOut.model_validate(
        _curve(points=[{"hours": 6.0, "views": 10, "engagement": 1}])
    )

    assert model.points[0].views == 10
    assert model.model_dump()[_early()] == 100


# ---- One definition of the names ------------------------------------------ #


def test_the_service_and_the_schema_agree_on_the_key_names(db, user):
    """The drift guard.

    ``Curve.as_dict`` produces these keys and the schema decides which keys are
    allowed. If those two ever computed the name separately, a rename would
    show up as a pair of quietly-refused fields rather than as a failure here.
    """
    from datetime import timedelta

    from app.models.content import Content, ContentStatus, ContentType
    from app.models.metrics import ContentMetric
    from app.models.mixins import utcnow
    from app.models.project import Project, Tone
    from app.models.publication import Platform, Publication, PublicationStatus
    from app.services import velocity

    project = Project(
        user_id=user.id,
        name="P",
        slug="p",
        description="A thing that ships.",
        tone=Tone.TECHNICAL,
    )
    db.add(project)
    db.flush()
    content = Content(
        project_id=project.id,
        title="A post",
        slug="a-post",
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.PUBLISHED,
        body_markdown="Body.",
    )
    db.add(content)
    db.flush()
    published_at = utcnow() - timedelta(days=5)
    publication = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_url="https://dev.to/x/a-post",
    )
    db.add(publication)
    db.flush()
    for hours in (24, 48, 72):
        db.add(
            ContentMetric(
                publication_id=publication.id,
                captured_at=published_at + timedelta(hours=hours),
                views=hours,
                reactions=1,
            )
        )
    db.commit()

    (curve,) = velocity.curves(db, user.id)
    produced = curve.as_dict()

    # The service's own keys pass the schema's bound. This is the assertion
    # that fails if either side starts naming these fields for itself.
    model = VelocityCurveOut.model_validate(produced)
    assert set(model.model_dump()) == set(produced)
    assert _early() in produced
    assert _benchmark() in produced


# ---- And the schema says so ----------------------------------------------- #


def test_both_names_are_typed_and_described_in_the_generated_schema(client):
    """OpenAPI used to say "additional properties, unspecified". The two counts
    are known by the time the schema is generated even though they are not known
    when the class is defined, so they are written into it."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    curve = schema["VelocityCurveOut"]

    for key in (_early(), _benchmark()):
        assert key in curve["properties"], f"{key} is undocumented"
        assert curve["properties"][key]["anyOf"] == [
            {"type": "integer"},
            {"type": "null"},
        ]
        assert curve["properties"][key]["description"]


def test_the_schema_closes_the_object_it_used_to_leave_open(client):
    """``additionalProperties: false`` is the half that tells a generated client
    the two documented extras are all the extras there are."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]

    assert schema["VelocityCurveOut"]["additionalProperties"] is False
    assert schema["VelocityCurveDetailOut"]["additionalProperties"] is False


def test_neither_count_is_required(client):
    """A window a post is not old enough for has no count. Marking these
    required would make a generated client's parse fail on a post published an
    hour ago."""
    curve = client.get("/openapi.json").json()["components"]["schemas"][
        "VelocityCurveOut"
    ]

    assert _early() not in curve.get("required", [])
    assert _benchmark() not in curve.get("required", [])


# ---- The windows travel with the curve ------------------------------------ #


def test_a_single_curve_says_which_two_keys_it_carries(client, auth, db, user):
    """``GET /analytics/velocity/{id}`` returns one curve. Without the window
    sizes on it, its two most interesting fields could not be located without
    first fetching the summary endpoint to find out what they were called."""
    curve = VelocityCurveOut.model_validate(_curve())

    assert window_count_key(curve.early_window_hours) == _early()
    assert window_count_key(curve.benchmark_window_hours) == _benchmark()
