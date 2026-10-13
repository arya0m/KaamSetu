"""P1-foundation: data model from PRD.md, the Alembic migration, and their agreement.

Metadata and offline-SQL tests always run. Tests marked `db` apply the
migration to a real PostgreSQL+pgvector (TEST_DATABASE_URL).
"""

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationStatus,
    AuditLog,
    Base,
    Course,
    Employer,
    Job,
    JobStatus,
    Match,
    Message,
    MessageDirection,
    Trade,
    VoiceNote,
    Worker,
    WorkerProfile,
)

API_DIR = Path(__file__).resolve().parents[1]

# Key columns per table, verbatim from the PRD data model.
PRD_TABLES: dict[str, set[str]] = {
    "workers": {"wa_phone_hash", "wa_phone_encrypted", "language", "consent_at", "state"},
    "worker_profiles": {"worker_id", "profile_json", "confirmed", "embedding", "lat", "lng"},
    "voice_notes": {"worker_id", "media_url", "transcript", "language", "duration_s"},
    "employers": {"email", "password_hash", "company_name", "verified"},
    "jobs": {
        "employer_id",
        "title",
        "trade",
        "skills",
        "pay_min",
        "pay_max",
        "lat",
        "lng",
        "openings",
        "status",
        "embedding",
    },
    "matches": {"worker_id", "job_id", "score", "sent_at", "worker_response"},
    "applications": {"worker_id", "job_id", "status"},
    "courses": {"skill", "title", "url", "language"},
    "messages": {"worker_id", "direction", "type", "wa_message_id", "status"},
    "audit_log": {"actor", "action", "entity", "entity_id"},
}
COMMON = {"id", "created_at", "updated_at"}


def _alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


# --- always-on tests -------------------------------------------------------


def test_p1_models_define_every_prd_table_and_column() -> None:
    tables = Base.metadata.tables
    assert set(tables) == set(PRD_TABLES)
    for name, cols in PRD_TABLES.items():
        assert set(tables[name].columns.keys()) == cols | COMMON, name


def test_p1_every_table_has_uuid_pk_and_timestamps() -> None:
    for table in Base.metadata.tables.values():
        assert [c.name for c in table.primary_key.columns] == ["id"]
        assert table.c.id.type.python_type is uuid.UUID
        created_type = table.c.created_at.type
        assert isinstance(created_type, DateTime) and created_type.timezone is True
        assert table.c.updated_at.onupdate is not None


def test_p1_enums_match_prd() -> None:
    assert [s.value for s in JobStatus] == ["draft", "active", "hidden", "closed"]
    assert [s.value for s in ApplicationStatus] == [
        "interested",
        "shortlisted",
        "hired",
        "rejected",
    ]
    assert {t.value for t in Trade} == {
        "electrician",
        "plumber",
        "tailor",
        "driver",
        "beautician",
        "carpenter",
        "delivery",
        "other",
    }


def test_p1_vector_columns_use_configured_dim() -> None:
    for table in ("worker_profiles", "jobs"):
        vector_type = Base.metadata.tables[table].c.embedding.type
        assert isinstance(vector_type, Vector) and vector_type.dim == 768


def test_p1_offline_migration_sql_creates_all_tables(capsys: pytest.CaptureFixture[str]) -> None:
    buf = io.StringIO()
    cfg = _alembic_config("postgresql+psycopg://offline@localhost/offline")
    cfg.output_buffer = buf
    command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in PRD_TABLES:
        assert f"CREATE TABLE {table} (" in sql
    assert sql.count("VECTOR(768)") == 2
    assert "uq_matches_worker_id_job_id" in sql
    assert "uq_messages_wa_message_id" in sql


# --- tests against a real PostgreSQL + pgvector ----------------------------


@pytest.fixture
def migrated(database_url: str) -> Iterator[Engine]:
    cfg = _alembic_config(database_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()
    command.downgrade(cfg, "base")


@pytest.mark.db
def test_p1_migration_matches_models(migrated: Engine) -> None:
    with migrated.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


@pytest.mark.db
def test_p1_migration_installs_pgvector_and_tables(migrated: Engine) -> None:
    with migrated.connect() as conn:
        ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).first()
    assert ext is not None
    assert set(PRD_TABLES) <= set(inspect(migrated).get_table_names())


