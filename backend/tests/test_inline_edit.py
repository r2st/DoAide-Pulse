"""Editing one passage of a draft rather than regenerating the piece.

The service is the interesting half. It has no mechanical fallback — nothing
but a model can shorten a paragraph — so everything it can do about a bad reply
is refuse it, and refusing well is most of what is tested here: an empty field,
a model that returned its own reasoning, and the one that looks fine until you
read it, a "shortened" passage longer than the original.

The rest is prompt construction, which is tested because the prompt is the only
place the guarantees live. There is no code path that stops a model inventing a
benchmark number; there is only an instruction that says not to, and a test
that the instruction is actually sent.
"""
from __future__ import annotations

import json

import pytest

from app.models.project import Project, Tone
from app.services import ai, inline_edit, llm_router
from app.services.inline_edit import EditOperation, EditUnavailable

BODY = """## Why the queue exists

Publishing runs in a worker rather than in the request, because a platform that
takes thirty seconds to answer should not hold a browser open for thirty
seconds. The queue is what makes that safe.

Each publication is a row. One row per platform, so "published to Dev.to,
rejected by LinkedIn" is a state the model can hold rather than an error.

## What happens when it fails

A failure is retried with backoff until the budget is spent.
"""

PASSAGE = (
    "Each publication is a row. One row per platform, so \"published to "
    "Dev.to,\nrejected by LinkedIn\" is a state the model can hold rather than "
    "an error."
)


