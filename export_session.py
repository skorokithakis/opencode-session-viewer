# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Export OpenCode session data to JSON for use with the session viewer.

Usage:
    uv run export_session.py                     # Interactive: lists sessions to choose from
    uv run export_session.py <session_id>        # Export specific session
    uv run export_session.py --output out.json   # Specify output file (default: session_data.json)

Or run directly from GitHub:
    uv run https://raw.githubusercontent.com/ericmjl/opencode-session-viewer/main/export_session.py
"""

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


def get_base_path() -> Path:
    """Get the OpenCode base data directory."""
    return Path.home() / ".local/share/opencode"


def get_storage_path() -> Path:
    """Get the legacy file-based OpenCode storage path."""
    return get_base_path() / "storage"


def get_db_path() -> Path:
    """Get the SQLite database path used by recent OpenCode versions."""
    return get_base_path() / "opencode.db"


def open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the OpenCode SQLite database read-only.

    Read-only mode is mandatory: OpenCode may be running and we must not
    interfere with its writes, and the user explicitly asked us not to delete
    or modify anything.
    """
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_json(path: Path) -> dict:
    """Load a JSON file."""
    with open(path) as f:
        return json.load(f)


def list_sessions_legacy(storage_path: Path) -> list[dict]:
    """List sessions from the legacy file storage."""
    sessions = []
    session_base = storage_path / "session"

    if not session_base.exists():
        return sessions

    # Check all subdirectories (global and project-specific).
    for subdir in session_base.iterdir():
        if subdir.is_dir():
            for session_file in subdir.glob("*.json"):
                try:
                    data = load_json(session_file)
                    data["_source"] = "legacy"
                    sessions.append(data)
                except Exception:
                    continue
    return sessions


def list_sessions_db(db_path: Path) -> list[dict]:
    """List sessions from the SQLite database."""
    if not db_path.exists():
        return []

    connection = open_db_readonly(db_path)
    try:
        cursor = connection.execute(
            "SELECT id, title, directory, time_created, time_updated, time_archived "
            "FROM session ORDER BY time_updated DESC"
        )
        sessions = []
        for row in cursor:
            session_id, title, directory, time_created, time_updated, time_archived = row
            sessions.append(
                {
                    "id": session_id,
                    "title": title or "Untitled",
                    "directory": directory or "",
                    "time": {
                        "created": time_created,
                        "updated": time_updated,
                    },
                    "time_archived": time_archived,
                    "_source": "db",
                }
            )
        return sessions
    finally:
        connection.close()


def list_sessions(storage_path: Path, db_path: Path) -> list[dict]:
    """List sessions from both backends, deduped (db wins on conflict)."""
    by_id: dict[str, dict] = {}
    # Legacy first so DB entries overwrite stale legacy copies.
    for session in list_sessions_legacy(storage_path):
        by_id[session["id"]] = session
    for session in list_sessions_db(db_path):
        by_id[session["id"]] = session

    sessions = list(by_id.values())
    sessions.sort(key=lambda s: s.get("time", {}).get("updated", 0) or 0, reverse=True)
    return sessions


def format_timestamp(ts: int) -> str:
    """Format a millisecond timestamp to human readable."""
    if not ts:
        return "Unknown"
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def get_message_parts(storage_path: Path, msg_id: str) -> list[dict]:
    """Load all parts for a message."""
    part_dir = storage_path / "part" / msg_id
    if not part_dir.exists():
        return []

    parts = []
    for part_file in sorted(part_dir.glob("*.json")):
        try:
            parts.append(load_json(part_file))
        except Exception:
            continue
    return parts


def export_session_legacy(storage_path: Path, session_id: str) -> list[dict] | None:
    """Export a session's messages from legacy file storage. Returns None if absent."""
    message_path = storage_path / "message" / session_id
    if not message_path.exists():
        return None

    messages = []
    for msg_file in message_path.glob("*.json"):
        try:
            msg = load_json(msg_file)
            msg["parts"] = get_message_parts(storage_path, msg["id"])
            messages.append(msg)
        except Exception as e:
            print(f"Warning: Failed to load message {msg_file}: {e}", file=sys.stderr)
            continue
    return messages


