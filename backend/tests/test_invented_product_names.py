"""A product Pulse has never heard of holds a piece back.

The third kind of corruption, and the only one that survives every gate the
other two built. ``test_garbled_text_gate`` covers the sampler slipping a
*character* from another script into a word; this covers it slipping a whole
*noun* in, spelled in plain ASCII, in a sentence that parses::

    ## Integration with Wird
    The outreach module reads from the Teppil inbox …

Neither name exists. Neither is a typo of anything. Neither appears in
TalentPing's brief, its stack, its keywords or its commits — a free-tier model
needed two nouns to finish a paragraph and wrote them with the same confidence
it wrote the rest. The piece scored fine, had no dead links, is ASCII from end
to end, and came back at 0.85 confidence.

The literals below are the real ones, from the rows they were found in:

* content 56 (TalentPing) — ``Wird``, ``Teppil``
* content 80 (N409) — ``Integrate‑mar``, ``builtExamples``

and the four the gate turned up in the same sweep, which nothing had noticed:
``aboutSPECIFIC`` (33), ``NestJSManchester‑poweredubar`` (34), ``invoicesISC``
(69), ``forEditor`` (78).

The "must not fire" half is the more important half. A gate on unfamiliar proper
nouns is a gate on most of the English language unless it is held down hard, and
every case in that section is a real sentence out of a real published article
that an earlier draft of this module reported.
"""
from __future__ import annotations

import pytest

from app.models.content import ContentStatus, ContentType
from app.models.project import AutopilotMode
from app.models.trigger import TriggerKind
from app.services import content_generator, content_pipeline, factcheck, github_client
from app.services.content_generator import GeneratedContent
from app.services.github_client import Commit, RepoActivity
from app.services.signals import TriggerSignal

# The names, exactly as production wrote them. `Integrate‑mar` carries U+2011,
# the non-breaking hyphen these models reach for — not the ASCII one, which is
# why the token pattern has to know about it.
WIRD = "Wird"
TEPPIL = "Teppil"
INTEGRATE_MAR = "Integrate‑mar"
BUILT_EXAMPLES = "builtExamples"
ABOUT_SPECIFIC = "aboutSPECIFIC"
INVOICES_ISC = "invoicesISC"
FOR_EDITOR = "forEditor"

#: What Pulse knows about the project every unit test below is written against.
KNOWN = factcheck.vocabulary(
    "TalentPing",
    "AI recruiting autopilot with automated outreach, follow-ups, ATS boards, "
    "and weekly digests.",
    ["FastAPI", "React", "PostgreSQL", "Celery"],
    ["AI recruiting", "automated outreach", "ATS"],
)


# --------------------------------------------------------------------------- #
# The four that shipped                                                        #
# --------------------------------------------------------------------------- #


def test_an_invented_integration_is_reported():
    """content 56, the heading. The claim a reader would act on."""
    text = f"## Integration with {WIRD}\n\nThe outreach module handles the rest."

    assert [claim.name for claim in factcheck.unsupported_names(text, KNOWN)] == [WIRD]


def test_an_invented_component_is_reported():
    """content 56, the sentence under it."""
    text = f"The outreach module reads from the {TEPPIL} inbox and flags replies."

    claims = factcheck.unsupported_names(text, KNOWN)

    assert [claim.name for claim in claims] == [TEPPIL]
    assert claims[0].rule == "framed"


def test_a_word_welded_to_another_word_is_reported():
    """content 80. Two words the sampler ran together, both of them English."""
    text = f"Use the {BUILT_EXAMPLES} to submit the PDF to your auditor."

    claims = factcheck.unsupported_names(text, KNOWN)

    assert [claim.name for claim in claims] == [BUILT_EXAMPLES]
    assert claims[0].rule == "fused"


def test_a_word_welded_to_a_fragment_is_reported():
    """content 80. ``Integrate`` plus three characters that are not a word."""
    text = f"- **{INTEGRATE_MAR}**: Connect the API to your existing HRIS."

    claims = factcheck.unsupported_names(text, KNOWN)

    assert [claim.name for claim in claims] == [INTEGRATE_MAR]
    assert claims[0].rule == "hyphen"


