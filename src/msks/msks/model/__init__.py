"""The data-access layer: every database touch lives under ``model/``.

SQLAlchemy 2.0 async ORM on aiosqlite; Alembic owns the schema
(``migrations/``). Code outside this package never builds queries —
it calls :class:`Model` methods. The state object follows the house
ownership rule: it takes ``app``, caches only ``self.app``, and reads
the database path live at call time.
"""

from .db import Base
from .model import Model, hash_token, new_token

__all__ = ["Base", "Model", "hash_token", "new_token"]
