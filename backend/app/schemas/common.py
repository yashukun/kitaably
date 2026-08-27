"""Shared response shapes.

Lists are paginated from day one. A bare array is a breaking change waiting to
happen, and the migration away from one always arrives at the worst moment.
"""

from pydantic import BaseModel


class Page[T](BaseModel):
    """Cursor-paginated list: ``?limit=&cursor=``.

    ``next_cursor`` is null until a list grows enough to need it. The envelope
    exists from day one so adding paging later is not a breaking change.
    """

    items: list[T]
    next_cursor: str | None = None