@pytest.mark.parametrize(
    ("sentence", "name"),
    [
        (
            "informed choices aboutSPECIFIC equity grants while staying compliant",
            ABOUT_SPECIFIC,
        ),
        ("Unmatched — invoicesISC flagged with a red exclamation point", INVOICES_ISC),
        ("generates a plain-English summary forEditor. each scenario", FOR_EDITOR),
    ],
)
def test_the_ones_nothing_had_noticed(sentence, name):
    """Found by this gate on its first sweep of the eighty-one production rows."""
    assert [c.name for c in factcheck.unsupported_names(sentence, KNOWN)] == [name]


@pytest.mark.parametrize(
    "sentence",
    [
        # Every one of these is the real clause out of the row it held back,
        # before the `fused` arm was made to read the head of a token as well
        # as its middle. Five of the six are a list of other people's products,
        # which is the shape a project has least reason to have on file.
        "Inbound inquiries from property portals (99acres, MagicBricks, "
        "Housing.com) are automatically captured.",  # 4
        "WhatsApp is essential in India/SE Asia/LatAm.",  # 7
        "TalentPing reads employer ATS boards directly — **Greenhouse, Lever, "
        "Ashby, Workable and SmartRecruiters** — because that is the freshest "
        "source.",  # 23, 26
        "Writes the hooks into settings.json — Stop, Notification, "
        "SubagentStop and PreToolUse — and installs a launchd service.",  # 28
        "**2. Compliance platforms (ClearTax, Taxilla, IRIS, Cygnet).** "
        "Purpose-built for GST at volume.",  # 38, 45
    ],
)
def test_a_third_party_brand_is_not_a_splice(sentence):
    """A capitalized name Pulse has not been told about is a brand, not a slip.

    Six of the thirteen rows an unrestricted `fused` arm flagged across the
    production corpus were this — a real product named once, in passing, that no
    project has any reason to carry in its brief. Holding a piece back for one
    is a review with nothing at the end of it, and six of them is how a reviewer
    learns to wave the queue through.
    """
    assert factcheck.unsupported_names(sentence, KNOWN) == []


def test_a_brand_is_still_read_when_the_copy_claims_it_ships():
    """What the tightened arm gives up, and where it is caught instead.

    `GoSumoX` is unknown and capitalized, so the shape arm now leaves it alone.
    The frame does not: the sentence claims TalentPing integrates with it, and
    that claim is the thing a reader would act on.
    """
    claims = factcheck.unsupported_names("Integration with GoSumoX is live.", KNOWN)

    assert [claim.name for claim in claims] == ["GoSumoX"]
    assert claims[0].rule == "framed"


def test_a_lowercase_brand_that_takes_a_capital_is_left_alone():
    """`iPhone` is the one real spelling shaped exactly like the corruption."""
    text = "The iPhone build ships alongside the iPad and watchOS clients."

    assert factcheck.unsupported_names(text, KNOWN) == []


def test_one_bad_token_is_one_claim_not_two():
    """``NestJSManchester‑poweredubar`` matches a frame *and* the fused shape.

    Reported twice, it reads as a piece with two hallucinations in it and sends
    the reviewer to the same word a second time.
    """
    text = (
        "Behind the scenes, the NestJSManchester‑poweredubar engine "
        "orchestrates the valuation."
    )

    assert len(factcheck.unsupported_names(text, KNOWN)) == 1


# --------------------------------------------------------------------------- #
# What must not fire                                                           #
# --------------------------------------------------------------------------- #


def test_a_clean_body_reports_nothing():
    """The control. Without this the section below passes for the wrong reason."""
    text = (
        "TalentPing pulls from company ATS boards. The outreach module sends "
        "follow-ups on a schedule, and the weekly digest summarises replies."
    )

    assert factcheck.unsupported_names(text, KNOWN) == []


