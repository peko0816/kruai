"""Declarative base.

docs/DATA_MODEL.sql is authoritative; these models exist to mirror it, not to
redefine it. tests/integration/test_schema_matches_ddl.py builds one database
from that file and another from the migration and fails on any difference, so a
model edited without a matching DDL edit is caught mechanically.

No naming_convention is configured. The DDL relies on PostgreSQL's own
auto-generated constraint names (``users_telegram_id_key``,
``courses_course_type_check``), and a convention would rename them all. Check
constraints are therefore named explicitly at each site — an unnamed
table-level CHECK in SQLAlchemy would come out as ``<table>_check`` rather than
the ``<table>_<column>_check`` PostgreSQL produces for an inline one.

No ``relationship()`` anywhere yet. Adding them before a query needs one buys
nothing and invites MissingGreenlet failures under the async session, since a
lazy load cannot run inside an awaited context. Add each one where it is first
needed, with an explicit loader strategy.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for every KruAI table."""
