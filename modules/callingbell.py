import base64
import json
import threading
import time
from collections import deque
from urllib import error

import cv2

from helpers.app_logging import get_logger
from helpers.config_reader import ConfigError, load_yaml_config
from helpers.matfix import send_matfix_message
from helpers.timezone_utils import format_timestamp, get_server_timezone

ROUTE = "/callingbell"
logger = get_logger(__name__)
_seen_event_ids = {}
_seen_event_ids_lock = threading.Lock()


class RTSPMonitor(threading.Thread):
    def __init__(self, uri, frame_buffer_seconds, target_fps):
        super().__init__(daemon=True)
        self.uri = uri
        self.frame_buffer_seconds = frame_buffer_seconds
        self.target_fps = target_fps
        self.frame_interval = 1.0 / target_fps if target_fps > 0 else 0.5

        maxlen = int(frame_buffer_seconds * target_fps)
        if maxlen < 1:
            maxlen = 1
        self.buffer = deque(maxlen=maxlen)
        self.buffer_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.cap = None

    def stop(self):
        self.stop_event.set()
        with self.buffer_lock:
            if self.cap is not None:
                self.cap.release()
        self.join(timeout=2.0)

    def run(self):
        logger.info("RTSP Monitor thread started")
        while not self.stop_event.is_set():
            logger.info("RTSP reconnect attempt")
            cap = cv2.VideoCapture(self.uri)
            with self.buffer_lock:
                if self.stop_event.is_set():
                    cap.release()
                    break
                self.cap = cap

            if not cap.isOpened():
                logger.warning("RTSP camera unavailable")
                cap.release()
                # Wait 5 seconds before retrying, checking stop_event frequently
                for _ in range(50):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
                continue

            logger.info("RTSP connected")
            last_stored_time = 0.0

            while not self.stop_event.is_set():
                grabbed = cap.grab()
                if not grabbed:
                    logger.warning("RTSP disconnected")
                    break

                now = time.time()
                if now - last_stored_time >= self.frame_interval:
                    ret, frame = cap.retrieve()
                    if ret and frame is not None:
                        try:
                            # Encode as JPEG in memory
                            ret_enc, encoded_img = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                            if not ret_enc:
                                logger.error("JPEG encoding failure")
                            else:
                                jpeg_bytes = encoded_img.tobytes()
                                base64_jpeg = base64.b64encode(jpeg_bytes).decode("utf-8")
                                with self.buffer_lock:
                                    self.buffer.append((now, base64_jpeg))
                                last_stored_time = now
                        except Exception as exc:
                            logger.error("JPEG encoding failure: %s", exc)

            cap.release()
            with self.buffer_lock:
                self.cap = None
            if not self.stop_event.is_set():
                for _ in range(50):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)

    def get_closest_snapshot(self, event_timestamp, max_age_seconds):
        with self.buffer_lock:
            # Prune snapshots older than now - max_age_seconds
            cutoff = time.time() - max_age_seconds
            while self.buffer and self.buffer[0][0] < cutoff:
                self.buffer.popleft()

            if not self.buffer:
                return None

            closest_snapshot = None
            min_diff = None
            for frame_ts, base64_jpeg in self.buffer:
                diff = abs(frame_ts - event_timestamp)
                if min_diff is None or diff < min_diff:
                    min_diff = diff
                    closest_snapshot = base64_jpeg
            return closest_snapshot


def _sanitize_rtsp_uri(uri: str) -> str:
    import urllib.parse
    if not uri:
        return ""
    try:
        parsed = urllib.parse.urlparse(uri)
        if parsed.username or parsed.password:
            if "@" in parsed.netloc:
                creds, host = parsed.netloc.rsplit("@", 1)
                return parsed._replace(netloc=f"***:***@{host}").geturl()
    except Exception:
        pass
    import re
    return re.sub(r'^(rtsp[s]?://)[^@]+@', r'\1***:***@', uri, flags=re.IGNORECASE)


_rtsp_monitor = None