@pytest.mark.parametrize(
    "sentence",
    [
        # The project's own name and stack, in the shapes the copy writes them.
        "TalentPing is built on FastAPI and PostgreSQL.",
        "TalentPing's outreach module reads the ATS board.",
        # Names common enough that no project declares them.
        "Inquiries arrive over WhatsApp and are pushed to GitHub.",
        "The pipeline runs on Kubernetes with OpenTelemetry traces.",
        "A NestJS-based service writes through TypeORM's query builder.",
        "Published to PyPI and npm on every tagged release.",
        # Title case, where capitalising a word says nothing about it.
        "## The Killer Feature: Scheduled Follow-ups",
        "## Front-End Integration",
        "- **Seamless integration** - the column is exposed through the API.",
        # Ordinary words sitting in a frame.
        "Clicking the Summary tab reveals a mobile-responsive page.",
        "The agent sees a new lead appear in the Leads panel.",
        "From the client portal, founders open the Scenarios tab.",
        "This integration streamlines property management.",
        "A WYSIWYG editor over the drafted narrative.",
        # Hyphenated English, which the narrowest arm has to leave alone.
        "Blocked is a different problem from due-soon.",
        "The audit-ready export is read-only and point-in-time.",
        "Flags auto-responses as Out-of-Office before follow-up.",
    ],
)
def test_real_sentences_that_are_not_claims(sentence):
    """Every one of these is out of a real article and reported by an early draft."""
    assert factcheck.unsupported_names(sentence, KNOWN) == []


def test_code_is_not_prose():
    """A fenced block is wall-to-wall camelCase and none of it is a claim."""
    text = (
        "Wire it up:\n\n```python\n"
        "resp = someClient.fetchAll(retryPolicy=defaultPolicy)\n"
        "```\n\nThat is the whole integration."
    )

    assert factcheck.unsupported_names(text, KNOWN) == []


def test_inline_code_is_not_prose():
    assert factcheck.unsupported_names("Call `getUserById` first.", KNOWN) == []


def test_a_url_is_not_a_claim():
    """A hostname is full of name-shaped fragments nobody wrote as prose."""
    text = "See https://docs.example.com/quickStart/setUp for the walkthrough."

    assert factcheck.unsupported_names(text, KNOWN) == []


def test_stripped_spans_do_not_weld_their_neighbours():
    """Removing a span must not fuse the words either side into a third."""
    assert factcheck.unsupported_names("the `id` Column is indexed", KNOWN) == []


def test_empty_text_reports_nothing():
    assert factcheck.unsupported_names("", KNOWN) == []


# --------------------------------------------------------------------------- #
# Grounding                                                                    #
# --------------------------------------------------------------------------- #


def test_a_name_the_brief_supports_is_not_a_claim():
    """The whole premise: the gate is about grounding, not about spelling."""
    ungrounded = factcheck.vocabulary("TalentPing")
    grounded = factcheck.vocabulary("TalentPing", "Now with a Teppil inbox.")
    text = "The outreach module reads from the Teppil inbox."

    assert [c.name for c in factcheck.unsupported_names(text, ungrounded)] == [TEPPIL]
    assert factcheck.unsupported_names(text, grounded) == []


def test_the_commits_that_prompted_the_piece_ground_it(project):
    """A release names the things it touched, and those names are in the commits.

    Nothing puts them in a column, so a piece written about a release would be
    held back for naming exactly what it was asked to write about.
    """
    activity = RepoActivity(
        full_name="r2st/DoAide-Pulse",
        new_commits=[
            Commit(
                sha="a" * 40,
                message="feat(inbox): add the Teppil digest",
                author="suman",
                committed_at=None,
                url="https://github.com/r2st/DoAide-Pulse/commit/aaa",
            )
        ],
    )
    text = "The outreach module reads from the Teppil inbox."

    bare = factcheck.project_vocabulary(project)
    with_activity = factcheck.project_vocabulary(project, activity=activity)

    assert factcheck.unsupported_names(text, bare)
    assert factcheck.unsupported_names(text, with_activity) == []


def test_a_trigger_signal_grounds_a_piece_too(project):
    """The same reasoning for the half of the pipeline that has no repo."""
    signal = TriggerSignal(
        kind=TriggerKind.RSS,
        source="RSS Changelog",
        headline="Teppil inbox is live",
        summary="",
    )
    text = "The outreach module reads from the Teppil inbox."

    assert factcheck.unsupported_names(
        text, factcheck.project_vocabulary(project, signal=signal)
    ) == []


def test_a_project_with_empty_columns_builds_a_vocabulary(db, project):
    """Every source that feeds this is nullable, and none of them may raise."""
    project.repo_url = None
    project.live_url = None
    project.tech_stack = []
    project.keywords = []
    project.description = ""
    project.target_audience = ""
    db.commit()

    assert "pulse" in factcheck.project_vocabulary(project)


