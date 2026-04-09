# icloud-caldav

OpenClaw skill integrating Apple iCloud Calendar over CalDAV — secure, UID-based, zero plaintext secrets.

Full documentation lives in [`SKILL.md`](./SKILL.md). This README is a quickstart.

## Quickstart

```bash
# 1. Install dependencies
uv pip install --system caldav icalendar pytz tzlocal

# 2. 

Export credentials (app-specific password from https://appleid.apple.com)
export ICLOUD_APPLE_ID="you@icloud.com"
export ICLOUD_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"

or

cat > ~/.openclaw/.env <<'EOF'
ICLOUD_APPLE_ID=apple.id@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
EOF
chmod 600 ~/.openclaw/.env

# 3. Run
python icloud_caldav_cli.py list-calendars
```

## Commands

| Command | Purpose |
|---|---|
| `list-calendars` | List all iCloud calendars |
| `list-events --calendar NAME --start DATE --end DATE` | Range query |
| `get-event UID [--calendar NAME]` | Fetch one event |
| `create-event --calendar NAME --title ... --start ... --end ...` | Create event |
| `update-event UID --calendar NAME [flags]` | Update by UID |
| `delete-event UID --calendar NAME --force` | Delete by UID (requires `--force`) |
| `search-events QUERY --calendar NAME` | Text search |

## Security invariants

- Credentials only from env vars (`ICLOUD_APPLE_ID`, `ICLOUD_APP_PASSWORD`).
- Destructive ops are UID-only; `delete-event` also requires `--force`.
- No regex / string parsing on iCal data — everything flows through the `caldav` + `icalendar` object model.
- All output is JSON; errors carry structured `error`/`detail` fields and non-zero exit codes.

See `SKILL.md` for the full security model, exit-code table and agent usage guidance.
