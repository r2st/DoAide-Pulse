"""The one path in Herald built from user input, and where it is allowed to go.

Herald accepts no uploads — there is no ``UploadFile`` in the tree and no
multipart endpoint — so the usual "where does the file land" question has one
answer and one place to ask it: :meth:`GitAdapter.path_for`, which turns a
``path_template`` credential and a content slug into the path of a file that is
then *committed to the user's repository*.

``test_publishers`` covers that the template interpolates and that ``../``
is refused. This file covers the rest of the surface, which is wider than it
looks because the resolved path is not only a path. It is interpolated straight
into the contents API URL::

    f"{_API}/repos/{repo}/contents/{path}"

and httpx leaves ``?``, ``#``, ``%`` and ``\\`` in a path exactly as it found
them. So the character that decides where the file lands is not always a
filesystem character:

* ``blog/{slug}.md?x`` commits to ``blog/<slug>.md``. The ``?x`` is a query
  parameter. Nothing anywhere reports that the path the user typed is not the
  path that was written.
* ``blog/{slug}.md#x`` commits to ``blog/<slug>.md`` and the fragment is never
  sent at all.
* ``%2e%2e`` is a ``..`` that the ``..`` check cannot see, decoded — if it is
  decoded — at the far end, where the decision is GitHub's rather than Herald's.

Which makes this a traversal test in a slightly unusual shape: the escape is
through the URL rather than through the tree, and it is reached by a
misconfiguration at least as often as by anyone trying.

The slug half is here too. It has never been able to carry any of this —
``slugify`` reduces a title to ``[a-z0-9-]`` — but ``path_for`` is where that
guarantee is *relied on*, and the two are four files apart.
"""
from __future__ import annotations

import re

import pytest

from app.models.content import unique_content_slug
from app.models.project import slugify
from app.services.publishers.base import CredentialError, PublishRequest
from app.services.publishers.git import GitAdapter

_CREDENTIALS = {"repo": "r2st/blog", "token": "ghp_test"}


@pytest.fixture
def request_() -> PublishRequest:
    return PublishRequest(
        title="Automating developer marketing",
        slug="automating",
        body_markdown="## Why\n\nHerald writes the posts.\n",
        excerpt="Herald writes the posts.",
        meta_description="Herald automates developer marketing end to end.",
        project_name="Herald",
    )


def _path(request_, template: str) -> str:
    return GitAdapter().path_for(request_, {**_CREDENTIALS, "path_template": template})


# --------------------------------------------------------------------------- #
# The templates that are supposed to work                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "template,expected",
    [
        # The default, and the three layouts the docstring on `path_template`
        # names: Astro, Hugo, Jekyll.
        ("src/content/blog/{slug}.md", "src/content/blog/automating.md"),
        ("content/posts/{slug}.md", "content/posts/automating.md"),
        ("{slug}.md", "automating.md"),
        # Relative-but-harmless. Normalised rather than refused: `./x` and `x`
        # are the same path, and somebody typing the first meant the second.
        ("./content/{slug}.md", "content/automating.md"),
        # A space is a legal filename character and httpx percent-encodes it on
        # the way out, so this one is allowed through where the others are not.
        ("blog posts/{slug}.md", "blog posts/automating.md"),
    ],
)
def test_a_reasonable_template_resolves_to_itself(request_, template, expected):
    """The control. Every refusal below is only meaningful against these."""
    assert _path(request_, template) == expected