@pytest.mark.parametrize(
    ("written", "declared"),
    [
        ("Node.js", "nodejs"),
        ("add-on", "addon"),
        ("Pulse's", "Pulse"),
        ("PostgreSQL", "postgresql"),
    ],
)
def test_a_name_grounds_across_spellings(written, declared):
    """Case, internal punctuation and the possessive all vary freely."""
    assert factcheck.normalize(written) == factcheck.normalize(declared)


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #


def test_a_claim_carries_the_sentence_it_appears_in():
    """"Wird" alone cannot be judged. What the piece claims about it can."""
    text = f"Recruiters keep one thread. Integration with {WIRD} syncs replies."

    (claim,) = factcheck.unsupported_names(text, KNOWN)

    assert WIRD in claim.context
    assert "syncs replies" in claim.context


def test_a_repeated_name_is_one_claim():
    text = f"Integration with {WIRD}. The {WIRD} bridge. Later, {WIRD} again."

    assert len(factcheck.unsupported_names(text, KNOWN)) == 1


def test_claims_come_back_in_document_order():
    """A reviewer opens the editor and reads from the top."""
    text = (
        f"Use the {BUILT_EXAMPLES} to export.\n\n"
        f"## Integration with {WIRD}\n\nAnd more."
    )

    names = [claim.name for claim in factcheck.unsupported_names(text, KNOWN)]

    assert names == [BUILT_EXAMPLES, WIRD]


def test_the_limit_caps_the_list_from_the_top():
    """The cap cuts the tail, not the beginning — see the ordering above."""
    text = "\n".join(f"Integration with Zzq{chr(97 + i)}nkl." for i in range(12))

    claims = factcheck.unsupported_names(text, KNOWN, limit=3)

    assert len(claims) == 3
    assert claims[0].name == "Zzqankl"


# --------------------------------------------------------------------------- #
# The gate, through the pipeline                                               #
# --------------------------------------------------------------------------- #

_CLEAN_BODY = (
    "## Retries in Pulse\n\n"
    "Retries in Pulse are the part people ask about first. "
    + "The retry path is careful about retries and about what a retry costs. " * 45
)


@pytest.fixture
def auto_project(db, project, connect, monkeypatch):
    """A project that would auto-publish anything it is handed."""
    project.autopilot_mode = AutopilotMode.AUTO
    project.autopilot_platforms = ["devto"]
    db.commit()
    connect("devto")
    monkeypatch.setattr(content_pipeline, "publish_now", lambda publication_id: None)
    monkeypatch.setattr(content_pipeline.link_check, "check_body", lambda body, **kw: [])
    # The README arm is a network call. The tests that mean to exercise it say
    # so; every other test here would otherwise reach GitHub to decide whether a
    # piece publishes, which is not a thing a unit test may do.
    monkeypatch.setattr(github_client, "fetch_readme", lambda full_name: "")
    return project


def _generated(**overrides) -> GeneratedContent:
    base = {
        "title": "Retries in Pulse",
        "body_markdown": _CLEAN_BODY,
        "excerpt": "How retries work.",
        "meta_description": (
            "How retries work in Pulse, why a retry never double-posts, and "
            "what the backoff actually does when a platform is down."
        ),
        "keywords": ["retries"],
        "tags": ["python"],
        "focus_keyword": "retries",
        "confidence": 0.99,
    }
    base.update(overrides)
    return GeneratedContent(**base)


@pytest.fixture
def writes(monkeypatch):
    def _install(generated: GeneratedContent) -> None:
        monkeypatch.setattr(content_generator, "generate", lambda *a, **k: generated)

    return _install


def _route(db, project):
    return content_pipeline.generate_and_route(
        db,
        project,
        content_type=ContentType.ANNOUNCEMENT,
        source={"kind": "test"},
    )


def test_a_clean_piece_still_auto_publishes(db, auto_project, writes):
    """The control."""
    writes(_generated())

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.unsupported_names == []


def test_an_invented_product_goes_to_review_instead_of_publishing(
    db, auto_project, writes
):
    """The bug. Two of these are sitting in production right now."""
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.content.status == ContentStatus.REVIEW
    assert routed.unsupported_names == [WIRD]


