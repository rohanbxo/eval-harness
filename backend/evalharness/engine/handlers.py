"""Handler tools: name -> callable registry, plus the v1 SQLite handlers (SPEC 5.3).

A handler receives ``(args, state, config)``:

``args``
    The model's arguments, already validated against the tool's JSON Schema.
``state``
    The per-attempt scratch dict owned by :class:`~evalharness.engine.tool_engine.ToolEngine`,
    cleared between attempts. Handlers cache expensive setup here.
``config``
    The tool's ``mock.config`` block, plus the reserved key ``_scenario_dir``
    that the engine injects so relative paths resolve without the handler
    needing the loader (see ``docs/DECISIONS.md`` D6).

Handlers must be deterministic and must never raise: a bad argument from the
model is a graded behavior, so problems come back as ``ok=False`` results.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from evalharness.schema.runtime import ToolResult

__all__ = [
    "MAX_ROWS",
    "SCENARIO_DIR_KEY",
    "HandlerFn",
    "get_handler",
    "handler_names",
    "leading_sql_keyword",
    "register_handler",
    "split_sql_statements",
]

HandlerFn = Callable[[Mapping[str, JsonValue], dict[str, Any], Mapping[str, JsonValue]], ToolResult]

#: Reserved ``config`` key holding the scenario directory as an absolute path.
SCENARIO_DIR_KEY = "_scenario_dir"

#: Row cap for ``sqlite_query`` (SPEC 5.3). Overridable per tool via ``config.max_rows``.
MAX_ROWS = 500

#: Where cached per-attempt SQLite connections live inside ``state``.
_DB_STATE_KEY = "_sqlite_connections"

_REGISTRY: dict[str, HandlerFn] = {}


def register_handler(name: str, fn: HandlerFn) -> None:
    """Register (or replace) a handler under ``name``."""
    _REGISTRY[name] = fn


def get_handler(name: str) -> HandlerFn:
    """Look up a handler, naming every known handler when the lookup fails."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "<none registered>"
        raise KeyError(f"unknown handler {name!r}; known handlers: {known}") from None


def handler_names() -> list[str]:
    """Every registered handler name, sorted."""
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #


def _config_str(config: Mapping[str, JsonValue], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) else None


