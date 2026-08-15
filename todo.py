#!/usr/bin/env python3
"""A small, self-contained, nested todo planner backed by SQLite."""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence


VERSION = "0.20.0"
SCHEMA_VERSION = 1
FULL_ID_LENGTH = 40
DEFAULT_SHORT_ID_LENGTH = 7
MIN_REFERENCE_LENGTH = 1
STATUSES = ("open", "done")
MARKERS = {"open": "[ ]", "done": "[x]"}
CURRENT_STYLE = "\033[1;36m"
RESET_STYLE = "\033[0m"


class TodoError(Exception):
    """An expected error that should be shown without a traceback."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def style_current_line(line: str, is_current: bool) -> str:
    if (
        is_current
        and sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").casefold() != "dumb"
    ):
        return f"{CURRENT_STYLE}{line}{RESET_STYLE}"
    return line


def validate_title(raw_title: str) -> str:
    if "\n" in raw_title or "\r" in raw_title:
        raise TodoError("a title must be a single line")
    title = raw_title.strip()
    if not title:
        raise TodoError("a title cannot be empty")
    return title


def read_body_file(raw_path: str) -> str:
    try:
        if raw_path == "-":
            content = sys.stdin.read()
        else:
            content = Path(raw_path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise TodoError(f"cannot read body file {raw_path!r}: {exc}") from exc
    except UnicodeError as exc:
        raise TodoError(f"body file {raw_path!r} is not valid UTF-8") from exc
    return content.rstrip("\r\n")


def generate_public_id(connection: sqlite3.Connection) -> str:
    while True:
        public_id = secrets.token_hex(FULL_ID_LENGTH // 2)
        exists = connection.execute(
            "SELECT 1 FROM items WHERE public_id = ?",
            (public_id,),
        ).fetchone()
        if exists is None:
            return public_id


def default_database_path() -> Path:
    override = os.environ.get("TODO_DB")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "todo" / "todo.db"


def connect_database(raw_path: str) -> sqlite3.Connection:
    if raw_path != ":memory:":
        path = Path(raw_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = str(path)

    connection = sqlite3.connect(raw_path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise TodoError(
            f"database schema version {version} is not supported; "
            f"expected {SCHEMA_VERSION}"
        )
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE items (
            id          INTEGER PRIMARY KEY,
            public_id   TEXT NOT NULL
                        CHECK (
                            length(public_id) = 40
                            AND public_id NOT GLOB '*[^0-9a-f]*'
                        ),
            parent_id   INTEGER REFERENCES items(id) ON DELETE RESTRICT,
            position    INTEGER NOT NULL CHECK (position >= 0),
            title       TEXT NOT NULL
                        CHECK (
                            length(trim(title)) > 0
                            AND instr(title, char(10)) = 0
                            AND instr(title, char(13)) = 0
                        ),
            body        TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'done')),
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE UNIQUE INDEX items_public_id_idx ON items(public_id);
        CREATE INDEX items_parent_position_idx
            ON items(parent_id, position, id);
        CREATE INDEX items_status_idx ON items(status);

        CREATE TRIGGER items_public_id_insert_guard
        BEFORE INSERT ON items
        WHEN NEW.public_id IS NULL
             OR length(NEW.public_id) != 40
             OR NEW.public_id GLOB '*[^0-9a-f]*'
        BEGIN
            SELECT RAISE(ABORT, 'invalid todo public ID');
        END;

        CREATE TRIGGER items_public_id_immutable
        BEFORE UPDATE OF public_id ON items
        WHEN NEW.public_id IS NOT OLD.public_id
        BEGIN
            SELECT RAISE(ABORT, 'todo public IDs are immutable');
        END;

        CREATE TRIGGER items_title_insert_guard
        BEFORE INSERT ON items
        WHEN length(trim(NEW.title)) = 0
             OR instr(NEW.title, char(10)) > 0
             OR instr(NEW.title, char(13)) > 0
        BEGIN
            SELECT RAISE(ABORT, 'todo titles must be non-empty single lines');
        END;

        CREATE TRIGGER items_title_update_guard
        BEFORE UPDATE OF title ON items
        WHEN length(trim(NEW.title)) = 0
             OR instr(NEW.title, char(10)) > 0
             OR instr(NEW.title, char(13)) > 0
        BEGIN
            SELECT RAISE(ABORT, 'todo titles must be non-empty single lines');
        END;

        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        PRAGMA user_version = 1;
        COMMIT;
        """
    )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def get_item(connection: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise TodoError("todo data refers to an item that does not exist")
    return row


def short_id(connection: sqlite3.Connection, item: sqlite3.Row | int) -> str:
    row = get_item(connection, item) if isinstance(item, int) else item
    public_id = row["public_id"]
    for length in range(DEFAULT_SHORT_ID_LENGTH, FULL_ID_LENGTH + 1):
        prefix = public_id[:length]
        count = connection.execute(
            "SELECT COUNT(*) FROM items WHERE substr(public_id, 1, ?) = ?",
            (length, prefix),
        ).fetchone()[0]
        if count == 1:
            return prefix
    return public_id


def item_reference(connection: sqlite3.Connection, item: sqlite3.Row | int) -> str:
    return short_id(connection, item)


def get_current_id(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT value FROM settings WHERE key = 'current_item_id'"
    ).fetchone()
    if row is None:
        return None
    try:
        item_id = int(row["value"])
    except ValueError:
        return None
    item = connection.execute(
        "SELECT status FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None or item["status"] != "open":
        return None
    return item_id


def set_current_id(connection: sqlite3.Connection, item_id: int | None) -> None:
    if item_id is None:
        connection.execute("DELETE FROM settings WHERE key = 'current_item_id'")
    else:
        connection.execute(
            """
            INSERT INTO settings(key, value) VALUES ('current_item_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(item_id),),
        )


def parse_reference_prefix(raw: str) -> str:
    value = raw.strip()
    value = value.lower()
    if not (MIN_REFERENCE_LENGTH <= len(value) <= FULL_ID_LENGTH):
        raise TodoError(f"invalid todo reference: {raw!r}")
    if any(character not in "0123456789abcdef" for character in value):
        raise TodoError(f"invalid todo reference: {raw!r}")
    return value


def resolve_reference(connection: sqlite3.Connection, raw: str) -> int:
    if raw == "@":
        current = get_current_id(connection)
        if current is None:
            raise TodoError("no current todo")
        return current
    if raw == "^":
        current = get_current_id(connection)
        if current is None:
            raise TodoError("no current todo")
        parent_id = get_item(connection, current)["parent_id"]
        if parent_id is None:
            raise TodoError("the current todo is already a root")
        return parent_id

    prefix = parse_reference_prefix(raw)
    matches = list(
        connection.execute(
            """
            SELECT id, public_id FROM items
            WHERE substr(public_id, 1, ?) = ?
            LIMIT 2
            """,
            (len(prefix), prefix),
        )
    )
    if not matches:
        raise TodoError(f"todo {prefix} does not exist")
    if len(matches) > 1:
        raise TodoError(f"todo reference {prefix} is ambiguous; use more characters")
    return matches[0]["id"]


def resolve_optional_reference(
    connection: sqlite3.Connection, raw: str | None
) -> int:
    if raw is not None:
        return resolve_reference(connection, raw)
    current = get_current_id(connection)
    if current is None:
        raise TodoError("no current todo; provide a todo ID")
    return current


def item_children(
    connection: sqlite3.Connection, parent_id: int | None
) -> list[sqlite3.Row]:
    if parent_id is None:
        return list(
            connection.execute(
                """
                SELECT * FROM items
                WHERE parent_id IS NULL
                ORDER BY position, id
                """
            )
        )
    return list(
        connection.execute(
            """
            SELECT * FROM items
            WHERE parent_id = ?
            ORDER BY position, id
            """,
            (parent_id,),
        )
    )


def next_open_peer_or_parent(
    connection: sqlite3.Connection, item_id: int
) -> int | None:
    """Choose where current work advances after an item leaves open work."""
    item = get_item(connection, item_id)
    siblings = item_children(connection, item["parent_id"])
    item_index = next(
        index for index, sibling in enumerate(siblings) if sibling["id"] == item_id
    )
    ordered_candidates = siblings[item_index + 1 :] + siblings[:item_index]
    for sibling in ordered_candidates:
        if sibling["status"] == "open":
            return sibling["id"]
    return item["parent_id"]


def ancestor_rows(connection: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    ancestors: list[sqlite3.Row] = []
    seen: set[int] = set()
    current = get_item(connection, item_id)
    while current["parent_id"] is not None:
        parent_id = current["parent_id"]
        if parent_id in seen:
            raise TodoError("cycle detected in the todo hierarchy")
        seen.add(parent_id)
        current = get_item(connection, parent_id)
        ancestors.append(current)
    ancestors.reverse()
    return ancestors


def subtree_ids(connection: sqlite3.Connection, item_id: int) -> list[int]:
    rows = connection.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM items WHERE id = ?
            UNION ALL
            SELECT items.id
            FROM items
            JOIN subtree ON items.parent_id = subtree.id
        )
        SELECT id FROM subtree
        """,
        (item_id,),
    )
    return [row["id"] for row in rows]


def subtree_rows(connection: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            WITH RECURSIVE subtree(id, depth) AS (
                SELECT id, 0 FROM items WHERE id = ?
                UNION ALL
                SELECT items.id, subtree.depth + 1
                FROM items
                JOIN subtree ON items.parent_id = subtree.id
            )
            SELECT items.*, subtree.depth
            FROM subtree
            JOIN items ON items.id = subtree.id
            ORDER BY subtree.depth, items.position, items.id
            """,
            (item_id,),
        )
    )


def delete_subtree(
    connection: sqlite3.Connection, item_id: int
) -> tuple[sqlite3.Row, list[sqlite3.Row], bool, int | None]:
    """Permanently delete a subtree inside an existing transaction."""
    item = get_item(connection, item_id)
    rows = subtree_rows(connection, item_id)
    parent_id = item["parent_id"]
    deleted_ids = {row["id"] for row in rows}
    current_id = get_current_id(connection)
    current_was_deleted = current_id in deleted_ids
    next_current_id = (
        next_open_peer_or_parent(connection, item_id)
        if current_was_deleted
        else current_id
    )
    timestamp = utc_now()

    for row in reversed(rows):
        connection.execute("DELETE FROM items WHERE id = ?", (row["id"],))
    remaining_siblings = [row["id"] for row in item_children(connection, parent_id)]
    rewrite_order(connection, remaining_siblings, timestamp)
    if current_was_deleted:
        set_current_id(connection, next_current_id)

    return item, rows, current_was_deleted, next_current_id


def require_open_parent(connection: sqlite3.Connection, parent_id: int | None) -> None:
    if parent_id is None:
        return
    parent = get_item(connection, parent_id)
    if parent["status"] != "open":
        raise TodoError(
            f"cannot place open work beneath {parent['status']} todo "
            f"{item_reference(connection, parent)} {parent['title']!r}; reopen it first"
        )


def next_position(connection: sqlite3.Connection, parent_id: int | None) -> int:
    if parent_id is None:
        row = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS value "
            "FROM items WHERE parent_id IS NULL"
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS value "
            "FROM items WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
    return int(row["value"])


def rewrite_order(
    connection: sqlite3.Connection, ordered_ids: Sequence[int], timestamp: str
) -> None:
    for position, item_id in enumerate(ordered_ids):
        connection.execute(
            "UPDATE items SET position = ?, updated_at = ? WHERE id = ?",
            (position, timestamp, item_id),
        )


def format_item_line(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    current_id: int | None = None,
) -> str:
    suffix = "  ← current" if row["id"] == current_id else ""
    return (
        f"{MARKERS[row['status']]} {item_reference(connection, row)} "
        f"{row['title']}{suffix}"
    )


def print_detail_lines(
    prefix: str,
    lines: Sequence[str],
    *,
    needs_own_bar: bool,
    followed_by_children: bool,
) -> None:
    if not lines:
        return
    bar = f"{prefix}│" if needs_own_bar else prefix.rstrip()
    text_prefix = f"{prefix}│   " if needs_own_bar else prefix
    print(bar)
    for line in lines:
        print(f"{text_prefix}{line}" if line else bar)
    if followed_by_children:
        print(bar)


def display_context(
    connection: sqlite3.Connection,
    selected_id: int,
    show_full_id: bool = False,
) -> None:
    rows = list(connection.execute("SELECT * FROM items ORDER BY position, id"))
    by_id = {row["id"]: row for row in rows}
    if selected_id not in by_id:
        raise TodoError("todo data refers to an item that does not exist")

    children: dict[int | None, list[int]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row["id"])

    selected_path = [
        *(row["id"] for row in ancestor_rows(connection, selected_id)),
        selected_id,
    ]
    expanded_ids = set(selected_path)
    current_id = get_current_id(connection)
    current_path: set[int] = set()
    if current_id is not None:
        current_path.update(
            row["id"] for row in ancestor_rows(connection, current_id)
        )
        current_path.add(current_id)

    def emit_details(
        item_id: int,
        prefix: str,
        child_ids: list[int],
        needs_own_bar: bool,
    ) -> None:
        item = by_id[item_id]
        details: list[str] = []
        if show_full_id and item_id == selected_id:
            details.append(f"Full ID: {item['public_id']}")
        if item["body"]:
            if details:
                details.append("")
            details.extend(item["body"].splitlines())
        print_detail_lines(
            prefix,
            details,
            needs_own_bar=needs_own_bar,
            followed_by_children=bool(child_ids),
        )

    def emit(
        item_id: int,
        prefix: str,
        is_last: bool,
        *,
        is_root: bool = False,
    ) -> None:
        row = by_id[item_id]
        child_ids = children.get(item_id, [])
        expanded = item_id in expanded_ids
        connector = "" if is_root else ("└── " if is_last else "├── ")
        line = prefix + connector + format_item_line(connection, row, current_id)
        if not expanded and row["body"]:
            line += " …"
        if not expanded and child_ids:
            count = len(child_ids)
            line += f"  · {count} child{'ren' if count != 1 else ''}"
        if not expanded and item_id != current_id and item_id in current_path:
            line += "  ← current below"
        print(style_current_line(line, item_id == current_id))

        if not expanded:
            return
        next_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")
        emit_details(
            item_id,
            next_prefix,
            child_ids,
            is_root or is_last,
        )
        for index, child_id in enumerate(child_ids):
            emit(
                child_id,
                next_prefix,
                index == len(child_ids) - 1,
            )

    root_ids = children.get(None, [])
    for index, root_id in enumerate(root_ids):
        if index:
            print()
        emit(root_id, "", True, is_root=True)


def display_advanced_current(
    connection: sqlite3.Connection, current_id: int | None
) -> None:
    if current_id is None:
        print("No current todo.")
        return
    print()
    display_context(connection, current_id)


def display_default(connection: sqlite3.Connection) -> None:
    current_id = get_current_id(connection)
    if current_id is not None:
        display_context(connection, current_id)
        return

    roots = [row for row in item_children(connection, None) if row["status"] == "open"]
    if not roots:
        print('No open todos. Create one with: todo add "Title" --root')
        return

    print("No current todo.")
    print()
    print("Open roots:")
    for root in roots:
        line = format_item_line(connection, root)
        if root["body"]:
            line += " …"
        print(f"  {line}")
    print()
    print("Switch to one with: todo switch ID")


def command_show(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_optional_reference(connection, args.ref)
    display_context(connection, item_id, show_full_id=args.full_id)


def command_add(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    title = validate_title(" ".join(args.title))
    body = read_body_file(args.body_file) if args.body_file is not None else ""

    anchor_id: int | None = None
    insert_after = False
    if args.under is not None:
        parent_id = resolve_reference(connection, args.under)
    elif args.root:
        parent_id = None
    elif args.before is not None:
        anchor_id = resolve_reference(connection, args.before)
        parent_id = get_item(connection, anchor_id)["parent_id"]
    elif args.after is not None:
        anchor_id = resolve_reference(connection, args.after)
        parent_id = get_item(connection, anchor_id)["parent_id"]
        insert_after = True
    else:
        parent_id = get_current_id(connection)

    timestamp = utc_now()
    with transaction(connection):
        require_open_parent(connection, parent_id)
        cursor = connection.execute(
            """
            INSERT INTO items(
                public_id, parent_id, position, title, body, status,
                created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, NULL)
            """,
            (
                generate_public_id(connection),
                parent_id,
                next_position(connection, parent_id),
                title,
                body,
                timestamp,
                timestamp,
            ),
        )
        item_id = int(cursor.lastrowid)

        if anchor_id is not None:
            ordered = [row["id"] for row in item_children(connection, parent_id)]
            ordered.remove(item_id)
            anchor_index = ordered.index(anchor_id)
            ordered.insert(anchor_index + (1 if insert_after else 0), item_id)
            rewrite_order(connection, ordered, timestamp)

        if args.switch:
            set_current_id(connection, item_id)

    item = get_item(connection, item_id)
    location = (
        "as a root"
        if parent_id is None
        else f"under {item_reference(connection, parent_id)}"
    )
    reference = item_reference(connection, item)
    print(f"Added {reference} {title!r} {location}.")
    if args.switch:
        print(f"Switched to {reference}.")


def command_switch(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_reference(connection, args.ref)
    item = get_item(connection, item_id)
    if item["status"] != "open":
        raise TodoError(
            f"cannot switch to {item['status']} todo "
            f"{item_reference(connection, item)} {item['title']!r}"
        )
    with transaction(connection):
        set_current_id(connection, item_id)
    display_context(connection, item_id)


def command_update(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.title is None and args.body_file is None:
        raise TodoError("provide --title, --body-file, or both")
    item_id = resolve_optional_reference(connection, args.ref)
    item = get_item(connection, item_id)
    title = item["title"] if args.title is None else validate_title(args.title)
    body = (
        item["body"]
        if args.body_file is None
        else read_body_file(args.body_file)
    )

    if title == item["title"] and body == item["body"]:
        print(f"No changes to {item_reference(connection, item)}.")
        return

    with transaction(connection):
        connection.execute(
            "UPDATE items SET title = ?, body = ?, updated_at = ? WHERE id = ?",
            (title, body, utc_now(), item_id),
        )
    print(f"Updated {item_reference(connection, item_id)} {title!r}.")


def command_move(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_reference(connection, args.ref)
    item = get_item(connection, item_id)
    anchor_id: int | None = None
    insert_after = False

    if args.under is not None:
        new_parent_id = resolve_reference(connection, args.under)
    elif args.root:
        new_parent_id = None
    elif args.before is not None:
        anchor_id = resolve_reference(connection, args.before)
        if anchor_id == item_id:
            raise TodoError("cannot move a todo relative to itself")
        new_parent_id = get_item(connection, anchor_id)["parent_id"]
    else:
        anchor_id = resolve_reference(connection, args.after)
        if anchor_id == item_id:
            raise TodoError("cannot move a todo relative to itself")
        new_parent_id = get_item(connection, anchor_id)["parent_id"]
        insert_after = True

    if new_parent_id == item_id:
        raise TodoError("cannot move a todo beneath itself")
    descendants = set(subtree_ids(connection, item_id))
    descendants.discard(item_id)
    if new_parent_id in descendants:
        raise TodoError("cannot move a todo beneath one of its descendants")

    if item["status"] == "open":
        require_open_parent(connection, new_parent_id)

    old_parent_id = item["parent_id"]
    timestamp = utc_now()
    with transaction(connection):
        old_order = [
            row["id"] for row in item_children(connection, old_parent_id)
            if row["id"] != item_id
        ]

        if new_parent_id == old_parent_id:
            new_order = old_order
        else:
            new_order = [
                row["id"] for row in item_children(connection, new_parent_id)
                if row["id"] != item_id
            ]
            rewrite_order(connection, old_order, timestamp)

        if anchor_id is None:
            insertion_index = len(new_order)
        else:
            insertion_index = new_order.index(anchor_id) + (1 if insert_after else 0)
        new_order.insert(insertion_index, item_id)

        connection.execute(
            "UPDATE items SET parent_id = ?, updated_at = ? WHERE id = ?",
            (new_parent_id, timestamp, item_id),
        )
        rewrite_order(connection, new_order, timestamp)

    location = (
        "to the roots"
        if new_parent_id is None
        else f"under {item_reference(connection, new_parent_id)}"
    )
    print(f"Moved {item_reference(connection, item_id)} {location}.")


def complete_item(
    connection: sqlite3.Connection,
    item_id: int,
    include_tree: bool,
) -> None:
    item = get_item(connection, item_id)
    reference = item_reference(connection, item)

    if item["status"] == "done":
        print(f"{reference} {item['title']!r} is already completed.")
        return
    if item["status"] != "open":
        raise TodoError(
            f"todo {reference} is {item['status']}; "
            "reopen it before completing it"
        )

    open_children = [
        child for child in item_children(connection, item_id)
        if child["status"] == "open"
    ]
    if open_children and not include_tree:
        count = len(open_children)
        noun = "child remains" if count == 1 else "children remain"
        raise TodoError(
            f"cannot complete {reference}: {count} open {noun}; "
            f"resolve them or use --tree"
        )

    affected_subtree = subtree_ids(connection, item_id) if include_tree else [item_id]
    placeholders = ",".join("?" for _ in affected_subtree)
    open_rows = connection.execute(
        f"SELECT id FROM items WHERE id IN ({placeholders}) AND status = 'open'",
        affected_subtree,
    ).fetchall()
    changed_ids = {row["id"] for row in open_rows}
    current_id = get_current_id(connection)
    current_was_completed = current_id in changed_ids
    next_current_id = (
        next_open_peer_or_parent(connection, item_id)
        if current_was_completed
        else current_id
    )
    timestamp = utc_now()

    with transaction(connection):
        connection.execute(
            f"""
            UPDATE items
            SET status = 'done', resolved_at = ?, updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'open'
            """,
            (timestamp, timestamp, *affected_subtree),
        )
        if current_was_completed:
            set_current_id(connection, next_current_id)

    descendant_count = max(0, len(changed_ids) - 1)
    suffix = (
        f" and {descendant_count} open descendant"
        f"{'s' if descendant_count != 1 else ''}"
        if descendant_count
        else ""
    )
    print(f"Completed {reference} {item['title']!r}{suffix}.")
    if current_was_completed:
        display_advanced_current(connection, next_current_id)


def command_done(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_optional_reference(connection, args.ref)
    complete_item(connection, item_id, args.tree)


def command_reopen(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_reference(connection, args.ref)
    item = get_item(connection, item_id)
    if item["status"] == "open":
        print(f"{item_reference(connection, item)} {item['title']!r} is already open.")
        return

    lineage = [*ancestor_rows(connection, item_id), item]
    changed_ids = [row["id"] for row in lineage if row["status"] != "open"]
    placeholders = ",".join("?" for _ in changed_ids)
    with transaction(connection):
        connection.execute(
            f"""
            UPDATE items
            SET status = 'open', resolved_at = NULL, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (utc_now(), *changed_ids),
        )

    ancestor_count = len(changed_ids) - 1
    suffix = (
        f" and {ancestor_count} resolved ancestor"
        f"{'s' if ancestor_count != 1 else ''}"
        if ancestor_count
        else ""
    )
    print(
        f"Reopened {item_reference(connection, item)} "
        f"{item['title']!r}{suffix}."
    )


def command_delete(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    item_id = resolve_reference(connection, args.ref)
    item = get_item(connection, item_id)
    reference = item_reference(connection, item)
    rows = subtree_rows(connection, item_id)
    if len(rows) > 1 and not args.tree:
        child_count = len(item_children(connection, item_id))
        noun = "child" if child_count == 1 else "children"
        raise TodoError(
            f"cannot delete {item_reference(connection, item)}: "
            f"it has {child_count} {noun}; use --tree"
        )

    with transaction(connection):
        item, rows, current_was_deleted, next_current_id = delete_subtree(
            connection, item_id
        )

    descendant_count = len(rows) - 1
    suffix = (
        f" and {descendant_count} descendant"
        f"{'s' if descendant_count != 1 else ''}"
        if descendant_count
        else ""
    )
    print(f"Deleted {reference} {item['title']!r}{suffix}.")
    if current_was_deleted:
        display_advanced_current(connection, next_current_id)


def command_all(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = list(connection.execute("SELECT * FROM items ORDER BY position, id"))
    by_id = {row["id"]: row for row in rows}
    children: dict[int | None, list[int]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row["id"])

    if args.ref is None:
        root_ids = children.get(None, [])
    else:
        root_ids = [resolve_reference(connection, args.ref)]

    if not root_ids:
        print("No matching todos.")
        return

    current_id = get_current_id(connection)

    def visible_children(item_id: int) -> list[int]:
        return children.get(item_id, [])

    def emit(
        item_id: int,
        prefix: str,
        is_last: bool,
        depth: int,
        is_root: bool = False,
    ) -> None:
        child_ids = visible_children(item_id)
        connector = "" if is_root else ("└── " if is_last else "├── ")
        line = prefix + connector + format_item_line(
            connection, by_id[item_id], current_id
        )
        if not args.details and by_id[item_id]["body"]:
            line += " …"
        if args.depth is not None and depth >= args.depth and child_ids:
            count = len(child_ids)
            line += f"  · {count} child{'ren' if count != 1 else ''}"
        print(style_current_line(line, item_id == current_id))

        next_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")
        if args.details and by_id[item_id]["body"]:
            children_will_follow = bool(child_ids) and (
                args.depth is None or depth < args.depth
            )
            print_detail_lines(
                next_prefix,
                by_id[item_id]["body"].splitlines(),
                needs_own_bar=is_root or is_last,
                followed_by_children=children_will_follow,
            )

        if args.depth is not None and depth >= args.depth:
            return
        for index, child_id in enumerate(child_ids):
            emit(
                child_id,
                next_prefix,
                index == len(child_ids) - 1,
                depth + 1,
            )

    for index, root_id in enumerate(root_ids):
        if index:
            print()
        emit(root_id, "", True, 0, is_root=True)


def nonnegative_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo",
        description="An ordered, nested todo planner with one persistent current todo.",
        epilog=(
            "Todo selectors are bare hexadecimal ID prefixes. Any unambiguous prefix "
            "works; use @ for the current todo and ^ for its parent."
        ),
    )
    parser.add_argument(
        "--db",
        default=str(default_database_path()),
        metavar="PATH",
        help="SQLite database path (default: %(default)s; also TODO_DB)",
    )
    parser.add_argument("--version", action="version", version=f"todo {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    add = subparsers.add_parser("add", help="add a todo")
    add.add_argument("title", nargs="+", metavar="TITLE")
    placement = add.add_mutually_exclusive_group()
    placement.add_argument("--under", metavar="ID", help="add beneath this todo")
    placement.add_argument("--root", action="store_true", help="add as a root")
    placement.add_argument("--before", metavar="ID", help="add before this sibling")
    placement.add_argument("--after", metavar="ID", help="add after this sibling")
    add.add_argument(
        "--switch", action="store_true", help="make the new todo current"
    )
    add.add_argument(
        "--body-file",
        metavar="PATH",
        help="read the Markdown body from PATH, or from stdin with -",
    )

    show = subparsers.add_parser(
        "show", help="show the current todo in context, or inspect another"
    )
    show.add_argument(
        "ref", nargs="?", metavar="ID", help="todo to inspect (default: current)"
    )
    show.add_argument(
        "--full-id", action="store_true", help="show the complete 40-character ID"
    )

    all_view = subparsers.add_parser("all", help="show the complete todo hierarchy")
    all_view.add_argument(
        "ref", nargs="?", metavar="ID", help="limit the view to this subtree"
    )
    all_view.add_argument("--depth", type=nonnegative_integer, metavar="N")
    all_view.add_argument(
        "--details", action="store_true", help="show every displayed todo body"
    )

    switch = subparsers.add_parser("switch", help="make an open todo current")
    switch.add_argument("ref", metavar="ID")

    update = subparsers.add_parser("update", help="update a todo's title or body")
    update.add_argument("ref", nargs="?", metavar="ID")
    update.add_argument("--title", metavar="TITLE", help="replace the title")
    update.add_argument(
        "--body-file",
        metavar="PATH",
        help="replace the Markdown body from PATH, or from stdin with -",
    )

    move = subparsers.add_parser("move", help="move or reorder a todo subtree")
    move.add_argument("ref", metavar="ID")
    move_placement = move.add_mutually_exclusive_group(required=True)
    move_placement.add_argument("--under", metavar="PARENT")
    move_placement.add_argument("--root", action="store_true")
    move_placement.add_argument("--before", metavar="OTHER")
    move_placement.add_argument("--after", metavar="OTHER")

    done = subparsers.add_parser("done", help="complete a todo")
    done.add_argument("ref", nargs="?", metavar="ID")
    done.add_argument("--tree", action="store_true", help="complete its open subtree")

    reopen = subparsers.add_parser("reopen", help="reopen a resolved todo")
    reopen.add_argument("ref", metavar="ID")

    delete = subparsers.add_parser(
        "delete", help="permanently delete a todo from the list"
    )
    delete.add_argument("ref", metavar="ID")
    delete.add_argument("--tree", action="store_true", help="delete its entire subtree")

    return parser


COMMANDS = {
    "add": command_add,
    "show": command_show,
    "all": command_all,
    "switch": command_switch,
    "update": command_update,
    "move": command_move,
    "done": command_done,
    "reopen": command_reopen,
    "delete": command_delete,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(args.db)
        if args.command is None:
            display_default(connection)
        else:
            COMMANDS[args.command](connection, args)
        return 0
    except TodoError as exc:
        print(f"todo: error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"todo: database error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
