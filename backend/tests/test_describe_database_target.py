"""The boot-log line that names the database host must never carry credentials."""

from __future__ import annotations

from evalharness.db.session import describe_database_target

SECRET = "s3cr3t-pw-do-not-log"


def test_names_host_port_and_database() -> None:
    line = describe_database_target(
        f"postgresql+asyncpg://app:{SECRET}@postgres.railway.internal:5432/railway"
    )
    assert line == "database target: host=postgres.railway.internal port=5432 database=railway"


def test_password_and_user_never_appear() -> None:
    line = describe_database_target(f"postgresql+asyncpg://app:{SECRET}@db.test:5432/x")
    assert SECRET not in line
    assert "app" not in line


def test_missing_parts_are_marked_not_blank() -> None:
    assert describe_database_target("postgresql+asyncpg:///x") == (
        "database target: host=<none> port=<default> database=x"
    )


def test_unparseable_url_is_not_echoed() -> None:
    bad = f"not a url {SECRET}"
    line = describe_database_target(bad)
    assert line == "database target: <unparseable DATABASE_URL>"
    assert SECRET not in line
