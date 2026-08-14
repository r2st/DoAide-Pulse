"""Bounds shared by more than one request schema.

Every field in the tree is bounded somewhere; these are the bounds that two
schemas have to agree about, kept in one place so they cannot drift apart.

The bug class this closes is specific. A ``list[str]`` field with
``max_length=30`` reads as bounded and is not: the number caps how *many*
entries arrive, and nothing caps how long each one is. Thirty entries of a
megabyte each satisfied every check ``ContentCreate`` had, and the values went
on to a JSON column — which, unlike a ``String(n)``, has no width of its own to
refuse them — and from there into an LLM prompt (``project.tech_stack``) or
straight out to a publishing adapter (``content.tags``).

These are annotations rather than validators on purpose: a constraint declared
in the type appears in the generated OpenAPI, so a client can see the limit
before it sends anything, and a validator's ``ValueError`` cannot say which
entry of the list was the long one while ``StringConstraints`` names the index.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from app.models.content import TAG_MAX_LENGTH
from app.services.seo import KEYWORD_MAX_LENGTH

#: One entry in a ``tags`` list.
Tag = Annotated[str, StringConstraints(max_length=TAG_MAX_LENGTH)]

#: One entry in a ``keywords`` list.
#:
#: The bound is imported from :mod:`app.services.seo` rather than repeated
#: because :func:`~app.services.seo.normalize_keywords` already *drops* entries
#: longer than it. Accepting one at the edge and discarding it two lines later
#: is the worst of the three options: the caller is told the value was stored.
Keyword = Annotated[str, StringConstraints(max_length=KEYWORD_MAX_LENGTH)]

#: One entry in ``project.tech_stack``.
#:
#: Same bound, because this list has the same shape — short labels, shown in the
#: same panel — and a different one for each would be a number to look up rather
#: than a rule to remember. It is the list with the longest reach, though:
#: :func:`app.services.content_generator.build_brief` joins it into the prompt
#: verbatim, so "twenty-five entries" was a description of the count and not of
#: the size.
TechStackEntry = Annotated[str, StringConstraints(max_length=TAG_MAX_LENGTH)]

__all__ = ["Keyword", "Tag", "TechStackEntry"]
