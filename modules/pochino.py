import socket
import threading
import time
from datetime import datetime
from urllib import error
from zoneinfo import ZoneInfo

from helpers.app_logging import get_logger
from helpers.config_reader import ConfigError, load_yaml_config, write_config_value
from helpers.matfix import send_matfix_message
from helpers.timezone_utils import format_timestamp, get_server_timezone


ROUTE = "/pochino"
logger = get_logger(__name__)

# Runtime State Machine (in-memory)
_lock = threading.Lock()
_pending_action: str | None = None
_pending_since: float = 0.0
_pending_device: str = ""
_timer: threading.Timer | None = None


## Helpers: Stable State & Fluctuation Persistence ##

def _normalize_action_key(key):
    if key is True:
        return "on"
    if key is False:
        return "off"
    return str(key).strip().lower()


def get_last_stable_state() -> tuple[str, int]:
    try:
        config = load_yaml_config("pochino.yaml")
        raw = str(config.get("last_stable_state", "")).strip()
        if ":" in raw:
            state, ts = raw.split(":", 1)
            state = _normalize_action_key(state)
            if state in ("on", "off"):
                return state, int(float(ts))
    except Exception as exc:
        logger.warning("Failed to load last_stable_state from pochino.yaml: %s", exc)
    return "on", int(time.time())


def set_last_stable_state(state: str, timestamp: int) -> None:
    try:
        write_config_value("last_stable_state", f"{state}:{int(timestamp)}", "pochino.yaml")
    except Exception:
        logger.exception("Failed to write last_stable_state to pochino.yaml")


def get_fluctuation_count() -> int:
    try:
        config = load_yaml_config("pochino.yaml")
        return int(config.get("fluctuation_count", 0))
    except Exception:
        return 0


def set_fluctuation_count(count: int) -> None:
    try:
        write_config_value("fluctuation_count", int(count), "pochino.yaml")
    except Exception:
        logger.exception("Failed to write fluctuation_count to pochino.yaml")


## Formatting Helpers ##

def format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


## API Interactions ##

def _send_response(handler, status, message):
    handler.send_response(status)
    if status == 405:
        handler.send_header("Allow", "POST")
    handler.end_headers()
    handler.wfile.write(message.encode("utf-8"))


def _resolve_device_name(handler=None) -> str:
    if handler and hasattr(handler, "headers") and handler.headers:
        device_name = handler.headers.get("X-Pochino-Device", "").strip()
        if device_name:
            return device_name
    return socket.gethostname()


