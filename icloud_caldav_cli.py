#!/usr/bin/env python3
"""
icloud_caldav_cli.py — Secure CalDAV client for Apple iCloud Calendar.

Part of the OpenClaw `icloud-caldav` skill.

Security model
--------------
* Credentials are read only from environment variables
  (ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD). No plaintext config files,
  no CLI flags containing secrets.
* Destructive operations (delete, update) operate strictly by UID.
  There is no "best match by title" heuristic and `delete-event`
  additionally requires an explicit `--force` flag.
* All communication goes through the `caldav` library and the
  `icalendar` object model. There is no manual parsing of iCal text,
  no regex, no string splitting on `.data`.
* Every command emits structured JSON on stdout:
    {"success": true,  "data": ...}      # exit 0
    {"success": false, "error": "...", "detail": "..."}  # exit != 0
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

# ---------------------------------------------------------------------------
# Dependency imports (with friendly errors if missing)
# ---------------------------------------------------------------------------

try:
    import caldav
    from caldav.lib import error as caldav_error
except ImportError as exc:  # pragma: no cover
    print(
        json.dumps(
            {
                "success": False,
                "error": (
                    "Missing dependency 'caldav'. Install with: "
                    "uv pip install --system caldav icalendar pytz tzlocal"
                ),
                "detail": str(exc),
            },
            indent=2,
        )
    )
    sys.exit(2)

try:
    from icalendar import Calendar as ICalendar
    from icalendar import Event as IEvent
except ImportError as exc:  # pragma: no cover
    print(
        json.dumps(
            {
                "success": False,
                "error": (
                    "Missing dependency 'icalendar'. Install with: "
                    "uv pip install --system caldav icalendar pytz tzlocal"
                ),
                "detail": str(exc),
            },
            indent=2,
        )
    )
    sys.exit(2)

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - py<3.9 fallback
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore

try:
    from tzlocal import get_localzone
except ImportError:  # pragma: no cover
    get_localzone = None  # type: ignore

import requests  # Transitive dependency of caldav, but we handle it explicitly.


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"
DEFAULT_LIST_LIMIT = 50
LOG = logging.getLogger("icloud-caldav")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """JSON serialization fallback for dates, datetimes, and other odd types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def output_success(data: Any) -> None:
    """Emit a success payload and exit 0."""
    print(json.dumps({"success": True, "data": data}, indent=2, default=_serialize))
    sys.exit(0)


def output_error(message: str, *, code: int = 1, detail: Optional[str] = None) -> None:
    """Emit a failure payload and exit with the given non-zero code."""
    payload: dict[str, Any] = {"success": False, "error": message}
    if detail:
        payload["detail"] = detail
    print(json.dumps(payload, indent=2, default=_serialize))
    sys.exit(code)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """Immutable pair of Apple ID and app-specific password."""

    apple_id: str
    app_password: str

    @classmethod
    def from_env(cls) -> "Credentials":
        """Load credentials strictly from the environment.

        Raises EnvironmentError if either variable is missing or empty.
        """
        apple_id = os.environ.get("ICLOUD_APPLE_ID", "").strip()
        app_password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
        if not apple_id or not app_password:
            raise EnvironmentError(
                "Missing iCloud credentials. Set ICLOUD_APPLE_ID and "
                "ICLOUD_APP_PASSWORD environment variables. An app-specific "
                "password is required (generate one at https://appleid.apple.com)."
            )
        return cls(apple_id=apple_id, app_password=app_password)


# ---------------------------------------------------------------------------
# Timezone & datetime parsing
# ---------------------------------------------------------------------------