@pytest.mark.db
def test_p1_downgrade_removes_everything(database_url: str) -> None:
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(database_url)
    try:
        assert not set(PRD_TABLES) & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _seed(session: Session) -> tuple[Worker, Job]:
    worker = Worker(wa_phone_hash="h" * 64, wa_phone_encrypted=b"enc", state="new")
    employer = Employer(email="e@example.com", password_hash="x", company_name="Acme")
    session.add_all([worker, employer])
    session.flush()
    job = Job(
        employer_id=employer.id,
        title="Electrician",
        trade=Trade.ELECTRICIAN,
        skills=["wiring"],
        pay_min=500,
        pay_max=800,
        lat=19.07,
        lng=72.87,
        embedding=[0.1] * 768,
    )
    session.add(job)
    session.flush()
    return worker, job


@pytest.mark.db
def test_p1_all_models_round_trip(migrated: Engine) -> None:
    with Session(migrated) as session:
        worker, job = _seed(session)
        session.add_all(
            [
                WorkerProfile(
                    worker_id=worker.id,
                    profile_json={"primary_trade": "electrician"},
                    embedding=[0.2] * 768,
                    lat=19.0,
                    lng=72.8,
                ),
                VoiceNote(worker_id=worker.id, media_url="wa://media/1", duration_s=12.5),
                Match(worker_id=worker.id, job_id=job.id, score=0.8),
                Application(worker_id=worker.id, job_id=job.id),
                Course(
                    skill="wiring", title="Basics", url="https://skillsbuild.org/x", language="hi"
                ),
                Message(
                    worker_id=worker.id,
                    direction=MessageDirection.INBOUND,
                    type="text",
                    wa_message_id="wamid.1",
                    status="received",
                ),
                AuditLog(actor="worker", action="consent", entity="workers", entity_id=worker.id),
            ]
        )
        session.commit()
        session.refresh(job)
        assert job.status is JobStatus.DRAFT
        assert job.openings == 1
        assert job.created_at is not None
        app_row = session.query(Application).one()
        assert app_row.status is ApplicationStatus.INTERESTED
        assert len(session.query(WorkerProfile).one().embedding) == 768


@pytest.mark.db
@pytest.mark.parametrize("model", [Match, Application])
def test_p1_unique_worker_job(migrated: Engine, model: type[Match] | type[Application]) -> None:
    with Session(migrated) as session:
        worker, job = _seed(session)
        extra = {"score": 0.7} if model is Match else {}
        session.add(model(worker_id=worker.id, job_id=job.id, **extra))
        session.flush()
        session.add(model(worker_id=worker.id, job_id=job.id, **extra))
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.db
def test_p1_messages_idempotent_on_wa_message_id(migrated: Engine) -> None:
    with Session(migrated) as session:
        for _ in range(2):
            session.add(
                Message(
                    direction=MessageDirection.INBOUND,
                    type="text",
                    wa_message_id="wamid.dup",
                    status="received",
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.db
def test_p1_deleting_worker_cascades_but_keeps_audit(migrated: Engine) -> None:
    with Session(migrated) as session:
        worker, job = _seed(session)
        session.add_all(
            [
                VoiceNote(worker_id=worker.id, media_url="wa://m"),
                Application(worker_id=worker.id, job_id=job.id),
                AuditLog(actor="worker", action="delete", entity="workers", entity_id=worker.id),
            ]
        )
        session.commit()
        session.execute(text("DELETE FROM workers WHERE id = :id"), {"id": worker.id})
        session.commit()
        assert session.query(VoiceNote).count() == 0
        assert session.query(Application).count() == 0
        assert session.query(AuditLog).count() == 1


@pytest.mark.db
def test_p1_job_pay_range_check(migrated: Engine) -> None:
    with Session(migrated) as session:
        _, job = _seed(session)
        job.pay_min = 900
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.db
def test_p1_readyz_green_against_real_services(migrated: Engine, redis_url: str) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        resp = c.get("/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
