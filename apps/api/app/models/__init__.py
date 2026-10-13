"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.models.audit import AuditLog, Message
from app.models.base import Base, TimestampedBase
from app.models.employer import Employer, Job
from app.models.enums import ApplicationStatus, JobStatus, MessageDirection, Trade
from app.models.matching import Application, Course, Match
from app.models.worker import VoiceNote, Worker, WorkerProfile

__all__ = [
    "Application",
    "ApplicationStatus",
    "AuditLog",
    "Base",
    "Course",
    "Employer",
    "Job",
    "JobStatus",
    "Match",
    "Message",
    "MessageDirection",
    "TimestampedBase",
    "Trade",
    "VoiceNote",
    "Worker",
    "WorkerProfile",
]
