# IoT Module Development Guide

This guide defines the required contract and conventions for all files in `modules/`.

## 1) Module Contract (Required)

Every module file must expose:

- `ROUTE`: string path, e.g. `"/status"`
- `handle(handler)`: callable function that receives `BaseHTTPRequestHandler`

The server loads all `.py` files from `modules/` and registers modules only when both values exist.

## 2) Route Rules

- Route must start with `/`.
- Use lowercase, hyphen-safe paths (example: `/device-status`).
- Keep routes unique across all modules.
- Current router does exact path matching only:
  - `/status` matches `/status`
  - `/status/` does not match `/status`
  - Query string handling is not normalized by the router

## 3) Logging Rules (Required)

Use the shared logging system from `helpers/app_logging.py`.

- Import logger helper:

```python
from helpers.app_logging import get_logger
```

- Create a module logger:

```python
logger = get_logger(__name__)
```

- Log important events in `handle(handler)`:
  - Request accepted
  - Validation failures
  - Internal exceptions

Do not use `print()` in modules.

## 4) Timezone & Time Handling

All timestamps should be handled using the centralized timezone utilities from `helpers/timezone_utils.py`.

**Important Assumption**: All incoming request timestamps are **UTC Unix timestamps** (seconds since epoch).

### Available Functions

```python
from helpers.timezone_utils import (
    format_timestamp,           # Convert UTC timestamp to timezone and format as string
    from_utc,                  # Convert UTC timestamp to timezone-aware datetime
    get_server_timezone,       # Get configured server timezone from app.yaml
    convert_between_timezones, # Alias for format_timestamp
)
```

### Usage Examples

**Display a timestamp in the configured server timezone:**
```python
import time
from helpers.timezone_utils import format_timestamp, get_server_timezone

# Display current time in server timezone
formatted = format_timestamp(int(time.time()), get_server_timezone())
# Output: "2026-06-01 18:30:45" (in configured timezone)
```

**Convert UTC timestamp to a different timezone:**
```python
from helpers.timezone_utils import format_timestamp

utc_timestamp = 1654091445
formatted_ist = format_timestamp(utc_timestamp, "Asia/Kolkata")
formatted_et = format_timestamp(utc_timestamp, "US/Eastern")
```

**Working with timezone-aware datetime objects:**
```python
from helpers.timezone_utils import from_utc

utc_timestamp = int(time.time())
dt_kolkata = from_utc(utc_timestamp, "Asia/Kolkata")  # Returns timezone-aware datetime
dt_tokyo = from_utc(utc_timestamp, "Asia/Tokyo")
```

### Server Configuration

Set the server timezone in `.config/app.yaml` under the `server` section:

```yaml
server:
  host: 0.0.0.0
  port: 8000
  timezone: Asia/Kolkata  # IANA timezone format (e.g., UTC, US/Eastern, Asia/Kolkata)
logging:
  level: INFO
  file: logs/iot.log
```

If not configured, defaults to `UTC`. The configured timezone is used for:
- Displaying timestamps in messages sent to external services (matfix, etc.)
- Formatting log timestamps in `logs/iot.log`

### Valid Timezone Formats

Use IANA timezone names that handle daylight saving time correctly:
- `UTC` — Coordinated Universal Time
- `Asia/Kolkata` — Indian Standard Time (IST)
- `US/Eastern` — Eastern Time (handles DST)
- `Europe/London` — Greenwich Mean Time (handles DST)
- `Asia/Tokyo` — Japan Standard Time
- Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

Do **not** use UTC offsets like `+05:30` — these do not handle daylight saving time.

## 5) Response Rules (Required)

In `handle(handler)` always:

1. Send status code (`handler.send_response(...)`)
2. End headers (`handler.end_headers()`)
3. Write bytes (`handler.wfile.write(b"...")`)

Notes:

- `wfile.write` requires bytes, not plain string.
- Keep response payloads short unless there is a clear requirement.

## 5) Error Handling

Use try/except in modules that do non-trivial work.

- Log exceptions with `logger.exception(...)`.
- Return a safe error response (typically 500) on unexpected failures.

Example pattern:

```python
try:
    # module logic
    ...
except Exception:
    logger.exception("Unhandled error in /your-route")
    handler.send_response(500)
    handler.end_headers()
    handler.wfile.write(b"internal error")
```

## 6) Module Template

Copy this when creating a new module in `modules/`:

```python
from helpers.app_logging import get_logger


ROUTE = "/example"
logger = get_logger(__name__)


def handle(handler):
    logger.info("Request received for %s", ROUTE)

    try:
        # Implement route behavior here
        payload = b"ok"

        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(payload)
    except Exception:
        logger.exception("Unhandled error in %s", ROUTE)
        handler.send_response(500)
        handler.end_headers()
        handler.wfile.write(b"internal error")
```

## 7) Checklist Before Adding a Module

- File is under `modules/` and ends with `.py`
- Exposes `ROUTE` and `handle(handler)`
- Route is unique and starts with `/`
- Uses `get_logger(__name__)`
- No `print()` calls
- Sends valid HTTP response (`send_response`, `end_headers`, `wfile.write`)
- Handles failure paths with logging

## 8) Current System Constraints

- Server currently handles GET requests only.
- Dynamic module loading happens at startup.
- New modules require server restart to be loaded.