def send_alert(action: str, config: dict, timestamp: float, downtime: int = 0, fluctuations: int = 0, device_name: str | None = None) -> list[str]:
    # Reload config to get the latest daily rate limit counters
    try:
        config_data = load_yaml_config("pochino.yaml")
    except Exception:
        config_data = config

    alerts = config_data.get("alerts", {})
    if not isinstance(alerts, dict):
        alerts = {}

    messages_today = alerts.get("messages_today", 0)
    last_reset_day = alerts.get("last_reset_day", "")
    max_messages_per_day = alerts.get("max_messages_per_day", 50)

    try:
        tz_name = get_server_timezone()
    except Exception:
        tz_name = "UTC"

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).strftime("%Y-%m-%d")

    if str(last_reset_day) != today:
        messages_today = 0
        last_reset_day = today
        try:
            write_config_value("alerts.messages_today", 0, "pochino.yaml")
            write_config_value("alerts.last_reset_day", today, "pochino.yaml")
        except Exception:
            logger.exception("Failed to write reset alert counters to pochino.yaml")

    if messages_today >= max_messages_per_day:
        logger.warning("Daily notification limit reached (%d/%d). Skipping alert '%s'.", messages_today, max_messages_per_day, action)
        return []

    template = config["messages"].get(action)
    if not template:
        logger.error("No message template found for action '%s'", action)
        return []

    resolved_device = device_name or socket.gethostname()
    try:
        formatted_time = format_timestamp(int(timestamp), tz_name)
    except Exception:
        formatted_time = str(int(timestamp))


    if action == "on":
        downtime_str = format_duration(downtime)
        body = template + f"\n\nDowntime: {downtime_str}"
        if fluctuations > 0:
            body += f"\nPower fluctuations: {fluctuations}"
        full_message = f"{body}\nDevice: {resolved_device}\nTime: {formatted_time}"
    else:
        full_message = f"{template}\nDevice: {resolved_device}\nTime: {formatted_time}"

    failures = []
    for room_id in config["room_ids"]:
        try:
            status = send_matfix_message(
                config["api_key"],
                config["account_id"],
                room_id,
                {
                    "type": "text",
                    "body": full_message,
                },
            )
            if status != 202:
                failures.append(f"{room_id} (status={status})")
        except error.HTTPError as exc:
            failures.append(f"{room_id} (http={exc.code})")
            logger.warning("Pochino send failed for %s: HTTP %s", room_id, exc.code)
        except error.URLError as exc:
            failures.append(f"{room_id} (network)")
            logger.warning("Pochino send failed for %s: %s", room_id, exc.reason)
        except Exception:
            failures.append(f"{room_id} (unexpected)")
            logger.exception("Pochino send failed for %s", room_id)

    messages_today += 1
    try:
        write_config_value("alerts.messages_today", messages_today, "pochino.yaml")
    except Exception:
        logger.exception("Failed to write incremented alerts.messages_today to pochino.yaml")

    return failures


## Config Handling ##

def _load_pochino_config():
    config = load_yaml_config("pochino.yaml")

    if not isinstance(config.get("api_key"), str) or not config.get("api_key").strip():
        raise ConfigError("pochino.yaml: api_key is required")

    if not isinstance(config.get("account_id"), str) or not config.get("account_id").strip():
        raise ConfigError("pochino.yaml: account_id is required")

    room_ids = config.get("room_ids")
    if not isinstance(room_ids, list) or not room_ids or not all(isinstance(item, str) and item for item in room_ids):
        raise ConfigError("pochino.yaml: room_ids must be a non-empty list of strings")

    messages = config.get("messages")
    if not isinstance(messages, dict):
        raise ConfigError("pochino.yaml: messages must be an object")

    normalized_messages = {}
    for action_key, message_value in messages.items():
        normalized_key = _normalize_action_key(action_key)
        if isinstance(message_value, str) and message_value:
            normalized_messages[normalized_key] = message_value

    debounce_seconds = config.get("debounce_seconds", 30)
    try:
        debounce_seconds = int(debounce_seconds)
    except (TypeError, ValueError):
        debounce_seconds = 30

    alerts = config.get("alerts", {})
    if not isinstance(alerts, dict):
        alerts = {}

    return {
        "api_key": config["api_key"],
        "account_id": config["account_id"],
        "room_ids": room_ids,
        "messages": normalized_messages,
        "debounce_seconds": debounce_seconds,
        "alerts": alerts,
        "fluctuation_count": config.get("fluctuation_count", 0),
    }


## State Machine Workflows ##

