"""The data-access layer: every database touch lives under ``model/``.

SQLAlchemy 2.0 async ORM on aiosqlite; Alembic owns the schema
(``migrations/``). Code outside this package never builds queries —
it calls :class:`Model` methods. The state object follows the house
ownership rule: it takes ``app``, caches only ``self.app``, and reads
the database path live at call time.
"""

from .db import Base
from .keys import (
    classify_blobs as classify_blobs,
)
from .keys import (
    coverage_key as coverage_key,
)
from .keys import (
    hash_token as hash_token,
)
from .keys import (
    new_token as new_token,
)
from .model import Model

__all__ = ["Base", "Model", "hash_token", "new_token"]
