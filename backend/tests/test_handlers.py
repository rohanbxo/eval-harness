"""Handler registry and SQLite handler tests (SPEC 5.3).

The read-only tests matter most: the model controls the SQL text, so the
enforcement has to hold against a statement that *starts* as a legitimate
``SELECT`` and turns into something else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from evalharness.engine.handlers import (
    MAX_ROWS,
    SCENARIO_DIR_KEY,
    _readonly_authorizer,  # the security boundary, tested directly
    close_connections,
    get_handler,
    handler_names,
    leading_sql_keyword,
    register_handler,
    split_sql_statements,
    sqlite_get_schema,
    sqlite_list_tables,
    sqlite_query,
    strip_sql_noise,
)
from evalharness.schema.runtime import ToolResult


@pytest.fixture(scope="session")
def demo_dir(data_dir: Path) -> Path:
    return data_dir / "ok" / "demo-scenario"


@pytest.fixture
def state() -> Iterator[dict[str, Any]]:
    scratch: dict[str, Any] = {}
    yield scratch
    close_connections(scratch)


@pytest.fixture
def config(demo_dir: Path) -> dict[str, JsonValue]:
    return {"seed_file": "seed.sql", SCENARIO_DIR_KEY: str(demo_dir)}


def content(result: ToolResult) -> dict[str, JsonValue]:
    assert isinstance(result.content, dict)
    return result.content


def query(state: dict[str, Any], config: Mapping[str, JsonValue], sql: str) -> ToolResult:
    return sqlite_query({"sql": sql}, state, config)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_v1_handlers_are_registered(self) -> None:
        assert {"sqlite_query", "sqlite_list_tables", "sqlite_get_schema"} <= set(handler_names())

    def test_get_handler_returns_the_callable(self) -> None:
        assert get_handler("sqlite_query") is sqlite_query

    def test_unknown_handler_names_the_known_ones(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            get_handler("sqlite_delete_everything")
        message = str(excinfo.value)
        assert "sqlite_delete_everything" in message
        assert "sqlite_query" in message

    def test_register_handler_round_trip(self) -> None:
        def noop(
            args: Mapping[str, JsonValue],
            state: dict[str, Any],
            config: Mapping[str, JsonValue],
        ) -> ToolResult:
            return ToolResult(ok=True, content=None)

        register_handler("test_noop", noop)
        assert get_handler("test_noop") is noop
        assert "test_noop" in handler_names()


# --------------------------------------------------------------------------- #
# SQL text inspection
# --------------------------------------------------------------------------- #


class TestStatementSplitting:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT 1", 1),
            ("SELECT 1;", 1),
            ("  ;  ", 0),
            ("", 0),
            ("SELECT 1; SELECT 2", 2),
            ("SELECT * FROM t; DROP TABLE t;", 2),
            ("SELECT 'a; b'", 1),
            ('SELECT "col; name" FROM t', 1),
            ("SELECT [odd; name] FROM t", 1),
            ("SELECT `back; tick` FROM t", 1),
            ("SELECT 1 -- ; not a statement\n", 1),
            ("SELECT /* ; */ 1", 1),
            ("SELECT 'it''s; fine'", 1),
        ],
    )
    def test_split(self, sql: str, expected: int) -> None:
        assert len(split_sql_statements(sql)) == expected

    def test_unterminated_literals_and_comments_do_not_hang(self) -> None:
        assert len(split_sql_statements("SELECT 'unterminated")) == 1
        assert len(split_sql_statements("SELECT 1 /* unterminated")) == 1
        assert len(split_sql_statements("SELECT [unterminated")) == 1

    @pytest.mark.parametrize(
        ("sql", "keyword"),
        [
            ("SELECT 1", "SELECT"),
            ("  select 1", "SELECT"),
            ("/* lead */ WITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
            ("(SELECT 1) UNION (SELECT 2)", "SELECT"),
            ("DROP TABLE t", "DROP"),
            ("", ""),
            ("-- only a comment", ""),
        ],
    )
    def test_leading_keyword(self, sql: str, keyword: str) -> None:
        assert leading_sql_keyword(sql) == keyword

    def test_masking_preserves_offsets(self) -> None:
        sql = "SELECT 'a; b' FROM t"
        masked = strip_sql_noise(sql)
        assert len(masked) == len(sql)
        assert ";" not in masked


# --------------------------------------------------------------------------- #
# sqlite_query
# --------------------------------------------------------------------------- #


class TestSqliteQuery:
    def test_select_returns_rows_as_dicts(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT code, city FROM airports ORDER BY code")
        assert result.ok
        body = content(result)
        assert body["columns"] == ["code", "city"]
        assert body["rows"] == [
            {"code": "DXB", "city": "Dubai"},
            {"code": "JED", "city": "Jeddah"},
            {"code": "RUH", "city": "Riyadh"},
        ]
        assert body["row_count"] == 3
        assert body["truncated"] is False

    def test_with_select_is_allowed(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(
            state,
            config,
            "WITH cheap AS (SELECT * FROM bookings WHERE price_aed < 800) "
            "SELECT flight_id FROM cheap",
        )
        assert result.ok
        assert content(result)["rows"] == [{"flight_id": "FL-101"}]

    def test_blobs_become_hex(self, state: dict[str, Any], config: dict[str, JsonValue]) -> None:
        result = query(state, config, "SELECT payload FROM bookings WHERE id = 1")
        assert content(result)["rows"] == [{"payload": "0102"}]

    def test_rows_are_capped_and_flagged(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(
            state,
            config,
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600) "
            "SELECT i FROM n",
        )
        assert result.ok
        body = content(result)
        assert body["row_count"] == MAX_ROWS
        assert body["truncated"] is True

    def test_the_cap_is_configurable(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = sqlite_query(
            {"sql": "SELECT code FROM airports"}, state, {**config, "max_rows": 2}
        )
        body = content(result)
        assert body["row_count"] == 2
        assert body["truncated"] is True

    def test_a_nonsense_max_rows_falls_back_to_the_default(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = sqlite_query(
            {"sql": "SELECT code FROM airports"}, state, {**config, "max_rows": "lots"}
        )
        assert content(result)["truncated"] is False

    def test_the_connection_is_seeded_once_per_attempt(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        query(state, config, "SELECT 1 AS a")
        connections = [c for c in state.values() if isinstance(c, dict)]
        assert connections, "the handler should cache its connection in state"
        first = next(iter(connections[0].values()))
        query(state, config, "SELECT 2 AS a")
        assert next(iter(connections[0].values())) is first

    def test_reset_closes_cached_connections(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        query(state, config, "SELECT 1 AS a")
        cache = next(c for c in state.values() if isinstance(c, dict))
        connection = next(iter(cache.values()))
        close_connections(state)
        assert cache == {}
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_close_connections_tolerates_unrelated_state(self) -> None:
        close_connections({})
        close_connections({"_sqlite_connections": "not a cache"})
        close_connections({"_sqlite_connections": {"stale": "not a connection"}})

    def test_sql_errors_come_back_as_results(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT * FROM does_not_exist")
        assert not result.ok
        assert result.error == "sql_error"
        assert "no such table" in str(content(result)["message"])

    def test_the_sql_argument_name_is_configurable(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        assert sqlite_query({"query": "SELECT 1 AS a"}, state, config).ok
        assert sqlite_query({"statement": "SELECT 1 AS a"}, state, config).ok
        named = sqlite_query({"q": "SELECT 1 AS a"}, state, {**config, "sql_arg": "q"})
        assert named.ok

    def test_a_missing_or_empty_sql_argument_is_an_error(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        assert sqlite_query({}, state, config).error == "invalid_arguments"
        assert sqlite_query({"sql": 42}, state, config).error == "invalid_arguments"
        assert query(state, config, "   ").error == "invalid_arguments"
        assert query(state, config, "-- just a comment").error == "invalid_arguments"


# --------------------------------------------------------------------------- #
# read-only enforcement
# --------------------------------------------------------------------------- #


class TestReadOnlyEnforcement:
    def test_a_trailing_drop_does_not_slip_past(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT * FROM bookings; DROP TABLE bookings")
        assert not result.ok
        assert result.error == "read_only_violation"
        assert "one statement" in str(content(result)["message"])
        assert query(state, config, "SELECT COUNT(*) AS n FROM bookings").ok

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM bookings",
            "DROP TABLE bookings",
            "UPDATE bookings SET price_aed = 0",
            "INSERT INTO bookings (id, flight_id, price_aed) VALUES (3, 'X', 1)",
            "CREATE TABLE evil (a TEXT)",
            "ALTER TABLE bookings ADD COLUMN evil TEXT",
            "PRAGMA table_list",
            "ATTACH DATABASE 'other.db' AS other",
            "VACUUM",
            "BEGIN",
        ],
    )
    def test_non_select_statements_are_refused(
        self, state: dict[str, Any], config: dict[str, JsonValue], sql: str
    ) -> None:
        result = query(state, config, sql)
        assert not result.ok
        assert result.error == "read_only_violation"

    def test_a_with_prefix_cannot_smuggle_a_write_past_the_keyword_check(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        """The leading keyword is ``WITH``; only SQLite's authorizer catches this."""
        result = query(
            state,
            config,
            "WITH doomed AS (SELECT id FROM bookings) "
            "DELETE FROM bookings WHERE id IN (SELECT id FROM doomed)",
        )
        assert not result.ok
        assert result.error == "read_only_violation"
        after = query(state, config, "SELECT COUNT(*) AS n FROM bookings")
        assert content(after)["rows"] == [{"n": 2}]

    def test_a_comment_cannot_hide_a_second_statement(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT 1 /* sneaky */ ; DELETE FROM bookings")
        assert result.error == "read_only_violation"

    def test_semicolons_inside_string_literals_are_fine(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT 'DROP TABLE bookings; --' AS joke")
        assert result.ok
        assert content(result)["rows"] == [{"joke": "DROP TABLE bookings; --"}]

    def test_load_extension_is_denied(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = query(state, config, "SELECT load_extension('evil.so')")
        assert not result.ok
        assert result.error in {"read_only_violation", "sql_error"}


# --------------------------------------------------------------------------- #
# seeding failures
# --------------------------------------------------------------------------- #


class TestSeeding:
    def test_missing_seed_key(self, state: dict[str, Any]) -> None:
        result = sqlite_query({"sql": "SELECT 1"}, state, {})
        assert result.error == "database_unavailable"
        assert "seed" in str(content(result)["message"])

    def test_missing_seed_file(self, state: dict[str, Any], demo_dir: Path) -> None:
        result = sqlite_query(
            {"sql": "SELECT 1"},
            state,
            {"seed_file": "nope.sql", SCENARIO_DIR_KEY: str(demo_dir)},
        )
        assert result.error == "database_unavailable"
        assert "not found" in str(content(result)["message"])

    def test_relative_seed_without_a_scenario_directory(self, state: dict[str, Any]) -> None:
        result = sqlite_query({"sql": "SELECT 1"}, state, {"seed_file": "seed.sql"})
        assert result.error == "database_unavailable"
        assert "scenario directory" in str(content(result)["message"])

    def test_an_absolute_seed_path_needs_no_scenario_directory(
        self, state: dict[str, Any], demo_dir: Path
    ) -> None:
        result = sqlite_query(
            {"sql": "SELECT COUNT(*) AS n FROM airports"},
            state,
            {"seed_file": str(demo_dir / "seed.sql")},
        )
        assert result.ok
        assert content(result)["rows"] == [{"n": 3}]

    def test_a_broken_seed_script_is_reported(self, state: dict[str, Any], tmp_path: Path) -> None:
        seed = tmp_path / "broken.sql"
        seed.write_text("CREATE TABLE (;", encoding="utf-8")
        result = sqlite_query({"sql": "SELECT 1"}, state, {"seed_file": str(seed)})
        assert result.error == "database_unavailable"
        assert "could not seed" in str(content(result)["message"])

    def test_an_unreadable_seed_file_is_reported(
        self, state: dict[str, Any], tmp_path: Path
    ) -> None:
        seed = tmp_path / "binary.sql"
        seed.write_bytes(bytes([0xFF, 0xFE]) + b"CREATE TABLE t (a TEXT);")
        result = sqlite_query({"sql": "SELECT 1"}, state, {"seed_file": str(seed)})
        assert result.error == "database_unavailable"
        assert "could not read" in str(content(result)["message"])

    def test_writes_are_impossible_even_outside_the_authorizer(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        """``PRAGMA query_only`` backs up the authorizer on the shared connection."""
        query(state, config, "SELECT 1 AS a")
        cache = next(c for c in state.values() if isinstance(c, dict))
        connection = next(iter(cache.values()))
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM bookings")


# --------------------------------------------------------------------------- #
# introspection handlers
# --------------------------------------------------------------------------- #


class TestIntrospection:
    def test_list_tables(self, state: dict[str, Any], config: dict[str, JsonValue]) -> None:
        result = sqlite_list_tables({}, state, config)
        assert result.ok
        body = content(result)
        assert body["tables"] == ["airports", "bookings", "refundable_bookings"]
        assert body["views"] == ["refundable_bookings"]

    def test_list_tables_reports_seed_problems(self, state: dict[str, Any]) -> None:
        assert sqlite_list_tables({}, state, {}).error == "database_unavailable"

    def test_schema_for_every_table(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        body = content(sqlite_get_schema({}, state, config))
        tables = body["tables"]
        assert isinstance(tables, list)
        assert [t["name"] for t in tables if isinstance(t, dict)] == [
            "airports",
            "bookings",
            "refundable_bookings",
        ]

    def test_schema_for_one_table(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        body = content(sqlite_get_schema({"table": "AIRPORTS"}, state, config))
        tables = body["tables"]
        assert isinstance(tables, list) and len(tables) == 1
        table = tables[0]
        assert isinstance(table, dict)
        assert table["name"] == "airports"
        assert "CREATE TABLE" in str(table["sql"])
        assert table["columns"] == [
            {"name": "code", "type": "TEXT", "not_null": False, "primary_key": True},
            {"name": "city", "type": "TEXT", "not_null": True, "primary_key": False},
        ]

    def test_schema_for_an_unknown_table(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = sqlite_get_schema({"table": "ghosts"}, state, config)
        assert result.error == "not_found"
        assert content(result)["tables"] == ["airports", "bookings", "refundable_bookings"]

    def test_schema_rejects_a_non_string_table_argument(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = sqlite_get_schema({"table": 7}, state, config)
        assert result.error == "invalid_arguments"

    def test_the_table_argument_name_is_configurable(
        self, state: dict[str, Any], config: dict[str, JsonValue]
    ) -> None:
        result = sqlite_get_schema({"name": "airports"}, state, {**config, "table_arg": "name"})
        tables = content(result)["tables"]
        assert isinstance(tables, list) and len(tables) == 1

    def test_schema_reports_seed_problems(self, state: dict[str, Any]) -> None:
        assert sqlite_get_schema({}, state, {}).error == "database_unavailable"


# --------------------------------------------------------------------------- #
# the authorizer itself
# --------------------------------------------------------------------------- #


class TestAuthorizer:
    """The authorizer is the security boundary, so it is tested directly."""

    def test_reads_and_selects_are_allowed(self) -> None:
        assert _readonly_authorizer(sqlite3.SQLITE_SELECT, None, None) == sqlite3.SQLITE_OK
        assert _readonly_authorizer(sqlite3.SQLITE_READ, "airports", "code") == sqlite3.SQLITE_OK
        assert _readonly_authorizer(sqlite3.SQLITE_FUNCTION, None, "max") == sqlite3.SQLITE_OK

    def test_every_other_action_is_denied(self) -> None:
        for action in (
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
        ):
            assert _readonly_authorizer(action, None, None) == sqlite3.SQLITE_DENY

    def test_escape_hatch_functions_are_denied(self) -> None:
        denied = _readonly_authorizer(sqlite3.SQLITE_FUNCTION, None, "load_extension")
        assert denied == sqlite3.SQLITE_DENY

    def test_raw_page_access_is_denied(self) -> None:
        denied = _readonly_authorizer(sqlite3.SQLITE_READ, "sqlite_dbpage", "data")
        assert denied == sqlite3.SQLITE_DENY