def _initialize_rtsp_at_startup():
    global _rtsp_monitor
    try:
        config = _load_callingbell_config()
        rtsp_config = config.get("rtsp")
        if rtsp_config and rtsp_config.get("enabled"):
            logger.info("Initializing RTSP Monitor with URI: %s", _sanitize_rtsp_uri(rtsp_config["uri"]))
            _rtsp_monitor = RTSPMonitor(
                uri=rtsp_config["uri"],
                frame_buffer_seconds=rtsp_config["frame_buffer_seconds"],
                target_fps=rtsp_config["target_fps"]
            )
            _rtsp_monitor.start()
    except Exception as exc:
        logger.debug("RTSP startup initialization skipped: %s", exc)


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

    rtsp_config = config.get("rtsp")
    rtsp_parsed = None
    if rtsp_config is not None:
        if not isinstance(rtsp_config, dict):
            raise ConfigError("callingbell.yaml: rtsp must be an object")

        enabled = rtsp_config.get("enabled")
        if not isinstance(enabled, bool):
            raise ConfigError("callingbell.yaml: rtsp.enabled must be a boolean")

        if enabled:
            uri = rtsp_config.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                raise ConfigError("callingbell.yaml: rtsp.uri is required when enabled is true")

            capture_threshold = rtsp_config.get("capture_threshold_seconds")
            if isinstance(capture_threshold, bool):
                raise ConfigError("callingbell.yaml: rtsp.capture_threshold_seconds must be a number")
            try:
                capture_threshold = float(capture_threshold)
            except (TypeError, ValueError):
                raise ConfigError("callingbell.yaml: rtsp.capture_threshold_seconds must be a number")
            if capture_threshold <= 0:
                raise ConfigError("callingbell.yaml: rtsp.capture_threshold_seconds must be greater than 0")

            frame_buffer = rtsp_config.get("frame_buffer_seconds")
            if isinstance(frame_buffer, bool):
                raise ConfigError("callingbell.yaml: rtsp.frame_buffer_seconds must be a number")
            try:
                frame_buffer = float(frame_buffer)
            except (TypeError, ValueError):
                raise ConfigError("callingbell.yaml: rtsp.frame_buffer_seconds must be a number")
            if frame_buffer <= 0:
                raise ConfigError("callingbell.yaml: rtsp.frame_buffer_seconds must be greater than 0")

            target_fps = rtsp_config.get("target_fps")
            if isinstance(target_fps, bool):
                raise ConfigError("callingbell.yaml: rtsp.target_fps must be a number")
            try:
                target_fps = float(target_fps)
            except (TypeError, ValueError):
                raise ConfigError("callingbell.yaml: rtsp.target_fps must be a number")
            if target_fps <= 0:
                raise ConfigError("callingbell.yaml: rtsp.target_fps must be greater than 0")

            rtsp_parsed = {
                "enabled": True,
                "uri": uri.strip(),
                "capture_threshold_seconds": capture_threshold,
                "frame_buffer_seconds": frame_buffer,
                "target_fps": target_fps,
            }
        else:
            rtsp_parsed = {
                "enabled": False,
            }
    else:
        rtsp_parsed = {
            "enabled": False,
        }

    return {
        "token_location_pairs": token_location_pairs,
        "api_key": config["api_key"],
        "account_id": config["account_id"],
        "room_ids": [item.strip() for item in room_ids],
        "message_template": message_template,
        "clear_seen_events_after_hours": clear_seen_events_after_hours,
        "rtsp": rtsp_parsed,
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
    expired_event_ids = [event_id for event_id, info in _seen_event_ids.items() if info[0] < cutoff]

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
            _, prev_status = _seen_event_ids[event_id]
            _send_response(handler, prev_status)
            return
        _seen_event_ids[event_id] = (time.time(), 200)

    place = expected_location
    message = _build_message(config["message_template"], timestamp, place)

    rtsp_config = config.get("rtsp", {})
    rtsp_enabled = rtsp_config.get("enabled", False)

    snapshot_available = False
    base64_jpeg = None
    filename = None

    if rtsp_enabled:
        delta = abs(time.time() - timestamp)
        if delta > rtsp_config["capture_threshold_seconds"]:
            logger.info("Snapshot skipped due to age (delta: %.2fs > threshold: %.2fs)", delta, rtsp_config["capture_threshold_seconds"])
        else:
            if _rtsp_monitor is None:
                logger.warning("RTSP monitor is not running")
                logger.info("Snapshot skipped due to missing frame")
            else:
                base64_jpeg = _rtsp_monitor.get_closest_snapshot(timestamp, rtsp_config["frame_buffer_seconds"])
                if base64_jpeg is None:
                    logger.info("Snapshot skipped due to missing frame")
                else:
                    snapshot_available = True
                    try:
                        dt_str = format_timestamp(timestamp, get_server_timezone(), "%Y-%m-%dT%H-%M-%S")
                    except Exception:
                        try:
                            from datetime import datetime, timezone
                            dt_str = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
                        except Exception:
                            dt_str = str(timestamp)
                    filename = f"{event_id}-{dt_str}.jpg"
                    logger.info("Snapshot selected (filename: %s)", filename)

    failures = []
    for room_id in config["room_ids"]:
        sent_file = False
        if snapshot_available and base64_jpeg and filename:
            try:
                file_payload = {
                    "type": "file",
                    "body": message,
                    "file": {
                        "data": base64_jpeg,
                        "mime_type": "image/jpeg",
                        "filename": filename,
                    }
                }
                status = send_matfix_message(
                    config["api_key"],
                    config["account_id"],
                    room_id,
                    file_payload,
                )
                if status == 202:
                    logger.info("File notification sent to room %s", room_id)
                    sent_file = True
                else:
                    logger.warning("Matfix attachment failure for room %s (status=%s)", room_id, status)
            except Exception as exc:
                logger.exception("Matfix attachment failure for room %s: %s", room_id, exc)

        if not sent_file:
            text_payload = {
                "type": "text",
                "body": message,
            }
            try:
                status = send_matfix_message(
                    config["api_key"],
                    config["account_id"],
                    room_id,
                    text_payload,
                )
                if status == 202:
                    if rtsp_enabled:
                        logger.info("Text-only fallback sent to room %s", room_id)
                    else:
                        pass
                else:
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
        with _seen_event_ids_lock:
            if event_id in _seen_event_ids:
                _seen_event_ids[event_id] = (_seen_event_ids[event_id][0], 502)
        _send_response(handler, 502)
        return

    logger.info("Callingbell event id=%s delivered to %d rooms", event_id, len(config["room_ids"]))
    with _seen_event_ids_lock:
        if event_id in _seen_event_ids:
            _seen_event_ids[event_id] = (_seen_event_ids[event_id][0], 200)
    _send_response(handler, 200)


# Initialize at startup
_initialize_rtsp_at_startup()