def resolve_timezone(tz_name: Optional[str]) -> ZoneInfo:
    """Return a ZoneInfo for `tz_name` or the local timezone as a fallback.

    Falls back to UTC if tzlocal is unavailable or returns something exotic.
    """
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {tz_name}") from exc

    if get_localzone is not None:
        try:
            local = get_localzone()
            if isinstance(local, ZoneInfo):
                return local
            # Older tzlocal returns a pytz tz; try to get its key.
            key = getattr(local, "key", None) or getattr(local, "zone", None)
            if key:
                try:
                    return ZoneInfo(key)
                except ZoneInfoNotFoundError:
                    pass
        except Exception:  # pragma: no cover
            pass
    return ZoneInfo("UTC")


def parse_iso_datetime(value: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO 8601 date or datetime string into an aware datetime.

    * Naive values are interpreted in `tz`.
    * Date-only values become midnight in `tz`.
    * Trailing 'Z' is normalised to '+00:00' for fromisoformat.
    """
    if not value:
        raise ValueError("Empty datetime value")

    normalised = value.strip()
    if normalised.endswith("Z"):
        normalised = normalised[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(normalised)
            parsed = datetime.combine(parsed_date, datetime.min.time())
        except ValueError as exc:
            raise ValueError(
                f"Invalid ISO 8601 datetime: {value!r}. "
                "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]."
            ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


# ---------------------------------------------------------------------------
# CalDAV client construction
# ---------------------------------------------------------------------------


def build_client_kwargs(creds: Credentials) -> dict[str, Any]:
    """Construct kwargs for `caldav.DAVClient`, enabling iCloud quirks if supported.

    Older caldav versions expose `features=["icloud"]`; newer versions
    auto-detect iCloud from the URL. We pass the kwarg only if the running
    version accepts it, so the skill remains compatible with both.
    """
    kwargs: dict[str, Any] = {
        "url": ICLOUD_CALDAV_URL,
        "username": creds.apple_id,
        "password": creds.app_password,
    }
    try:
        import inspect

        sig = inspect.signature(caldav.DAVClient.__init__)
        if "features" in sig.parameters:
            kwargs["features"] = ["icloud"]
    except (TypeError, ValueError):  # pragma: no cover
        pass
    return kwargs


def get_calendar_by_name(
    principal: "caldav.Principal", name: str
) -> "caldav.Calendar":
    """Resolve a calendar by its name or displayname (exact, case-sensitive)."""
    for cal in principal.calendars():
        if (cal.name or "") == name:
            return cal
        try:
            display = cal.get_display_name()
        except Exception:
            display = None
        if display and display == name:
            return cal
    raise LookupError(f"Calendar not found: {name!r}")


# ---------------------------------------------------------------------------
# Event serialisation
# ---------------------------------------------------------------------------


def _ical_value(component: Any, key: str) -> Optional[str]:
    val = component.get(key)
    return None if val is None else str(val)


def _ical_datetime(component: Any, key: str) -> Optional[str]:
    val = component.get(key)
    if val is None:
        return None
    dt = getattr(val, "dt", val)
    if isinstance(dt, (datetime, date)):
        return dt.isoformat()
    return str(dt)


def event_to_dict(event: "caldav.Event") -> dict[str, Any]:
    """Convert a caldav.Event into a plain JSON-ready dict."""
    try:
        component = event.icalendar_component
    except Exception as exc:
        return {
            "uid": None,
            "error": f"Unable to parse event: {exc}",
            "url": str(event.url) if getattr(event, "url", None) else None,
        }

    return {
        "uid": _ical_value(component, "uid"),
        "summary": _ical_value(component, "summary"),
        "description": _ical_value(component, "description"),
        "location": _ical_value(component, "location"),
        "start": _ical_datetime(component, "dtstart"),
        "end": _ical_datetime(component, "dtend"),
        "created": _ical_datetime(component, "created"),
        "last_modified": _ical_datetime(component, "last-modified"),
        "status": _ical_value(component, "status"),
        "url": str(event.url) if getattr(event, "url", None) else None,
    }


def calendar_to_dict(calendar: "caldav.Calendar") -> dict[str, Any]:
    try:
        display = calendar.get_display_name()
    except Exception:
        display = None
    return {
        "name": calendar.name,
        "displayname": display,
        "url": str(calendar.url) if calendar.url else None,
    }


def _find_event_in_calendar(
    calendar: "caldav.Calendar", uid: str
) -> Optional["caldav.Event"]:
    """Return the event with the given UID, or None if absent.

    Only NotFoundError / generic DAVError is swallowed; hard errors
    (network, auth) propagate to the top-level handler.
    """
    try:
        return calendar.event_by_uid(uid)
    except caldav_error.NotFoundError:
        return None
    except caldav_error.DAVError:
        return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_list_calendars(args: argparse.Namespace, creds: Credentials) -> None:
    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendars = principal.calendars()
        output_success([calendar_to_dict(c) for c in calendars])


def cmd_list_events(args: argparse.Namespace, creds: Credentials) -> None:
    tz = resolve_timezone(args.tz)
    start_dt = parse_iso_datetime(args.start, tz)
    end_dt = parse_iso_datetime(args.end, tz)
    if end_dt < start_dt:
        raise ValueError("--end must be >= --start")

    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendar = get_calendar_by_name(principal, args.calendar)

        # Prefer `search()` (caldav >= 1.0); fall back to the older
        # `date_search()` when running against an older library.
        try:
            results = calendar.search(
                start=start_dt,
                end=end_dt,
                event=True,
                expand=True,
            )
        except (TypeError, AttributeError):
            results = calendar.date_search(start=start_dt, end=end_dt)

        payload = [event_to_dict(ev) for ev in results]
        payload.sort(key=lambda d: d.get("start") or "")
        output_success(payload[: args.limit])


def cmd_get_event(args: argparse.Namespace, creds: Credentials) -> None:
    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()

        if args.calendar:
            calendar = get_calendar_by_name(principal, args.calendar)
            event = _find_event_in_calendar(calendar, args.uid)
            if event is None:
                raise LookupError(
                    f"Event with UID {args.uid!r} not found in calendar "
                    f"{args.calendar!r}"
                )
            data = event_to_dict(event)
            data["calendar"] = calendar.name
            output_success(data)

        # No calendar provided — scan all calendars.
        for cal in principal.calendars():
            event = _find_event_in_calendar(cal, args.uid)
            if event is not None:
                data = event_to_dict(event)
                data["calendar"] = cal.name
                output_success(data)

        raise LookupError(f"Event with UID {args.uid!r} not found in any calendar")


def cmd_create_event(args: argparse.Namespace, creds: Credentials) -> None:
    tz = resolve_timezone(args.tz)
    start_dt = parse_iso_datetime(args.start, tz)
    end_dt = parse_iso_datetime(args.end, tz)
    if end_dt <= start_dt:
        raise ValueError("--end must be greater than --start")
    if not args.title.strip():
        raise ValueError("--title must not be empty")

    uid = str(uuid4())

    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendar = get_calendar_by_name(principal, args.calendar)

        # Build the VCALENDAR with icalendar's object model — no raw iCal text.
        ical = ICalendar()
        ical.add("prodid", "-//OpenClaw//icloud-caldav//EN")
        ical.add("version", "2.0")

        vevent = IEvent()
        vevent.add("uid", uid)
        vevent.add("summary", args.title)
        vevent.add("dtstart", start_dt)
        vevent.add("dtend", end_dt)
        vevent.add("dtstamp", datetime.now(tz=timezone.utc))
        vevent.add("created", datetime.now(tz=timezone.utc))
        if args.description:
            vevent.add("description", args.description)
        if args.location:
            vevent.add("location", args.location)

        ical.add_component(vevent)

        new_event = calendar.save_event(ical=ical.to_ical().decode("utf-8"))
        data = event_to_dict(new_event)
        # save_event() may return an event whose icalendar_component does not
        # reparse cleanly on every caldav version; ensure UID is present.
        if not data.get("uid"):
            data["uid"] = uid
        data["calendar"] = calendar.name
        output_success(data)


def cmd_update_event(args: argparse.Namespace, creds: Credentials) -> None:
    tz = resolve_timezone(args.tz)

    if not any(
        [
            args.title is not None,
            args.start is not None,
            args.end is not None,
            args.description is not None,
            args.location is not None,
        ]
    ):
        raise ValueError(
            "update-event requires at least one of "
            "--title/--start/--end/--description/--location"
        )

    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendar = get_calendar_by_name(principal, args.calendar)

        event = _find_event_in_calendar(calendar, args.uid)
        if event is None:
            raise LookupError(
                f"Event with UID {args.uid!r} not found in {args.calendar!r}"
            )

        ical_instance = event.icalendar_instance
        vevents = list(ical_instance.walk("VEVENT"))
        if not vevents:
            raise ValueError("Event has no VEVENT component")
        vevent = vevents[0]

        def _replace(key: str, value: Any) -> None:
            """Replace an icalendar property by delete-then-add."""
            if key in vevent:
                del vevent[key]
            vevent.add(key, value)

        if args.title is not None:
            if not args.title.strip():
                raise ValueError("--title must not be empty")
            _replace("summary", args.title)

        if args.description is not None:
            _replace("description", args.description)

        if args.location is not None:
            _replace("location", args.location)

        if args.start is not None:
            _replace("dtstart", parse_iso_datetime(args.start, tz))

        if args.end is not None:
            _replace("dtend", parse_iso_datetime(args.end, tz))

        # Validate start/end consistency when both are datetimes.
        start_prop = vevent.get("dtstart")
        end_prop = vevent.get("dtend")
        if start_prop is not None and end_prop is not None:
            start_v = getattr(start_prop, "dt", None)
            end_v = getattr(end_prop, "dt", None)
            if isinstance(start_v, datetime) and isinstance(end_v, datetime):
                if end_v <= start_v:
                    raise ValueError("Updated dtend must be greater than dtstart")

        _replace("last-modified", datetime.now(tz=timezone.utc))

        # Crucial: reassign .data so caldav.Event.save() ships the new bytes.
        event.data = ical_instance.to_ical().decode("utf-8")
        event.save()

        data = event_to_dict(event)
        data["calendar"] = calendar.name
        output_success(data)


def cmd_delete_event(args: argparse.Namespace, creds: Credentials) -> None:
    if not args.force:
        output_error(
            "Refusing to delete without --force. Re-run with --force to confirm.",
            code=3,
        )

    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendar = get_calendar_by_name(principal, args.calendar)

        event = _find_event_in_calendar(calendar, args.uid)
        if event is None:
            raise LookupError(
                f"Event with UID {args.uid!r} not found in {args.calendar!r}"
            )

        snapshot = event_to_dict(event)
        event.delete()
        output_success({"deleted": True, "calendar": calendar.name, "event": snapshot})


def cmd_search_events(args: argparse.Namespace, creds: Credentials) -> None:
    query = args.query.strip().lower()
    if not query:
        raise ValueError("Empty search query")

    with caldav.DAVClient(**build_client_kwargs(creds)) as client:
        principal = client.principal()
        calendar = get_calendar_by_name(principal, args.calendar)

        # Client-side filter is portable across CalDAV servers and avoids
        # relying on text-match XML extensions that iCloud does not fully
        # expose. We iterate the calendar's events and match on the
        # already-structured dict — zero regex, zero string parsing.
        events: Iterable = calendar.events()
        matches: list[dict[str, Any]] = []
        for ev in events:
            data = event_to_dict(ev)
            haystack = " ".join(
                str(data.get(k) or "")
                for k in ("summary", "description", "location")
            ).lower()
            if query in haystack:
                matches.append(data)
                if len(matches) >= args.limit:
                    break

        matches.sort(key=lambda d: d.get("start") or "")
        output_success(matches)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icloud_caldav_cli.py",
        description=(
            "Secure CalDAV CLI for Apple iCloud Calendar. All output is JSON."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging to stderr (never logs credentials).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit JSON (default; retained for forward compatibility).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list-calendars
    subparsers.add_parser("list-calendars", help="List all iCloud calendars.")

    # list-events
    p = subparsers.add_parser("list-events", help="List events in a calendar.")
    p.add_argument("--calendar", required=True)
    p.add_argument("--start", required=True, help="ISO 8601 date or datetime")
    p.add_argument("--end", required=True, help="ISO 8601 date or datetime")
    p.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT)
    p.add_argument("--timezone", dest="tz", default=None)

    # get-event
    p = subparsers.add_parser("get-event", help="Fetch a single event by UID.")
    p.add_argument("uid")
    p.add_argument(
        "--calendar",
        default=None,
        help="Optional: restrict lookup to one calendar.",
    )

    # create-event
    p = subparsers.add_parser("create-event", help="Create a new event.")
    p.add_argument("--calendar", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--description", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--timezone", dest="tz", default=None)

    # update-event
    p = subparsers.add_parser(
        "update-event", help="Update an event (UID required)."
    )
    p.add_argument("uid")
    p.add_argument("--calendar", required=True)
    p.add_argument("--title", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--timezone", dest="tz", default=None)

    # delete-event
    p = subparsers.add_parser(
        "delete-event", help="Delete an event (UID required; needs --force)."
    )
    p.add_argument("uid")
    p.add_argument("--calendar", required=True)
    p.add_argument(
        "--force",
        action="store_true",
        help="Confirm the deletion. Required — no deletion without it.",
    )

    # search-events
    p = subparsers.add_parser(
        "search-events",
        help="Case-insensitive search over summary/description/location.",
    )
    p.add_argument("query")
    p.add_argument("--calendar", required=True)
    p.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT)

    return parser


COMMAND_HANDLERS = {
    "list-calendars": cmd_list_calendars,
    "list-events": cmd_list_events,
    "get-event": cmd_get_event,
    "create-event": cmd_create_event,
    "update-event": cmd_update_event,
    "delete-event": cmd_delete_event,
    "search-events": cmd_search_events,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )
        logging.getLogger("caldav").setLevel(logging.DEBUG)

    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        output_error(f"Unknown command: {args.command}", code=2)

    try:
        creds = Credentials.from_env()
    except EnvironmentError as exc:
        output_error(str(exc), code=4)
        return  # unreachable; satisfies type checkers

    try:
        handler(args, creds)
    except SystemExit:
        raise
    except caldav_error.AuthorizationError as exc:
        output_error(
            "Authorization failed. Check ICLOUD_APPLE_ID and "
            "ICLOUD_APP_PASSWORD (must be an app-specific password).",
            code=5,
            detail=str(exc),
        )
    except caldav_error.NotFoundError as exc:
        output_error(f"Resource not found: {exc}", code=6)
    except caldav_error.DAVError as exc:
        output_error(
            f"CalDAV error: {exc}", code=7, detail=type(exc).__name__
        )
    except requests.exceptions.ConnectionError as exc:
        output_error(f"Cannot reach iCloud CalDAV server: {exc}", code=8)
    except requests.exceptions.Timeout as exc:
        output_error(f"Timeout contacting iCloud: {exc}", code=9)
    except requests.exceptions.HTTPError as exc:
        output_error(f"HTTP error: {exc}", code=10)
    except requests.exceptions.RequestException as exc:
        output_error(f"Network error: {exc}", code=11)
    except LookupError as exc:
        output_error(str(exc), code=12)
    except ValueError as exc:
        output_error(str(exc), code=13)
    except EnvironmentError as exc:
        output_error(str(exc), code=14)
    except Exception as exc:  # pragma: no cover - last-resort catch
        if args.verbose:
            import traceback

            traceback.print_exc(file=sys.stderr)
        output_error(
            f"Unexpected error: {exc}", code=99, detail=type(exc).__name__
        )


if __name__ == "__main__":
    main()
