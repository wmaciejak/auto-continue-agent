#!/usr/bin/env python3
"""
Auto-Continue Daemon - Extended

Background service that automatically continues Codex and OpenCode sessions
when rate limits are hit.

Supported tools:
- Codex CLI (OpenAI)
- OpenCode

Detection methods:
- Codex: Polls thread_history_1.sqlite for usageLimitExceeded errors
- OpenCode: Monitors log file for "usage limit exceeded" errors

Usage:
    python daemon.py                    # Run in foreground
    python daemon.py --log-level DEBUG  # Verbose logging
"""

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Codex paths
CODEX_HOME = Path.home() / ".codex"
CODEX_THREAD_HISTORY_DB = CODEX_HOME / "thread_history_1.sqlite"
CODEX_STATE_DB = CODEX_HOME / "state_5.sqlite"

# OpenCode paths
OPENCODE_HOME = Path.home() / ".local" / "share" / "opencode"
OPENCODE_LOG_FILE = OPENCODE_HOME / "log" / "opencode.log"
OPENCODE_DB = OPENCODE_HOME / "opencode.db"

# State file (shared)
STATE_FILE = Path.home() / ".local" / "state" / "auto-continue-daemon.json"

DEFAULT_POLL_INTERVAL = 30  # seconds
DEFAULT_WAIT_MINUTES = 60
MAX_CONSECUTIVE_WAITS = 10  # safety limit
MAX_WAIT_SECONDS = 2 * 60 * 60  # 2 hours absolute max

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("auto-continue-daemon")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """Load persisted state (tracks which sessions we've already continued)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load state: {e}")
    return {"continued_sessions": {}}


def save_state(state: dict) -> None:
    """Persist state to disk."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except IOError as e:
        logger.error(f"Failed to save state: {e}")


def mark_continued(tool: str, session_id: str, turn_id: str, wait_seconds: int) -> None:
    """Mark a session/turn as handled."""
    state = load_state()
    key = f"{tool}:{session_id}"
    state["continued_sessions"][key] = {
        "tool": tool,
        "session_id": session_id,
        "turn_id": turn_id,
        "continued_at": datetime.now().isoformat(),
        "wait_seconds": wait_seconds,
    }
    save_state(state)


def already_continued(tool: str, session_id: str, turn_id: str) -> bool:
    """Check if we've already handled this specific turn."""
    state = load_state()
    key = f"{tool}:{session_id}"
    entry = state["continued_sessions"].get(key)
    if not entry:
        return False
    return entry.get("turn_id") == turn_id


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------


def parse_reset_time(time_str: str) -> Optional[datetime]:
    """
    Parse '7:54 PM' style time into a datetime.
    Assumes today's date; if the time has already passed, assumes tomorrow.
    """
    try:
        now = datetime.now()
        # Parse time like "7:54 PM" or "19:30"
        for fmt in ("%I:%M %p", "%H:%M"):
            try:
                parsed = datetime.strptime(time_str.strip(), fmt)
                # Set to today
                candidate = now.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=0,
                    microsecond=0,
                )
                # If it's in the past, assume tomorrow
                if candidate < now:
                    candidate += timedelta(days=1)
                return candidate
            except ValueError:
                continue
        return None
    except Exception as e:
        logger.warning(f"Failed to parse reset time '{time_str}': {e}")
        return None


def seconds_until(target: datetime) -> int:
    """Seconds until target datetime."""
    delta = (target - datetime.now()).total_seconds()
    return max(0, int(delta))


# ---------------------------------------------------------------------------
# Codex detection
# ---------------------------------------------------------------------------