def test_an_invented_product_in_the_title_goes_to_review(db, auto_project, writes):
    writes(_generated(title=f"Retries and the {TEPPIL} inbox"))

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_an_invented_product_in_the_meta_description_goes_to_review(
    db, auto_project, writes
):
    """Not derived from the body on every path, so it is read on its own."""
    writes(
        _generated(
            meta_description=(
                f"How retries work in Pulse, and how the {TEPPIL} inbox keeps "
                "every reply on one thread for the whole team to read."
            )
        )
    )

    assert _route(db, auto_project).status == content_pipeline.QUEUED_FOR_REVIEW


def test_the_claim_and_its_sentence_are_banked_on_the_row(db, auto_project, writes):
    """A reviewer should not have to read the logs to find out what was wrong."""
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    routed = _route(db, auto_project)

    (recorded,) = routed.content.source["unsupported_names"]
    assert recorded["name"] == WIRD
    assert recorded["rule"] == "framed"
    assert WIRD in recorded["context"]


def test_the_names_are_banked_even_when_the_gate_is_off(
    db, auto_project, writes, monkeypatch
):
    """The switch is about whether Pulse acts on this, not whether it looks.

    A piece that auto-publishes with a name nothing supports is still a piece
    somebody will want to find later, and the row is the only place that record
    can live.
    """
    monkeypatch.setattr(content_pipeline.settings, "factcheck_enabled", False)
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.content.source["unsupported_names"][0]["name"] == WIRD


def test_the_summary_reports_the_names(db, auto_project, writes):
    """What the Celery task returns is what a caller sees without the row."""
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    assert _route(db, auto_project).summary()["unsupported_names"] == [WIRD]


# --------------------------------------------------------------------------- #
# The README arm                                                               #
# --------------------------------------------------------------------------- #


def test_the_readme_is_not_fetched_for_a_clean_piece(db, auto_project, writes, monkeypatch):
    """The common case must cost nothing. A gate is not a reason to call GitHub."""
    calls: list[str] = []
    monkeypatch.setattr(
        github_client, "fetch_readme", lambda full_name: calls.append(full_name) or ""
    )
    writes(_generated())

    _route(db, auto_project)

    assert calls == []


def test_a_name_the_readme_supports_is_not_a_claim(db, auto_project, writes, monkeypatch):
    """The false positive the arm exists to remove.

    A real module named in the repo and in no Pulse column would otherwise hold
    every piece that mentions it, for ever.
    """
    monkeypatch.setattr(
        github_client,
        "fetch_readme",
        lambda full_name: "# Pulse\n\nShips with the Teppil inbox for replies.",
    )
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\nThe module reads the {TEPPIL} inbox."
        )
    )

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.AUTO_PUBLISHED
    assert routed.unsupported_names == []


def test_a_github_outage_leaves_the_first_answer_standing(
    db, auto_project, writes, monkeypatch
):
    """Failing a generation because a validator's optional input was missing
    would be the gate doing more harm than the thing it guards."""

    def _boom(full_name: str) -> str:
        raise github_client.GitHubError("rate limited")

    monkeypatch.setattr(github_client, "fetch_readme", _boom)
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    routed = _route(db, auto_project)

    assert routed.status == content_pipeline.QUEUED_FOR_REVIEW
    assert routed.unsupported_names == [WIRD]


def test_a_project_with_no_github_repo_skips_the_readme(
    db, auto_project, writes, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(
        github_client, "fetch_readme", lambda full_name: calls.append(full_name) or ""
    )
    auto_project.repo_url = "https://gitlab.com/r2st/DoAide-Pulse"
    db.commit()
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    assert _route(db, auto_project).unsupported_names == [WIRD]
    assert calls == []


def test_the_readme_arm_can_be_switched_off(db, auto_project, writes, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        github_client, "fetch_readme", lambda full_name: calls.append(full_name) or ""
    )
    monkeypatch.setattr(content_pipeline.settings, "factcheck_readme_enabled", False)
    writes(
        _generated(
            body_markdown=f"{_CLEAN_BODY}\n\n## Integration with {WIRD}\n\nIt syncs."
        )
    )

    assert _route(db, auto_project).unsupported_names == [WIRD]
    assert calls == []
