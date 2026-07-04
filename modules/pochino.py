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

# Runtime State (RAM only)
_pending_state = None
_pending_timestamp = 0.0
_generation = 0
_first_off_timestamp = None
_lock = threading.Lock()


## Stable state helpers ##

def get_last_stable_state():
    config = load_yaml_config("pochino.yaml")
    state, ts = config["last_stable_state"].split(":")
    return state, int(ts)


def set_last_stable_state(state: str, timestamp: int):
    write_config_value("last_stable_state", f"{state}:{timestamp}", "pochino.yaml")


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


def _normalize_action_key(key):
    if key is True:
        return "on"
    if key is False:
        return "off"
    return str(key).strip().lower()


def send_alert(handler, action, config, timestamp, downtime=0, fluctuations=0) -> list[str]:
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
        tz = ZoneInfo(get_server_timezone())
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

    device_name = _resolve_device_name(handler)
    formatted_time = format_timestamp(int(timestamp), get_server_timezone())

    if action == "on":
        downtime_str = format_duration(downtime)
        body = template + f"\n\nDowntime: {downtime_str}"
        if fluctuations > 1:
            body += f"\nPower fluctuations: {fluctuations}"
        full_message = f"{body}\nDevice: {device_name}\nTime: {formatted_time}"
    else:
        full_message = f"{template}\nDevice: {device_name}\nTime: {formatted_time}"

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

    # Increment and persist daily messages counter
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


def _resolve_device_name(handler):
    device_name = handler.headers.get("X-Pochino-Device", "").strip()
    if device_name:
        return device_name
    return socket.gethostname()


## Threading & State Machine Workflows ##

def debounce_worker(gen, act, ts, handler, config):
    global _pending_state, _pending_timestamp, _generation, _first_off_timestamp

    with _lock:
        if _generation != gen:
            logger.info("Debounce worker (generation %d, action %s) discarded: newer webhook received", gen, act)
            return
        if _pending_state != act:
            logger.info("Debounce worker (generation %d, action %s) discarded: pending state changed to %s", gen, act, _pending_state)
            return

        try:
            stable_state, stable_timestamp = get_last_stable_state()
        except Exception as exc:
            logger.error("Failed to load last stable state: %s", exc)
            stable_state, stable_timestamp = "on", int(ts)

        if stable_state == act:
            logger.info("State is already stable '%s', no transition needed", act)
            return

        logger.info("Transition to '%s' is stable", act)

        try:
            current_config = _load_pochino_config()
        except Exception:
            current_config = config

        if act == "off":
            outage_ts = _first_off_timestamp if _first_off_timestamp is not None else ts
            try:
                set_last_stable_state("off", int(outage_ts))
            except Exception:
                logger.exception("Failed to write stable state 'off'")

            send_alert(handler, "off", current_config, timestamp=ts)

        elif act == "on":
            outage_start_timestamp = stable_timestamp if stable_state == "off" else ts
            downtime = int(ts - outage_start_timestamp)

            fluctuations = current_config.get("fluctuation_count", 0)

            send_alert(handler, "on", current_config, timestamp=ts, downtime=downtime, fluctuations=fluctuations)

            try:
                set_last_stable_state("on", int(ts))
                write_config_value("fluctuation_count", 0, "pochino.yaml")
            except Exception:
                logger.exception("Failed to write stable state 'on' or reset fluctuations")

            _first_off_timestamp = None


def handle_off(handler, config):
    global _pending_state, _pending_timestamp, _generation, _first_off_timestamp
    now = time.time()

    with _lock:
        if _pending_state == "on":
            try:
                config_data = load_yaml_config("pochino.yaml")
                fluctuation_count = config_data.get("fluctuation_count", 0)
                fluctuation_count += 1
                write_config_value("fluctuation_count", fluctuation_count, "pochino.yaml")
                logger.info("Outage fluctuation detected. Fluctuation count incremented to %d", fluctuation_count)
            except Exception:
                logger.exception("Failed to update fluctuation count")

        _pending_state = "off"
        _pending_timestamp = now
        _generation += 1
        current_generation = _generation

        try:
            stable_state, _ = get_last_stable_state()
            if stable_state == "on" and _first_off_timestamp is None:
                _first_off_timestamp = now
        except Exception:
            if _first_off_timestamp is None:
                _first_off_timestamp = now

    debounce_seconds = config.get("debounce_seconds", 30)

    def worker_target():
        time.sleep(debounce_seconds)
        debounce_worker(current_generation, "off", now, handler, config)

    t = threading.Thread(target=worker_target)
    t.daemon = True
    t.start()

    _send_response(handler, 200, "Pochino off state change pending")


def handle_on(handler, config):
    global _pending_state, _pending_timestamp, _generation
    now = time.time()

    with _lock:
        _pending_state = "on"
        _pending_timestamp = now
        _generation += 1
        current_generation = _generation

    debounce_seconds = config.get("debounce_seconds", 30)

    def worker_target():
        time.sleep(debounce_seconds)
        debounce_worker(current_generation, "on", now, handler, config)

    t = threading.Thread(target=worker_target)
    t.daemon = True
    t.start()

    _send_response(handler, 200, "Pochino on state change pending")


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

    if action == "on":
        handle_on(handler, config)
    else:
        handle_off(handler, config)
