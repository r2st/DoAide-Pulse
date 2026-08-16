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

A ``dict[str, str]`` field has the same hole twice over, because a mapping has
two sides: ``ConnectionCreate.credentials`` said ``min_length=1``, which is a
floor on the number of entries and a bound on nothing at all.

These are annotations rather than validators on purpose: a constraint declared
in the type appears in the generated OpenAPI, so a client can see the limit
before it sends anything, and a validator's ``ValueError`` cannot say which
entry of the list was the long one while ``StringConstraints`` names the index.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from app.models.content import TAG_MAX_LENGTH
from app.services.scheduling import TIMEZONE_MAX_LENGTH
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

#: An IANA timezone name on a scheduling request.
#:
#: Bounded at the schema edge as well as inside
#: :func:`app.services.scheduling.resolve_zone` because the two refusals happen
#: at different moments and only one of them is free. Pydantic rejects an
#: over-long string before any route body runs; the service check is what covers
#: the callers that reach it without a request behind them. The value itself is
#: still resolved by the service — a length is not a spelling.
Timezone = Annotated[str, StringConstraints(max_length=TIMEZONE_MAX_LENGTH)]

#: The most credential fields one platform connection may carry.
#:
#: The widest adapter asks for five (``git``). Twenty is room for one that has
#: not been written yet, and still a bound — the router refuses any key the
#: adapter did not declare, but it does that *after* pydantic has parsed the
#: body, so a payload of a million keys was a million keys parsed before
#: anything looked at them.
MAX_CREDENTIAL_FIELDS = 20

#: The longest a credential *key* may be.
#:
#: Every real one is an identifier of twenty characters or fewer
#: (``application_password`` is the longest). The bound matters because an
#: unknown key is echoed back in the 400 that rejects it — the message names
#: which field was not recognised, which is only useful if the field name is
#: something a human could have typed.
CREDENTIAL_KEY_MAX_LENGTH = 100

#: The longest a credential *value* may be.
#:
#: Deliberately generous: these are API keys, app passwords, handles, repo
#: paths and site URLs, none of which run past a few hundred characters, but a
#: signed token can be long and refusing a legitimate one would lock a user out
#: of their own platform. What matters is that the ceiling exists. Without it
#: an oversized value was handed to ``adapter.verify`` — which sends it to the
#: platform over the network — and then encrypted and written to a ``Text``
#: column with no width of its own to refuse it.
CREDENTIAL_VALUE_MAX_LENGTH = 2000

#: One key in a ``credentials`` mapping.
CredentialKey = Annotated[str, StringConstraints(max_length=CREDENTIAL_KEY_MAX_LENGTH)]

#: One value in a ``credentials`` mapping.
CredentialValue = Annotated[
    str, StringConstraints(max_length=CREDENTIAL_VALUE_MAX_LENGTH)
]

__all__ = [
    "CREDENTIAL_KEY_MAX_LENGTH",
    "CREDENTIAL_VALUE_MAX_LENGTH",
    "MAX_CREDENTIAL_FIELDS",
    "CredentialKey",
    "CredentialValue",
    "Keyword",
    "Tag",
    "TechStackEntry",
    "Timezone",
]
