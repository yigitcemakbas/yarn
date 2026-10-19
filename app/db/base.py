"""Declarative base. Deliberately has no knowledge of concrete models, see app/models/__init__.py for the import hub that registers them on
Base.metadata (avoids a circular import between this module and the
model modules, which each import Base from here).
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