def _on_debounce_complete(action: str, trigger_timestamp: float, device_name: str, config: dict):
    global _pending_action, _pending_since, _pending_device, _timer

    with _lock:
        if _pending_action != action:
            logger.info("Debounce completed for '%s' but current pending action is '%s'; discarded", action, _pending_action)
            return

        _pending_action = None
        _timer = None

        stable_state, stable_ts = get_last_stable_state()
        if stable_state == action:
            logger.info("State is already stable '%s', no transition needed", action)
            return

        try:
            current_config = _load_pochino_config()
        except Exception:
            current_config = config

        fluctuations = get_fluctuation_count()

    # Outside lock: perform transitions and send alerts
    if action == "off":
        outage_ts = int(trigger_timestamp)
        set_last_stable_state("off", outage_ts)
        set_fluctuation_count(0)
        logger.info("Transition to stable 'off' confirmed at timestamp %d", outage_ts)
        send_alert("off", current_config, timestamp=outage_ts, device_name=device_name)

    elif action == "on":
        restore_ts = int(trigger_timestamp)
        downtime = max(0, restore_ts - stable_ts) if stable_state == "off" else 0
        logger.info(
            "Transition to stable 'on' confirmed at %d. Downtime: %d seconds, fluctuations: %d",
            restore_ts, downtime, fluctuations,
        )
        send_alert(
            "on",
            current_config,
            timestamp=restore_ts,
            downtime=downtime,
            fluctuations=fluctuations,
            device_name=device_name,
        )
        set_last_stable_state("on", restore_ts)
        set_fluctuation_count(0)


def process_signal(action: str, device_name: str, config: dict, now: float | None = None) -> tuple[int, str]:
    """
    Core state machine logic.
    Returns (http_status_code, response_message).
    """
    global _pending_action, _pending_since, _pending_device, _timer

    if now is None:
        now = time.time()

    with _lock:
        stable_state, stable_ts = get_last_stable_state()

        # 1. Duplicate check: If signal is already pending debounce, keep existing timer running!
        if _pending_action == action:
            logger.info("Pochino action '%s' is already pending debounce (since %s); ignoring duplicate", action, _pending_since)
            return 200, f"Pochino {action} state change already pending"

        # 2. Already stable check: If signal matches stable state and nothing is pending
        if _pending_action is None and stable_state == action:
            logger.info("State is already stable '%s', no transition needed", action)
            return 200, f"Pochino state is already stable {action}"

        # 3. Handling opposing signals during debounce:
        if _pending_action is not None:
            if _timer is not None:
                _timer.cancel()
                _timer = None

            cancelled_action = _pending_action
            _pending_action = None

            if stable_state == "off" and cancelled_action == "on" and action == "off":
                # Power flickered ON briefly (< debounce) and went back OFF during an outage!
                fluctuations = get_fluctuation_count() + 1
                set_fluctuation_count(fluctuations)
                logger.info("Outage fluctuation detected. Fluctuation count incremented to %d", fluctuations)
                return 200, f"Outage fluctuation detected (count={fluctuations})"

            if stable_state == "on" and cancelled_action == "off" and action == "on":
                # Power dipped briefly (< debounce) but came back ON before stable OFF!
                logger.info("Power flickered off then recovered before debounce; cancelled pending off")
                return 200, "Pending powercut cancelled by recovery"

        # 4. Starting a new pending transition
        _pending_action = action
        _pending_since = now
        _pending_device = device_name
        debounce_seconds = config.get("debounce_seconds", 30)

        _timer = threading.Timer(
            debounce_seconds,
            _on_debounce_complete,
            args=[action, now, device_name, config],
        )
        _timer.daemon = True
        _timer.start()

        logger.info("Pochino %s state change pending (debounce %ds)", action, debounce_seconds)
        return 200, f"Pochino {action} state change pending"


## Module Entrypoint ##

def handle(handler):
    if handler.command != "POST":
        _send_response(handler, 405, "Method not allowed. Use POST /pochino/on or POST /pochino/off")
        return

    action = handler.path.split("?", 1)[0].removeprefix(ROUTE).strip("/").lower()
    if action not in ("on", "off"):
        _send_response(handler, 404, f"Unknown pochino action: {action}")
        return

    try:
        config = _load_pochino_config()
    except (ConfigError, ValueError, TypeError) as exc:
        logger.error("Pochino config error: %s", exc)
        _send_response(handler, 500, "Pochino config error")
        return

    device_name = _resolve_device_name(handler)
    status_code, message = process_signal(action, device_name, config)
    _send_response(handler, status_code, message)
