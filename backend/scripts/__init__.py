"""One-shot jobs, each run as ``python -m scripts.<name>``.

Importing the package registers every table (apps/schema.py): a script that loads one table
module, at import time or inside a function, would otherwise leave foreign keys to other
modules' tables unresolved and fail its first flush with NoReferencedTableError
(tests/apps/test_schema.py).
"""
import bfx_funding_bot.apps.schema  # noqa: F401
