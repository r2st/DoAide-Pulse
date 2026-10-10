"""Edge cases in the revision diff that the restore tests do not exercise.

The existing ``test_an_edit_can_be_taken_back`` covers the happy path of diff
through the API. This file exercises the service-level edge cases: the
DIFF_LINE_LIMIT clipping, the ``_as_text`` conversion for None and list values,
and the ``changed`` property on a ``FieldDiff``.
"""
from __future__ import annotations

from app.services.revisions import DIFF_LINE_LIMIT, FieldDiff, _as_text, diff


class _FakeRow:
    """A stand-in for either a Content row or a ContentRevision row.

    Both types carry the same seven tracked fields, which is what makes
    ``diff()`` work on either without a conversion step.
    """

    def __init__(self, **kwargs):
        defaults = {
            "title": "",
            "body_markdown": "",
            "excerpt": "",
            "meta_description": "",
            "keywords": [],
            "tags": [],
            "focus_keyword": "",
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestAsText:
    def test_none_becomes_empty_string(self):
        assert _as_text(None) == ""

    def test_string_passes_through(self):
        assert _as_text("hello") == "hello"

    def test_list_is_comma_separated(self):
        assert _as_text(["a", "b", "c"]) == "a, b, c"

    def test_empty_list_is_empty_string(self):
        assert _as_text([]) == ""


class TestFieldDiff:
    def test_changed_is_true_when_different(self):
        fd = FieldDiff(field="title", before="A", after="B", unified=[])
        assert fd.changed is True

    def test_changed_is_false_when_same(self):
        fd = FieldDiff(field="title", before="A", after="A", unified=[])
        assert fd.changed is False

    def test_as_dict_includes_changed(self):
        fd = FieldDiff(field="title", before="A", after="B", unified=[])
        d = fd.as_dict()
        assert d["changed"] is True
        assert d["field"] == "title"
        assert d["before"] == "A"
        assert d["after"] == "B"


class TestDiff:
    def test_identical_rows_produce_no_diffs(self):
        a = _FakeRow(title="Same", body_markdown="Same body")
        b = _FakeRow(title="Same", body_markdown="Same body")
        assert diff(a, b) == []

    def test_only_changed_fields_appear(self):
        a = _FakeRow(title="Old Title", body_markdown="Same")
        b = _FakeRow(title="New Title", body_markdown="Same")
        result = diff(a, b)
        fields = {d.field for d in result}
        assert fields == {"title"}

    def test_body_diff_produces_unified_lines(self):
        a = _FakeRow(body_markdown="line one\nline two\n")
        b = _FakeRow(body_markdown="line one\nline changed\n")
        result = diff(a, b)
        body_diff = next(d for d in result if d.field == "body_markdown")
        assert any(line.startswith("-") for line in body_diff.unified)
        assert any(line.startswith("+") for line in body_diff.unified)

    def test_short_field_has_no_unified_lines(self):
        a = _FakeRow(focus_keyword="old")
        b = _FakeRow(focus_keyword="new")
        result = diff(a, b)
        fk_diff = next(d for d in result if d.field == "focus_keyword")
        assert fk_diff.unified == []
        assert fk_diff.before == "old"
        assert fk_diff.after == "new"

    def test_list_field_reordering_is_a_change(self):
        a = _FakeRow(tags=["alpha", "beta"])
        b = _FakeRow(tags=["beta", "alpha"])
        result = diff(a, b)
        tags_diff = next(d for d in result if d.field == "tags")
        assert tags_diff.before == "alpha, beta"
        assert tags_diff.after == "beta, alpha"

    def test_large_body_diff_is_clipped(self):
        old_lines = [f"line {i}" for i in range(300)]
        new_lines = [f"changed {i}" for i in range(300)]
        a = _FakeRow(body_markdown="\n".join(old_lines))
        b = _FakeRow(body_markdown="\n".join(new_lines))
        result = diff(a, b)
        body_diff = next(d for d in result if d.field == "body_markdown")
        assert len(body_diff.unified) <= DIFF_LINE_LIMIT + 1
        assert "not shown" in body_diff.unified[-1]

    def test_none_vs_empty_string_is_no_change(self):
        a = _FakeRow(meta_description=None)
        b = _FakeRow(meta_description="")
        result = diff(a, b)
        assert not any(d.field == "meta_description" for d in result)

    def test_keywords_none_vs_empty_list_is_no_change(self):
        a = _FakeRow(keywords=None)
        b = _FakeRow(keywords=[])
        result = diff(a, b)
        assert not any(d.field == "keywords" for d in result)

    def test_excerpt_produces_unified_lines(self):
        a = _FakeRow(excerpt="First line.\nSecond line.")
        b = _FakeRow(excerpt="First line.\nThird line.")
        result = diff(a, b)
        exc_diff = next(d for d in result if d.field == "excerpt")
        assert len(exc_diff.unified) > 0