def get_codex_rate_limit_errors(max_age_seconds: int = 300) -> list[dict]:
    """
    Query Codex thread_turns for recent usageLimitExceeded errors.
    Only returns errors from the last `max_age_seconds` seconds (default: 5 min).
    """
    if not CODEX_THREAD_HISTORY_DB.exists():
        return []

    now_sec = int(time.time())
    cutoff_sec = now_sec - max_age_seconds

    try:
        conn = sqlite3.connect(f"file:{CODEX_THREAD_HISTORY_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT 
                thread_id,
                turn_id,
                error_json,
                started_at,
                completed_at
            FROM thread_turns
            WHERE error_json LIKE '%usageLimitExceeded%'
              AND status = 'failed'
              AND completed_at >= ?
            ORDER BY started_at DESC
            LIMIT 20
        """, (cutoff_sec,)).fetchall()

        results = []
        for row in rows:
            try:
                err = json.loads(row["error_json"])
                message = err.get("message", "")
                
                # Extract "try again at <time>" pattern
                reset_match = re.search(
                    r"try again at (\d{1,2}:\d{2}\s*(?:AM|PM)?)",
                    message,
                    re.IGNORECASE,
                )
                reset_time_str = reset_match.group(1) if reset_match else None

                results.append({
                    "thread_id": row["thread_id"],
                    "turn_id": row["turn_id"],
                    "error_message": message,
                    "reset_time_str": reset_time_str,
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                })
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse Codex error_json for turn {row['turn_id']}: {e}")

        conn.close()
        return results

    except sqlite3.Error as e:
        logger.error(f"Codex database error: {e}")
        return []


def get_codex_thread_name(thread_id: str) -> str:
    """Get human-readable thread name from Codex state DB."""
    if not CODEX_STATE_DB.exists():
        return thread_id[:8]

    try:
        conn = sqlite3.connect(f"file:{CODEX_STATE_DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT name, title FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        conn.close()
        
        if row:
            return row[0] or row[1] or thread_id[:8]
        return thread_id[:8]
    except sqlite3.Error:
        return thread_id[:8]


def resume_codex_session(thread_id: str) -> bool:
    """Resume a Codex session via `codex queue --resume`."""
    cmd = [
        "codex", "queue",
        "--thread", thread_id,
        "--message", "continue where you left off",
        "--resume",
    ]
    logger.info(f"Codex: queueing continue for thread {thread_id[:8]}...")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info("Codex: successfully queued continue message")
            return True
        else:
            logger.error(f"Codex: queue failed (exit {result.returncode}): {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Codex: queue command timed out")
        return False
    except FileNotFoundError:
        logger.error("'codex' not found in PATH")
        return False


# ---------------------------------------------------------------------------
# OpenCode detection
# ---------------------------------------------------------------------------


def get_opencode_rate_limit_errors(max_age_seconds: int = 300) -> list[dict]:
    """
    Parse OpenCode log file for recent rate limit errors.
    Returns list of {session_id, error_message, reset_time_str, timestamp}.
    """
    if not OPENCODE_LOG_FILE.exists():
        return []

    now_sec = int(time.time())
    cutoff_sec = now_sec - max_age_seconds
    cutoff_ms = cutoff_sec * 1000

    results = []

    try:
        # Read last 1000 lines (log can be large)
        with open(OPENCODE_LOG_FILE, "r") as f:
            lines = f.readlines()[-1000:]

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse structured log format:
            # timestamp=2026-11-07T16:12:09.833Z level=ERROR ... sessionID=ses_...
            if "usage limit exceeded" not in line.lower():
                continue

            # Extract timestamp
            ts_match = re.search(r"timestamp=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)", line)
            if not ts_match:
                continue

            ts_str = ts_match.group(1)
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                ts_sec = int(ts.timestamp())
            except ValueError:
                continue

            # Skip if too old
            if ts_sec < cutoff_sec:
                continue

            # Extract sessionID
            session_match = re.search(r"sessionID=(ses_\w+)", line)
            session_id = session_match.group(1) if session_match else "unknown"

            # Extract error message (if present)
            error_match = re.search(r'cause="([^"]+)"', line)
            error_message = error_match.group(1) if error_match else "usage limit exceeded"

            # Try to extract reset time (if mentioned)
            reset_match = re.search(r"try again (?:in|at) (\d+:\d+\s*(?:AM|PM)?)", error_message, re.IGNORECASE)
            reset_time_str = reset_match.group(1) if reset_match else None

            results.append({
                "session_id": session_id,
                "error_message": error_message,
                "reset_time_str": reset_time_str,
                "timestamp": ts_sec,
            })

        return results

    except IOError as e:
        logger.error(f"OpenCode log file error: {e}")
        return []


def get_opencode_session_name(session_id: str) -> str:
    """Get human-readable session name from OpenCode DB."""
    if not OPENCODE_DB.exists():
        return session_id[:12] if session_id != "unknown" else "unknown"

    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT title FROM session_v2 WHERE id = ?",
            (session_id,),
        ).fetchone()
        conn.close()
        
        if row and row[0]:
            return row[0]
        return session_id[:12] if session_id != "unknown" else "unknown"
    except sqlite3.Error:
        return session_id[:12] if session_id != "unknown" else "unknown"


def resume_opencode_session(session_id: str) -> bool:
    """Resume an OpenCode session via CLI."""
    # OpenCode uses --continue or --session flag
    if session_id == "unknown":
        # Continue last session
        cmd = ["opencode", "--continue"]
    else:
        # Continue specific session
        cmd = ["opencode", "--session", session_id]

    logger.info(f"OpenCode: resuming session {session_id[:12]}...")
    
    try:
        # Run in background (don't wait for interactive session)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "OPENCODE_NON_INTERACTIVE": "1"},
        )
        # OpenCode might exit immediately if non-interactive
        if result.returncode == 0 or result.returncode == 1:
            logger.info(f"OpenCode: resume command executed (exit {result.returncode})")
            return True
        else:
            logger.warning(f"OpenCode: resume exited with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        # Timeout is expected if OpenCode starts interactive session
        logger.info("OpenCode: resume command started (interactive session)")
        return True
    except FileNotFoundError:
        logger.error("'opencode' not found in PATH")
        return False


# ---------------------------------------------------------------------------
# Unified error handling
# ---------------------------------------------------------------------------


def handle_rate_limit_error(tool: str, error: dict) -> bool:
    """
    Process a single rate limit error from any tool.
    Returns True if we successfully resumed the session.
    """
    if tool == "codex":
        session_id = error["thread_id"]
        turn_id = error["turn_id"]
        session_name = get_codex_thread_name(session_id)
        resume_fn = resume_codex_session
    elif tool == "opencode":
        session_id = error["session_id"]
        turn_id = error.get("timestamp", "unknown")
        session_name = get_opencode_session_name(session_id)
        resume_fn = resume_opencode_session
    else:
        logger.error(f"Unknown tool: {tool}")
        return False

    reset_time_str = error.get("reset_time_str")

    # Check if we've already handled this turn
    if already_continued(tool, session_id, turn_id):
        logger.debug(f"Already handled {tool} turn {turn_id} for session {session_id[:12]}")
        return False

    logger.info(f"{tool.capitalize()}: rate limit detected in session '{session_name}' ({session_id[:12]}...)")

    # Calculate wait time
    if reset_time_str:
        reset_time = parse_reset_time(reset_time_str)
        if reset_time:
            wait_seconds = seconds_until(reset_time)
            if wait_seconds > MAX_WAIT_SECONDS:
                logger.warning(
                    f"Calculated wait ({wait_seconds}s) exceeds max ({MAX_WAIT_SECONDS}s). "
                    f"Reset time '{reset_time_str}' may be stale. Using default."
                )
                wait_seconds = DEFAULT_WAIT_MINUTES * 60
            else:
                logger.info(
                    f"Reset time: {reset_time_str} ({reset_time.strftime('%H:%M:%S')}). "
                    f"Waiting {wait_seconds}s ({wait_seconds // 60}m{wait_seconds % 60}s)"
                )
        else:
            wait_seconds = DEFAULT_WAIT_MINUTES * 60
            logger.info(f"Could not parse reset time '{reset_time_str}', using default {DEFAULT_WAIT_MINUTES}m")
    else:
        wait_seconds = DEFAULT_WAIT_MINUTES * 60
        logger.info(f"No reset time in error, using default {DEFAULT_WAIT_MINUTES}m")

    # Wait
    if wait_seconds > 0:
        logger.info(f"Sleeping for {wait_seconds}s...")
        time.sleep(wait_seconds)

    # Resume session
    success = resume_fn(session_id)
    
    if success:
        mark_continued(tool, session_id, turn_id, wait_seconds)
        logger.info(f"{tool.capitalize()}: auto-continue completed for session '{session_name}'")
    
    return success


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main_loop(poll_interval: int) -> None:
    """Main daemon loop."""
    logger.info(f"Starting auto-continue daemon (poll every {poll_interval}s)")
    logger.info(f"Monitoring Codex: {CODEX_THREAD_HISTORY_DB}")
    logger.info(f"Monitoring OpenCode: {OPENCODE_LOG_FILE}")
    logger.info(f"State file: {STATE_FILE}")

    consecutive_waits = 0

    while True:
        try:
            # Check Codex
            codex_errors = get_codex_rate_limit_errors()
            if codex_errors:
                latest = codex_errors[0]
                success = handle_rate_limit_error("codex", latest)
                if success:
                    consecutive_waits += 1
                    if consecutive_waits >= MAX_CONSECUTIVE_WAITS:
                        logger.warning(
                            f"Hit {MAX_CONSECUTIVE_WAITS} consecutive waits. "
                            f"Pausing for 10 minutes to avoid infinite loop."
                        )
                        time.sleep(600)
                        consecutive_waits = 0
                    continue

            # Check OpenCode
            opencode_errors = get_opencode_rate_limit_errors()
            if opencode_errors:
                latest = opencode_errors[0]
                success = handle_rate_limit_error("opencode", latest)
                if success:
                    consecutive_waits += 1
                    if consecutive_waits >= MAX_CONSECUTIVE_WAITS:
                        logger.warning(
                            f"Hit {MAX_CONSECUTIVE_WAITS} consecutive waits. "
                            f"Pausing for 10 minutes to avoid infinite loop."
                        )
                        time.sleep(600)
                        consecutive_waits = 0
                    continue

            # No errors found
            logger.debug("No rate limit errors detected")
            consecutive_waits = 0
            time.sleep(poll_interval)

        except KeyboardInterrupt:
            logger.info("Interrupted, shutting down...")
            break
        except Exception as e:
            logger.exception(f"Unexpected error in main loop: {e}")
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def handle_signal(signum: int, frame) -> None:
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-continue daemon for Codex and OpenCode",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between checks (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    # Install signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Check prerequisites
    if not CODEX_THREAD_HISTORY_DB.exists() and not OPENCODE_LOG_FILE.exists():
        logger.error("Neither Codex nor OpenCode data sources found")
        logger.error(f"  Codex: {CODEX_THREAD_HISTORY_DB}")
        logger.error(f"  OpenCode: {OPENCODE_LOG_FILE}")
        return 1

    try:
        main_loop(args.poll_interval)
        return 0
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
