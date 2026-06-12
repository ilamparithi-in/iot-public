import json
import socket
import time
from urllib import error, request

from helpers.app_logging import get_logger
from helpers.config_reader import ConfigError, load_yaml_config
from helpers.timezone_utils import format_timestamp, get_server_timezone


ROUTE = "/pochino"
logger = get_logger(__name__)


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


def _load_pochino_config():
	config = load_yaml_config("pochino.yaml")

	if not isinstance(config.get("matfix_url"), str) or not config.get("matfix_url").strip():
		raise ConfigError("pochino.yaml: matfix_url is required")

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

	return {
		"matfix_url": config["matfix_url"].rstrip("/"),
		"api_key": config["api_key"],
		"account_id": config["account_id"],
		"room_ids": room_ids,
		"messages": normalized_messages,
		"request_timeout_seconds": int(config.get("request_timeout_seconds", 5)),
	}


def _send_matfix_message(matfix_url, api_key, account_id, room_id, message, timeout):
	url = f"{matfix_url}/v1/send"
	payload = json.dumps(
		{
			"account_id": account_id,
			"destination": room_id,
			"message": {
				"type": "text",
				"body": message,
			},
		}
	).encode("utf-8")

	req = request.Request(
		url,
		data=payload,
		method="POST",
		headers={
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
		},
	)
	with request.urlopen(req, timeout=timeout) as resp:
		return resp.status


def _resolve_device_name(handler):
	device_name = handler.headers.get("X-Pochino-Device", "").strip()
	if device_name:
		return device_name
	return socket.gethostname()


def _build_message(template, device_name):
	formatted_time = format_timestamp(int(time.time()), get_server_timezone())
	return f"{template}\nDevice: {device_name}\nTime: {formatted_time}"


def handle(handler):
	if handler.command != "POST":
		_send_response(handler, 405, "Method not allowed. Use POST /pochino/on or POST /pochino/off")
		return

	action = handler.path.split("?", 1)[0].removeprefix(ROUTE).strip("/").lower()
	if not action:
		_send_response(handler, 400, "Missing pochino action. Use POST /pochino/on or POST /pochino/off")
		return

	try:
		config = _load_pochino_config()
	except (ConfigError, ValueError, TypeError) as exc:
		logger.error("Pochino config error: %s", exc)
		_send_response(handler, 500, "Pochino config error")
		return

	template = config["messages"].get(action)
	if not isinstance(template, str) or not template:
		_send_response(handler, 404, f"Unknown pochino action: {action}")
		return

	full_message = _build_message(template, _resolve_device_name(handler))
	failures = []

	for room_id in config["room_ids"]:
		try:
			status = _send_matfix_message(
				config["matfix_url"],
				config["api_key"],
				config["account_id"],
				room_id,
				full_message,
				config["request_timeout_seconds"],
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

	if failures:
		_send_response(handler, 502, "Message delivery failed for: " + ", ".join(failures))
		return

	logger.info("Pochino action '%s' sent to %d rooms", action, len(config["room_ids"]))
	_send_response(handler, 200, f"Pochino action '{action}' sent")
