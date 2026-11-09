# Auto-Continue Daemon

Background service that automatically continues Codex and OpenCode sessions when rate limits are hit.

## Supported Tools

- **Codex CLI** (OpenAI) - monitors `~/.codex/thread_history_1.sqlite`
- **OpenCode** - monitors `~/.local/share/opencode/log/opencode.log`

## How it works

### Codex Detection
1. Polls `thread_history_1.sqlite` every 30 seconds
2. Detects `usageLimitExceeded` errors from the last 5 minutes
3. Extracts reset time from error message ("try again at 7:54 PM")
4. Waits until reset time (max 2h cap to avoid stale errors)
5. Automatically runs `codex queue --thread <id> --message "continue" --resume`

### OpenCode Detection
1. Tails `opencode.log` for "usage limit exceeded" errors
2. Extracts sessionID from log entry
3. Waits for default duration (60 minutes)
4. Resumes session with `opencode --session <id>` or `opencode --continue`

## Installation

No dependencies required — uses only Python 3.7+ standard library.

```bash
cd /home/dev/projects/auto-continue-agent
chmod +x daemon.py
```

## Usage

### Run in foreground

```bash
./daemon.py
./daemon.py --poll-interval 30 --log-level DEBUG
```

### Install as systemd service

```bash
# Copy service file
cp auto-continue-daemon.service ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable auto-continue-daemon.service
systemctl --user start auto-continue-daemon.service

# Check status
systemctl --user status auto-continue-daemon.service

# View logs
journalctl --user -u auto-continue-daemon.service -f
```

## Configuration

Edit constants at the top of `daemon.py`:

- `DEFAULT_POLL_INTERVAL` — Seconds between database checks (default: 30)
- `DEFAULT_WAIT_MINUTES` — Default wait time if reset time not in error (default: 60)
- `MAX_CONSECUTIVE_WAITS` — Safety limit to prevent infinite loops (default: 10)
- `MAX_WAIT_SECONDS` — Absolute max wait time (default: 7200 = 2 hours)

## State file

Tracks which sessions have been continued: `~/.local/state/auto-continue-daemon.json`

Format:
```json
{
  "continued_sessions": {
    "codex:01a1bb89-...": {
      "tool": "codex",
      "session_id": "01a1bb89-...",
      "turn_id": "01a1bb90-...",
      "continued_at": "2026-11-09T21:30:00",
      "wait_seconds": 3600
    },
    "opencode:ses_abc123...": {
      "tool": "opencode",
      "session_id": "ses_abc123...",
      "turn_id": "1794089566",
      "continued_at": "2026-11-09T21:35:00",
      "wait_seconds": 3600
    }
  }
}
```

## Logging

Logs to stdout/stderr. With systemd, logs go to the journal.

Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`

Example log output:
```
2026-11-09 21:30:37 [INFO] Starting auto-continue daemon (poll every 30s)
2026-11-09 21:30:37 [INFO] Monitoring Codex: /home/dev/.codex/thread_history_1.sqlite
2026-11-09 21:30:37 [INFO] Monitoring OpenCode: /home/dev/.local/share/opencode/log/opencode.log
2026-11-09 21:30:37 [INFO] State file: /home/dev/.local/state/auto-continue-daemon.json
2026-11-09 21:35:15 [INFO] Codex: rate limit detected in thread 'Fix tests' (01a1bb89...)
2026-11-09 21:35:15 [INFO] Reset time: 7:54 PM (19:54:00). Waiting 3600s (60m0s)
2026-11-09 21:35:15 [INFO] Sleeping for 3600s...
2026-11-09 22:35:15 [INFO] Codex: queueing continue for thread 01a1bb89...
2026-11-09 22:35:16 [INFO] Codex: successfully queued continue message
2026-11-09 22:35:16 [INFO] Codex: auto-continue completed for thread 'Fix tests'
```

## Limitations

- Requires Codex CLI and/or OpenCode installed
- Codex: only detects `usageLimitExceeded` errors
- OpenCode: only detects "usage limit exceeded" in logs
- Reset time parsing assumes 12-hour format with AM/PM
- OpenCode resume may start interactive session (daemon handles timeout gracefully)

## Troubleshooting

**Daemon not detecting errors:**
- Check that data sources exist and have recent entries
- Run with `--log-level DEBUG` to see database queries

**Codex queue command failing:**
- Verify `codex` is in PATH
- Check that the thread still exists and is not archived

**OpenCode resume failing:**
- Verify `opencode` is in PATH
- Check session exists in `~/.local/share/opencode/opencode.db`

**Infinite wait loop:**
- Check `~/.local/state/auto-continue-daemon.json` for continued sessions
- Manually delete entries to re-process a session

## Service Management

```bash
# View logs
journalctl --user -u auto-continue-daemon.service -f

# Restart
systemctl --user restart auto-continue-daemon.service

# Stop
systemctl --user stop auto-continue-daemon.service

# Disable autostart
systemctl --user disable auto-continue-daemon.service

# Check if enabled
systemctl --user is-enabled auto-continue-daemon.service
```
