import json
import threading
import time
from urllib import error

from helpers.app_logging import get_logger
from helpers.config_reader import ConfigError, load_yaml_config
from helpers.matfix import send_matfix_message
from helpers.timezone_utils import format_timestamp, get_server_timezone


ROUTE = "/callingbell"
logger = get_logger(__name__)
_seen_event_ids = {}
_seen_event_ids_lock = threading.Lock()


def _send_response(handler, status, message=None):
    handler.send_response(status)
    if status == 401:
        handler.send_header("WWW-Authenticate", 'Bearer realm="callingbell"')
    handler.end_headers()
    if message:
        handler.wfile.write(message.encode("utf-8"))


def _load_callingbell_config():
    config = load_yaml_config("callingbell.yaml")
    provisioned_tokens = config.get("provisioned_tokens")
    clear_seen_events_after_hours = config.get("clear_seen_events_after_hours")
    message_template = config.get("message_template")

    if not isinstance(provisioned_tokens, list) or not provisioned_tokens:
        raise ConfigError("callingbell.yaml: provisioned_tokens must be a non-empty list")

    token_location_pairs = {}
    seen_locations = set()
    for entry in provisioned_tokens:
        if not isinstance(entry, dict):
            raise ConfigError("callingbell.yaml: each provisioned token entry must be an object")

        token = entry.get("token")
        location = entry.get("location")
        if not isinstance(token, str) or not token.strip():
            raise ConfigError("callingbell.yaml: provisioned token 'token' is required")
        if not isinstance(location, str) or not location.strip():
            raise ConfigError("callingbell.yaml: provisioned token 'location' is required")

        normalized_token = token.strip()
        normalized_location = location.strip()
        if normalized_token in token_location_pairs:
            raise ConfigError("callingbell.yaml: duplicate token values are not allowed")
        if normalized_location in seen_locations:
            raise ConfigError("callingbell.yaml: duplicate location values are not allowed")

        token_location_pairs[normalized_token] = normalized_location
        seen_locations.add(normalized_location)

    if not isinstance(config.get("api_key"), str) or not config.get("api_key").strip():
        raise ConfigError("callingbell.yaml: api_key is required")

    if not isinstance(config.get("account_id"), str) or not config.get("account_id").strip():
        raise ConfigError("callingbell.yaml: account_id is required")

    room_ids = config.get("room_ids")
    if not isinstance(room_ids, list) or not room_ids or not all(isinstance(item, str) and item.strip() for item in room_ids):
        raise ConfigError("callingbell.yaml: room_ids must be a non-empty list of strings")

    if not isinstance(message_template, str) or not message_template.strip():
        raise ConfigError("callingbell.yaml: message_template is required")

    if isinstance(clear_seen_events_after_hours, bool):
        raise ConfigError("callingbell.yaml: clear_seen_events_after_hours must be a number")

    try:
        clear_seen_events_after_hours = float(clear_seen_events_after_hours)
    except (TypeError, ValueError):
        raise ConfigError("callingbell.yaml: clear_seen_events_after_hours is required")

    if clear_seen_events_after_hours <= 0:
        raise ConfigError("callingbell.yaml: clear_seen_events_after_hours must be greater than 0")

    return {
        "token_location_pairs": token_location_pairs,
        "api_key": config["api_key"],
        "account_id": config["account_id"],
        "room_ids": [item.strip() for item in room_ids],
        "message_template": message_template,
        "clear_seen_events_after_hours": clear_seen_events_after_hours,
    }


def _read_request_body(handler):
    content_length = handler.headers.get("Content-Length", "0")
    try:
        length = max(int(content_length), 0)
    except (TypeError, ValueError):
        raise ValueError("Invalid Content-Length")

    if length == 0:
        return ""

    return handler.rfile.read(length).decode("utf-8")