def test_the_date_placeholders_still_work(request_):
    """Jekyll's layout, which is the reason the date placeholders exist."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    path = _path(request_, "_posts/{year}-{month}-{day}-{slug}.md")
    assert path == f"_posts/{now:%Y}-{now:%m}-{now:%d}-automating.md"


# --------------------------------------------------------------------------- #
# Out through the tree                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "template",
    [
        "../../etc/{slug}.md",          # the plain climb
        "../{slug}.md",                 # one level is enough
        "blog/../../{slug}.md",         # mid-path, after a legitimate segment
        "/etc/cron.d/{slug}",           # absolute
        "~/.ssh/{slug}",                # home-anchored
        "~root/{slug}.md",              # and the other ~ form
        "./../{slug}.md",               # the case the strip order exists for
    ],
)
def test_a_path_that_leaves_the_repository_is_refused(request_, template):
    with pytest.raises(CredentialError) as exc:
        _path(request_, template)
    assert "not a path inside the repository" in str(exc.value)


def test_the_leading_dot_slash_is_stripped_after_the_check_not_before(request_):
    """Order matters here and only here.

    ``"./../x.md".removeprefix("./")`` is ``"../x.md"`` — strip first and a
    traversal turns into a plausible relative path that the check never sees
    again. The parametrised case above covers it; this names why.
    """
    with pytest.raises(CredentialError):
        _path(request_, "./../{slug}.md")
    assert _path(request_, "./blog/{slug}.md") == "blog/automating.md"


# --------------------------------------------------------------------------- #
# Out through the URL                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "template,why",
    [
        ("blog/{slug}.md?ref=main", "the ? starts a query string"),
        ("blog/{slug}.md#section", "the # starts a fragment"),
        ("blog/%2e%2e/{slug}.md", "a percent-escaped .."),
        ("blog%2f{slug}.md", "a percent-escaped separator"),
        ("blog\\{slug}.md", "a backslash separator"),
        ("blog/{slug}.md\nX-Injected: 1", "a newline"),
        ("blog/\x00{slug}.md", "a NUL"),
    ],
)
def test_a_path_that_is_not_the_path_it_looks_like_is_refused(request_, template, why):
    """The characters httpx passes through unchanged, refused before they can.

    Each of these produces a URL that addresses something other than the file
    the template names, and does it silently — the commit succeeds, at a path
    the user did not write.
    """
    with pytest.raises(CredentialError) as exc:
        _path(request_, template)
    assert "not a path inside the repository" in str(exc.value), why


def test_httpx_really_does_leave_those_characters_alone():
    """The premise of the test above, asserted rather than assumed.

    If a later httpx starts percent-encoding these on its own, the refusals
    above become belt-and-braces rather than the load-bearing check they are
    today — and this is where that shows up, instead of the guard quietly
    being the only thing standing between a template and the wrong file.
    """
    import httpx

    base = "https://api.github.com/repos/o/r/contents/"
    assert str(httpx.URL(base + "x.md?y")).endswith("x.md?y")
    assert str(httpx.URL(base + "x.md#y")).endswith("x.md#y")
    assert str(httpx.URL(base + "%2e%2e/x.md")).endswith("%2e%2e/x.md")
    assert str(httpx.URL(base + "a\\b.md")).endswith("a\\b.md")
    # And the one that is encoded, which is why a space is allowed above.
    assert str(httpx.URL(base + "a b.md")).endswith("a%20b.md")


def test_a_template_resolving_to_a_directory_is_refused(request_):
    """``contents/blog/`` is a listing, not a file.

    Left through, it reaches ``_existing_sha``, comes back as a JSON list and
    is reported as "is a directory in r2st/blog, not a file" — after two API
    calls, from inside a publish, against a mistake visible in the template.
    """
    with pytest.raises(CredentialError) as exc:
        _path(request_, "blog/{slug}/")
    assert "directory, not a file" in str(exc.value)


# --------------------------------------------------------------------------- #
# The slug half                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title",
    [
        "../../etc/passwd",
        "..%2f..%2fetc",
        "post?ref=main",
        "post#fragment",
        "a/b/c",
        "~/.ssh/authorized_keys",
        "post\\windows",
        "post\nX-Injected: 1",
        "100% done",
    ],
)
def test_a_hostile_title_cannot_steer_the_commit_path(title):
    """``slugify`` is what makes the slug half of the path safe. Pin it.

    ``path_for`` checks the *formatted* path, so a slug carrying a traversal
    would be caught there too — but it would be caught as a ``CredentialError``
    blaming the template, for a piece whose title is the actual cause. The slug
    never getting there is the behaviour worth having, and it lives in a
    different file from every caller that depends on it.
    """
    slug = slugify(title)
    assert slug
    assert re.fullmatch(r"[a-z0-9-]+", slug), slug


def test_a_hostile_title_still_produces_a_usable_commit_path(request_, db, project):
    """End to end: title in, path out, through the real slug the router stores."""
    from dataclasses import replace

    slug = unique_content_slug(db, project.id, "../../etc/passwd?x=1")
    path = _path(replace(request_, slug=slug), "src/content/blog/{slug}.md")
    assert path == f"src/content/blog/{slug}.md"
    assert ".." not in path and "?" not in path


def test_an_empty_slug_falls_back_rather_than_writing_a_dotfile(request_):
    """``""`` in ``{slug}.md`` is ``.md`` — a hidden file, silently.

    Only reachable for a request built by hand; content always carries a slug.
    Cheap to hold anyway, since the fallback is one line and the failure is
    invisible in a repo listing.
    """
    from dataclasses import replace

    assert _path(replace(request_, slug=""), "{slug}.md") == "post.md"
    assert _path(replace(request_, slug="   "), "blog/{slug}.md") == "blog/post.md"


# --------------------------------------------------------------------------- #
# The other half of the credential                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "repo",
    ["r2st/blog/../../other", "r2st/blog?x=1", "../../../user/repos", "r2st blog"],
)
def test_the_repository_is_owner_slash_name_and_nothing_else(repo):
    """The path is not the only user-controlled segment of that URL.

    ``_repo`` has always enforced this; it is asserted here beside the path
    checks because the two are the same question about the same URL, and a
    reader who finds one guard tends to assume the other exists.
    """
    with pytest.raises(CredentialError) as exc:
        GitAdapter()._repo({"repo": repo})
    assert "is not an owner/name repository" in str(exc.value)