def _config_int(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _error(code: str, message: str, **extra: JsonValue) -> ToolResult:
    content: dict[str, JsonValue] = {"error": code, "message": message}
    content.update(extra)
    return ToolResult(ok=False, error=code, content=content)


# --------------------------------------------------------------------------- #
# SQL statement inspection
# --------------------------------------------------------------------------- #


def strip_sql_noise(sql: str) -> str:
    """Blank out comments and string/identifier literals, preserving offsets.

    Splitting and keyword inspection run over this masked copy so a semicolon or
    a ``DROP`` inside ``'a string'`` cannot change how the statement is read.
    """
    out = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            for pos in range(index, end):
                out[pos] = " "
            index = end
        elif char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            for pos in range(index, end):
                out[pos] = " "
            index = end
        elif char in "'\"`[":
            closer = "]" if char == "[" else char
            pos = index + 1
            while pos < length:
                if sql[pos] == closer:
                    # '' and "" are escaped quotes inside the same literal.
                    if closer != "]" and sql.startswith(closer * 2, pos):
                        pos += 2
                        continue
                    break
                pos += 1
            end = min(pos + 1, length)
            for blank in range(index, end):
                out[blank] = " "
            index = end
        else:
            index += 1
    return "".join(out)


def split_sql_statements(sql: str) -> list[str]:
    """Split on statement-terminating semicolons, ignoring comments and literals."""
    masked = strip_sql_noise(sql)
    statements: list[str] = []
    start = 0
    for index, char in enumerate(masked):
        if char == ";":
            chunk = sql[start:index]
            if masked[start:index].strip():
                statements.append(chunk.strip())
            start = index + 1
    if masked[start:].strip():
        statements.append(sql[start:].strip())
    return statements


def leading_sql_keyword(statement: str) -> str:
    """The first keyword of a statement, ignoring comments, literals and parens."""
    masked = strip_sql_noise(statement).strip().lstrip("(").strip()
    words = masked.split(None, 1)
    return words[0].upper() if words else ""


#: Authorizer actions a read-only query legitimately needs.
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

#: Functions that reach outside the database, denied even though they are reads.
_DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


def _readonly_authorizer(action: int, arg1: str | None, arg2: str | None, *_: str | None) -> int:
    """SQLite's own authorizer callback -- the real read-only enforcement.

    Keyword inspection alone is bypassable; this runs during statement
    preparation and vetoes every action a ``SELECT`` does not need, whatever the
    text looked like.
    """
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (arg1 or "").lower() == "sqlite_dbpage":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# --------------------------------------------------------------------------- #
# database setup
# --------------------------------------------------------------------------- #


def _resolve_seed(config: Mapping[str, JsonValue]) -> Path | str:
    """Resolve ``config.seed_file`` against the scenario directory, or return a reason."""
    seed = _config_str(config, "seed_file")
    if not seed:
        return "handler config is missing the 'seed_file' key naming a .sql file"
    path = Path(seed)
    if not path.is_absolute():
        base = _config_str(config, SCENARIO_DIR_KEY)
        if base is None:
            return f"cannot resolve relative seed path {seed!r}: no scenario directory in config"
        path = Path(base) / path
    if not path.is_file():
        return f"seed file not found: {path}"
    return path


def _connect(state: dict[str, Any], config: Mapping[str, JsonValue]) -> sqlite3.Connection | str:
    """The attempt's connection for this seed file, creating it on first use.

    The database lives in memory and is seeded once per attempt; the connection
    is cached in ``state`` so a scenario that runs ten queries pays for the seed
    exactly once.
    """
    resolved = _resolve_seed(config)
    if isinstance(resolved, str):
        return resolved

    cache: dict[str, sqlite3.Connection] = state.setdefault(_DB_STATE_KEY, {})
    key = str(resolved)
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        script = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"could not read seed file {resolved}: {exc}"

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(script)
        connection.commit()
        # Belt and braces alongside the authorizer: SQLite itself refuses every
        # write on this connection from here on.
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        connection.close()
        return f"could not seed the database from {resolved.name}: {exc}"

    cache[key] = connection
    return connection


def close_connections(state: Mapping[str, Any]) -> None:
    """Close every cached connection. Called by the engine when state is reset."""
    cache = state.get(_DB_STATE_KEY)
    if not isinstance(cache, dict):
        return
    for connection in cache.values():
        if isinstance(connection, sqlite3.Connection):
            connection.close()
    cache.clear()


def _cell(value: object) -> JsonValue:
    """Coerce one SQLite cell to JSON. BLOBs become their hex representation."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)  # pragma: no cover - sqlite3 returns nothing else by default


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[JsonValue]:
    return [{key: _cell(value) for key, value in dict(row).items()} for row in rows]


def _query(
    connection: sqlite3.Connection, sql: str, params: tuple[str, ...] = ()
) -> list[sqlite3.Row]:
    """Run an internal (harness-authored) query with no authorizer installed."""
    cursor = connection.execute(sql, params)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


def sqlite_query(
    args: Mapping[str, JsonValue],
    state: dict[str, Any],
    config: Mapping[str, JsonValue],
) -> ToolResult:
    """Run a single read-only ``SELECT`` (or ``WITH ... SELECT``) (SPEC 5.3)."""
    sql_arg = _config_str(config, "sql_arg")
    candidates = [sql_arg] if sql_arg else ["sql", "query", "statement"]
    raw = next(
        (args[name] for name in candidates if name and isinstance(args.get(name), str)), None
    )
    if not isinstance(raw, str) or not raw.strip():
        return _error(
            "invalid_arguments",
            f"expected a SQL string in one of: {', '.join(n for n in candidates if n)}",
        )

    statements = split_sql_statements(raw)
    if len(statements) == 0:
        return _error("invalid_arguments", "no SQL statement was supplied")
    if len(statements) > 1:
        return _error(
            "read_only_violation",
            f"only one statement may be executed; got {len(statements)}",
            statements=len(statements),
        )
    statement = statements[0]
    keyword = leading_sql_keyword(statement)
    if keyword not in {"SELECT", "WITH", "VALUES"}:
        return _error(
            "read_only_violation",
            f"this database is read-only: only SELECT/WITH is allowed, got {keyword or '<empty>'}",
        )

    connection = _connect(state, config)
    if isinstance(connection, str):
        return _error("database_unavailable", connection)

    max_rows = _config_int(config, "max_rows", MAX_ROWS)
    connection.set_authorizer(_readonly_authorizer)
    try:
        cursor = connection.execute(statement)
        columns = [description[0] for description in (cursor.description or [])]
        rows = cursor.fetchmany(max_rows + 1)
        cursor.close()
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        code = "read_only_violation" if "not authorized" in message else "sql_error"
        return _error(code, message)
    finally:
        connection.set_authorizer(None)

    truncated = len(rows) > max_rows
    kept = rows[:max_rows]
    return ToolResult(
        ok=True,
        content={
            "columns": columns,
            "rows": _rows_to_dicts(kept),
            "row_count": len(kept),
            "truncated": truncated,
        },
    )


def sqlite_list_tables(
    args: Mapping[str, JsonValue],
    state: dict[str, Any],
    config: Mapping[str, JsonValue],
) -> ToolResult:
    """List the user tables and views in the seeded database (SPEC 5.3)."""
    del args
    connection = _connect(state, config)
    if isinstance(connection, str):
        return _error("database_unavailable", connection)
    rows = _query(
        connection,
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return ToolResult(
        ok=True,
        content={
            "tables": [str(row["name"]) for row in rows],
            "views": [str(row["name"]) for row in rows if row["type"] == "view"],
        },
    )


def sqlite_get_schema(
    args: Mapping[str, JsonValue],
    state: dict[str, Any],
    config: Mapping[str, JsonValue],
) -> ToolResult:
    """Describe one table, or every table when no table is named (SPEC 5.3)."""
    connection = _connect(state, config)
    if isinstance(connection, str):
        return _error("database_unavailable", connection)

    table_arg = _config_str(config, "table_arg") or "table"
    wanted = args.get(table_arg)
    if wanted is not None and not isinstance(wanted, str):
        return _error("invalid_arguments", f"{table_arg} must be a string")

    rows = _query(
        connection,
        "SELECT name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    names = [str(row["name"]) for row in rows]
    if wanted is not None:
        matched = next((n for n in names if n.lower() == wanted.lower()), None)
        if matched is None:
            return _error(
                "not_found",
                f"no table named {wanted!r}",
                tables=[str(n) for n in names],
            )
        names = [matched]

    creates = {str(row["name"]): row["sql"] for row in rows}
    tables: list[JsonValue] = []
    for name in names:
        # Identifiers cannot be bound as parameters; the name came from
        # sqlite_master, so it is a real table, not model-supplied text.
        info = _query(connection, f'PRAGMA table_info("{name.replace(chr(34), chr(34) * 2)}")')
        tables.append(
            {
                "name": name,
                "sql": creates.get(name) if isinstance(creates.get(name), str) else None,
                "columns": [
                    {
                        "name": str(column["name"]),
                        "type": str(column["type"]),
                        "not_null": bool(column["notnull"]),
                        "primary_key": bool(column["pk"]),
                    }
                    for column in info
                ],
            }
        )
    return ToolResult(ok=True, content={"tables": tables})


register_handler("sqlite_query", sqlite_query)
register_handler("sqlite_list_tables", sqlite_list_tables)
register_handler("sqlite_get_schema", sqlite_get_schema)
