"""Refusing an explicit ``null`` on a column that cannot hold one.

Every PATCH endpoint here builds its update with ``model_dump(exclude_unset=True)``
so a field the client did not send is left alone. That makes a field the client
*did* send as ``null`` a real instruction — and for a ``NOT NULL`` column it is
an instruction the database cannot carry out. What happened instead depended
entirely on the column type, and neither outcome was acceptable:

* **Scalar columns** raised ``IntegrityError`` on commit, which surfaced as a
  500. Bad input, reported as a server fault.
* **JSON columns** did not raise at all. SQLAlchemy writes Python ``None`` into
  a JSON column as the JSON value ``null`` rather than SQL ``NULL``, so the
  ``NOT NULL`` constraint was satisfied and the row was accepted — and every
  subsequent *read* of that row then failed response validation, because the
  schema says ``list`` and the column now held ``None``. One malformed PATCH
  left a project unreadable until somebody fixed it in the database.

:func:`reject_nulls` turns both into a 422 naming the fields. The nullable set
is derived from the mapper rather than listed, so a column that changes its
nullability cannot leave a stale allow-list behind.

:func:`app.routers.auth.update_me` predates this and hand-rolls the same check
for its single ``NOT NULL`` column; its docstring has the argument in full.
"""
from __future__ import annotations

from functools import cache
from typing import Any

from fastapi import HTTPException, status


@cache
def _not_nullable(model: type) -> frozenset[str]:
    """Column names on *model* that reject ``NULL``, by attribute name.

    Keyed on the mapper's attribute names rather than the database column
    names, because that is what a ``model_dump`` is keyed on.
    """
    mapper = model.__mapper__
    return frozenset(
        name
        for name, prop in mapper.column_attrs.items()
        if not any(column.nullable for column in prop.columns)
    )


def reject_nulls(model: type, data: dict[str, Any]) -> None:
    """422 for any ``"field": null`` in *data* that *model* cannot store.

    A no-op for fields the model does not have (a schema field that is not a
    column, like a write-only flag) and for columns that are genuinely
    nullable — clearing those is what ``null`` is *for*, and the account name
    on :func:`app.routers.auth.update_me` is the case that proves it.
    """
    offenders = sorted(
        key for key, value in data.items() if value is None and key in _not_nullable(model)
    )
    if not offenders:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            "These fields cannot be null: "
            + ", ".join(offenders)
            + ". Leave a field out of the request to keep its current value, or "
            'send an empty list or "" to clear it.'
        ),
    )


__all__ = ["reject_nulls"]