def _parse_event_payload(handler):
    body = _read_request_body(handler)
    if not body.strip():
        raise ValueError("Missing request body")

    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    event_id = payload.get("id")
    timestamp = payload.get("timestamp")
    if isinstance(event_id, bool):
        raise ValueError("Field 'id' must be a string or integer")
    if not isinstance(event_id, (str, int)):
        raise ValueError("Field 'id' must be a string or integer")
    if isinstance(event_id, str) and not event_id.strip():
        raise ValueError("Field 'id' cannot be empty")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise ValueError("Field 'timestamp' must be an integer")

    normalized_event_id = str(event_id).strip()
    return normalized_event_id, timestamp


def _clear_expired_events(max_age_seconds):
    cutoff = time.time() - max_age_seconds
    expired_event_ids = [event_id for event_id, seen_at in _seen_event_ids.items() if seen_at < cutoff]

    for event_id in expired_event_ids:
        del _seen_event_ids[event_id]


def _format_event_time(timestamp):
    try:
        return format_timestamp(timestamp, get_server_timezone())
    except (ValueError, OverflowError, OSError):
        return str(timestamp)


def _build_message(template, timestamp, place):
    return template.replace("{{time}}", _format_event_time(timestamp)).replace("{{place}}", place)


def _extract_bearer_token(handler):
    authorization = handler.headers.get("Authorization", "").strip()
    prefix = "Bearer "

    if not authorization.startswith(prefix):
        return None

    token = authorization[len(prefix):].strip()
    if not token:
        return None

    return token

def handle(handler):
    if handler.command != "POST":
        _send_response(handler, 405)
        return

    try:
        config = _load_callingbell_config()
    except (ConfigError, ValueError, TypeError) as exc:
        logger.error("Callingbell config error: %s", exc)
        _send_response(handler, 500)
        return

    request_token = _extract_bearer_token(handler)
    request_location = handler.headers.get("X-Bell-Location", "").strip()
    expected_location = config["token_location_pairs"].get(request_token) if request_token else None

    if expected_location is None or request_location != expected_location:
        logger.warning("Callingbell rejected unauthorized request due to token/location mismatch")
        _send_response(handler, 401)
        return

    try:
        event_id, timestamp = _parse_event_payload(handler)
    except json.JSONDecodeError:
        logger.warning("Callingbell received invalid JSON")
        _send_response(handler, 400)
        return
    except ValueError as exc:
        logger.warning("Callingbell rejected invalid payload: %s", exc)
        _send_response(handler, 400)
        return

    with _seen_event_ids_lock:
        _clear_expired_events(config["clear_seen_events_after_hours"] * 3600)

        if event_id in _seen_event_ids:
            logger.info("Callingbell ignored duplicate event id=%s", event_id)
            _send_response(handler, 409)
            return
        _seen_event_ids[event_id] = time.time()

    place = expected_location
    message = _build_message(config["message_template"], timestamp, place)
    failures = []

    for room_id in config["room_ids"]:
        try:
            status = send_matfix_message(
                config["api_key"],
                config["account_id"],
                room_id,
                {
                    "type": "text",
                    "body": message,
                },
            )
            if status != 202:
                failures.append(f"{room_id} (status={status})")
        except error.HTTPError as exc:
            failures.append(f"{room_id} (http={exc.code})")
            logger.warning("Callingbell send failed for %s: HTTP %s", room_id, exc.code)
        except error.URLError as exc:
            failures.append(f"{room_id} (network)")
            logger.warning("Callingbell send failed for %s: %s", room_id, exc.reason)
        except Exception:
            failures.append(f"{room_id} (unexpected)")
            logger.exception("Callingbell send failed for %s", room_id)

    if failures:
        logger.warning("Callingbell delivery failed for event id=%s: %s", event_id, ", ".join(failures))
        _send_response(handler, 502)
        return

    logger.info("Callingbell event id=%s delivered to %d rooms", event_id, len(config["room_ids"]))

    handler.send_response(200)
    handler.end_headers()
