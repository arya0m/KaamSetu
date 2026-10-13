"""Enumerations shared by models and schemas. Values are taken verbatim from PRD.md."""

import enum

from sqlalchemy import Enum as SAEnum


class Trade(enum.StrEnum):
    ELECTRICIAN = "electrician"
    PLUMBER = "plumber"
    TAILOR = "tailor"
    DRIVER = "driver"
    BEAUTICIAN = "beautician"
    CARPENTER = "carpenter"
    DELIVERY = "delivery"
    OTHER = "other"


class JobStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    HIDDEN = "hidden"
    CLOSED = "closed"


class ApplicationStatus(enum.StrEnum):
    INTERESTED = "interested"
    SHORTLISTED = "shortlisted"
    HIRED = "hired"
    REJECTED = "rejected"


class MessageDirection(enum.StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


def pg_enum(enum_cls: type[enum.StrEnum], name: str) -> SAEnum:
    """Native PostgreSQL enum storing the lowercase values, not member names."""
    return SAEnum(enum_cls, name=name, values_callable=lambda cls: [m.value for m in cls])