def export_session_db(db_path: Path, session_id: str) -> list[dict] | None:
    """Export a session's messages from SQLite. Returns None if absent.

    The `data` column on messages and parts holds a partial JSON blob; it
    lacks identity fields like `id`, `sessionID`, `messageID`. We merge those
    back in from the row columns to reproduce the legacy JSON shape that the
    downstream viewer expects.
    """
    if not db_path.exists():
        return None

    connection = open_db_readonly(db_path)
    try:
        # Confirm the session exists in the DB before claiming a hit.
        exists = connection.execute(
            "SELECT 1 FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            return None

        message_rows = connection.execute(
            "SELECT id, time_created, time_updated, data FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()

        messages = []
        for message_id, time_created, time_updated, data_text in message_rows:
            message = json.loads(data_text)
            message["id"] = message_id
            message["sessionID"] = session_id
            # Prefer the embedded time object (it may carry extra fields like
            # `completed`), but fall back to row columns when missing.
            time_obj = message.get("time") or {}
            time_obj.setdefault("created", time_created)
            time_obj.setdefault("updated", time_updated)
            message["time"] = time_obj

            part_rows = connection.execute(
                "SELECT id, data FROM part WHERE message_id = ? ORDER BY id",
                (message_id,),
            ).fetchall()
            parts = []
            for part_id, part_data_text in part_rows:
                part = json.loads(part_data_text)
                part["id"] = part_id
                part["messageID"] = message_id
                part["sessionID"] = session_id
                parts.append(part)
            message["parts"] = parts
            messages.append(message)
        return messages
    finally:
        connection.close()


def export_session(storage_path: Path, db_path: Path, session_id: str) -> dict:
    """Export a session, preferring the SQLite backend over legacy files."""
    messages = export_session_db(db_path, session_id)
    if messages is None:
        messages = export_session_legacy(storage_path, session_id)
    if messages is None:
        raise ValueError(f"Session not found: {session_id}")

    messages.sort(key=lambda m: m.get("time", {}).get("created", 0) or 0)

    return {
        "sessionID": session_id,
        "exportedAt": datetime.now().isoformat(),
        "messageCount": len(messages),
        "messages": messages,
    }


def interactive_select(sessions: list[dict]) -> str | None:
    """Let user interactively select a session."""
    if not sessions:
        print("No sessions found.")
        return None

    print("\nAvailable OpenCode sessions:\n")
    print(f"{'#':<4} {'Updated':<18} {'Dir':<40} {'Title':<50}")
    print("-" * 115)

    for i, session in enumerate(sessions, 1):
        updated = format_timestamp(session.get("time", {}).get("updated"))
        directory = session.get("directory", "")
        # Shorten directory for display
        if len(directory) > 38:
            directory = "..." + directory[-35:]
        title = session.get("title", "Untitled")[:48]
        print(f"{i:<4} {updated:<18} {directory:<40} {title:<50}")

    print()

    try:
        choice = input("Enter session number (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            return None

        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]["id"]
        else:
            print("Invalid selection.")
            return None
    except (ValueError, KeyboardInterrupt):
        return None


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Export OpenCode session data to JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                           # Interactive session selection
  %(prog)s ses_abc123...             # Export specific session
  %(prog)s --list                    # List all sessions
  %(prog)s --output my_session.json  # Custom output filename
        """,
    )
    parser.add_argument("session_id", nargs="?", help="Session ID to export")
    parser.add_argument(
        "--list", "-l", action="store_true", help="List available sessions"
    )
    parser.add_argument(
        "--output", "-o", default="session_data.json", help="Output filename"
    )

    args = parser.parse_args()

    storage_path = get_storage_path()
    db_path = get_db_path()

    if not storage_path.exists() and not db_path.exists():
        print(
            f"OpenCode data not found at {get_base_path()} "
            f"(neither {storage_path} nor {db_path})",
            file=sys.stderr,
        )
        sys.exit(1)

    sessions = list_sessions(storage_path, db_path)

    if args.list:
        if not sessions:
            print("No sessions found.")
        else:
            print(f"\nFound {len(sessions)} sessions:\n")
            for session in sessions:
                updated = format_timestamp(session.get("time", {}).get("updated"))
                print(f"  {session['id']}")
                print(f"    Title: {session.get('title', 'Untitled')}")
                print(f"    Directory: {session.get('directory', 'Unknown')}")
                print(f"    Updated: {updated}")
                print()
        sys.exit(0)

    # Get session ID
    session_id = args.session_id
    if not session_id:
        session_id = interactive_select(sessions)
        if not session_id:
            sys.exit(0)

    # Export
    print(f"Exporting session: {session_id}")

    try:
        data = export_session(storage_path, db_path, session_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Write output
    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(data, f)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(
        f"Exported {data['messageCount']} messages to {output_path} ({size_mb:.1f} MB)"
    )
    print(f"\nTo view: open index.html and load {output_path}")


if __name__ == "__main__":
    main()