@pytest.fixture
def stub_llm(monkeypatch):
    """Make the chain return whatever the test sets, and capture the prompt."""
    state = {"payload": None, "messages": None, "kwargs": None, "calls": 0}

    def fake_complete(messages, *, model=None, fallback_models=(), **kwargs):
        state["calls"] += 1
        state["messages"] = messages
        state["kwargs"] = kwargs
        payload = state["payload"]
        return llm_router.Completion(
            text=payload if isinstance(payload, str) else json.dumps(payload),
            provider="stub",
            model=model or "stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    return state


def _project(**overrides) -> Project:
    defaults = dict(user_id=1, name="Pulse", slug="pulse", tone=Tone.TECHNICAL)
    defaults.update(overrides)
    return Project(**defaults)


def _edit(operation=EditOperation.REWRITE, **overrides):
    kwargs = dict(
        body_markdown=BODY,
        selection=PASSAGE,
        operation=operation,
        title="How Pulse publishes",
        project=_project(),
    )
    kwargs.update(overrides)
    return inline_edit.edit(**kwargs)


def _prompt(state) -> str:
    return "\n".join(m["content"] for m in state["messages"])


# --------------------------------------------------------------------------- #
# The happy path                                                               #
# --------------------------------------------------------------------------- #


def test_the_replacement_comes_back_with_who_wrote_it(stub_llm):
    stub_llm["payload"] = {"replacement": "One row per platform. That is the whole trick."}

    result = _edit()

    assert result.replacement == "One row per platform. That is the whole trick."
    assert result.operation is EditOperation.REWRITE
    assert result.provider == "stub"
    assert result.model == "stub-model"


def test_only_the_passage_is_sent_as_the_thing_to_edit(stub_llm):
    """The rest of the article goes too, but marked as context.

    Without the surrounding paragraphs the model repeats the point made just
    above; without the marking it edits them as well, and the caller splices a
    replacement for one paragraph over three.
    """
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit()

    prompt = _prompt(stub_llm)
    assert "The passage to edit:" in prompt
    assert "context only" in prompt
    assert "[[THE PASSAGE]]" in prompt


def test_the_context_is_the_text_either_side_of_the_passage(stub_llm):
    """Not the top of the article — the paragraph before is what it follows."""
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit()

    prompt = _prompt(stub_llm)
    assert "What happens when it fails" in prompt  # after
    assert "should not hold a browser open" in prompt  # before


def test_the_model_is_told_not_to_invent(stub_llm):
    """The only thing standing between this feature and a fabricated benchmark.

    Worth a test precisely because it is not enforceable in code: if the
    instruction goes missing in an edit to the prompt, nothing else fails.
    """
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit()

    prompt = _prompt(stub_llm).lower()
    assert "never invent" in prompt


def test_a_passage_that_reads_as_an_instruction_is_still_content(stub_llm):
    """A draft about prompt injection must not be able to perform it.

    The selection is the author's text. The operation is the only instruction,
    and the system prompt has to say so — the passage arrives inside the user
    message either way.
    """
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit()

    system = stub_llm["messages"][0]["content"].lower()
    assert "never followed" in system


@pytest.mark.parametrize("operation", list(EditOperation))
def test_every_operation_sends_an_instruction_of_its_own(stub_llm, operation):
    """A missing entry would silently send an edit with no operation in it."""
    stub_llm["payload"] = {"replacement": "x" * 20}
    _edit(operation)

    assert inline_edit._INSTRUCTIONS[operation] in _prompt(stub_llm)


# --------------------------------------------------------------------------- #
# Length discipline                                                            #
# --------------------------------------------------------------------------- #


def test_shorten_asks_for_a_number_not_an_adjective(stub_llm):
    """Small models handle "roughly 90 characters" better than "half"."""
    stub_llm["payload"] = {"replacement": "short"}
    _edit(EditOperation.SHORTEN)

    assert f"roughly {len(PASSAGE) // 2} characters" in _prompt(stub_llm)


def test_expand_asks_for_more(stub_llm):
    stub_llm["payload"] = {"replacement": "x" * (len(PASSAGE) * 2)}
    _edit(EditOperation.EXPAND)

    assert f"roughly {len(PASSAGE) * 2} characters" in _prompt(stub_llm)


def test_expand_is_given_room_to_answer(stub_llm):
    """A budget that clips an expansion mid-sentence is worse than no feature."""
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit(EditOperation.EXPAND)
    expanding = stub_llm["kwargs"]["max_tokens"]

    _edit(EditOperation.PROOFREAD)
    proofreading = stub_llm["kwargs"]["max_tokens"]

    assert expanding > proofreading


def test_the_edit_runs_cool(stub_llm):
    """These are transformations of the author's text, not fresh writing.

    PROOFREAD in particular must not get creative — the operation is "fix the
    typos", and a model at 0.7 rewrites the sentence while it is in there.
    """
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit()

    assert stub_llm["kwargs"]["temperature"] <= 0.4


# --------------------------------------------------------------------------- #
# Refusing a bad reply                                                         #
# --------------------------------------------------------------------------- #


def test_a_dead_provider_chain_is_not_dressed_up_as_an_edit(stub_llm, monkeypatch):
    """Returning the selection unchanged would read as "nothing to fix"."""

    def dead(*args, **kwargs):
        raise llm_router.AllProvidersFailed("everything is down")

    monkeypatch.setattr(llm_router, "complete", dead)

    with pytest.raises(EditUnavailable) as exc:
        _edit()
    assert "try again" in str(exc.value).lower()


def test_an_empty_replacement_is_refused(stub_llm):
    stub_llm["payload"] = {"replacement": "   "}
    with pytest.raises(EditUnavailable):
        _edit()


def test_a_reply_with_no_json_in_it_is_refused(stub_llm):
    stub_llm["payload"] = "Sure! Here is the edited passage:"
    with pytest.raises(EditUnavailable):
        _edit()


def test_the_models_own_reasoning_is_refused(stub_llm):
    """Free reasoning models leak their scratchpad into the answer field."""
    stub_llm["payload"] = {
        "replacement": (
            "We need to shorten this paragraph. The user wants it cut down, so "
            "let's write a version that keeps the first sentence."
        )
    }
    with pytest.raises(EditUnavailable):
        _edit(EditOperation.SHORTEN)


def test_a_shorten_that_came_back_longer_is_refused(stub_llm):
    """The failure that looks like success until you read it.

    The model ignored the operation rather than disagreeing with it, and the
    author asked for a cut. Handing back something longer and calling it done
    is the one outcome they cannot skim past.
    """
    stub_llm["payload"] = {"replacement": PASSAGE + " And another thing entirely."}
    with pytest.raises(EditUnavailable) as exc:
        _edit(EditOperation.SHORTEN)
    assert "longer" in str(exc.value)


def test_a_runaway_reply_is_refused(stub_llm):
    """A model that started writing the whole article instead of the passage."""
    stub_llm["payload"] = {"replacement": "word " * 20_000}
    with pytest.raises(EditUnavailable):
        _edit()


def test_an_expansion_is_allowed_to_be_much_longer(stub_llm):
    """The length guard must not fire on the operation whose job is to grow."""
    stub_llm["payload"] = {"replacement": PASSAGE * 3}
    result = _edit(EditOperation.EXPAND)
    assert len(result.replacement) > len(PASSAGE)


# --------------------------------------------------------------------------- #
# Tone                                                                         #
# --------------------------------------------------------------------------- #


def test_retone_defaults_to_the_projects_own_voice(stub_llm):
    """"Make this sound like the rest of my writing" needs no parameter."""
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit(EditOperation.RETONE, project=_project(tone=Tone.CASUAL))

    assert inline_edit._TONE_GUIDANCE[Tone.CASUAL] in _prompt(stub_llm)


def test_an_explicit_tone_wins(stub_llm):
    stub_llm["payload"] = {"replacement": "x" * 40}
    _edit(EditOperation.RETONE, project=_project(tone=Tone.CASUAL), tone=Tone.MARKETING)

    prompt = _prompt(stub_llm)
    assert inline_edit._TONE_GUIDANCE[Tone.MARKETING] in prompt
    assert inline_edit._TONE_GUIDANCE[Tone.CASUAL] not in prompt


def test_the_other_operations_do_not_carry_a_tone_instruction(stub_llm):
    """Retoning as a side effect of shortening is not what was asked for."""
    stub_llm["payload"] = {"replacement": "short"}
    _edit(EditOperation.SHORTEN, project=_project(tone=Tone.MARKETING))

    assert inline_edit._TONE_GUIDANCE[Tone.MARKETING] not in _prompt(stub_llm)


def test_a_project_with_no_tone_still_works(stub_llm):
    """`project=None` is reachable — content rows can outlive their project."""
    stub_llm["payload"] = {"replacement": "x" * 40}
    result = _edit(EditOperation.RETONE, project=None)
    assert result.replacement


# --------------------------------------------------------------------------- #
# Loose typing from free models                                                #
# --------------------------------------------------------------------------- #


def test_a_replacement_returned_as_a_list_is_coerced(stub_llm):
    """`ai.as_str` exists because free models are loose about types."""
    stub_llm["payload"] = {"replacement": ["One row per platform.", " That is it."]}
    result = _edit()
    assert "One row per platform." in result.replacement


def test_the_service_never_raises_a_bare_ai_error(stub_llm, monkeypatch):
    """Callers handle one exception type. `AIError` escaping would 500."""

    def dead(*args, **kwargs):
        raise ai.AIError("nope")

    monkeypatch.setattr(ai, "json_completion", dead)

    with pytest.raises(EditUnavailable):
        _edit()
