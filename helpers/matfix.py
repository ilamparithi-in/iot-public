import json
from urllib import request

from helpers.config_reader import ConfigError, get_config_value


def _get_matfix_url():
	matfix_url = get_config_value("matfix.url")
	if not isinstance(matfix_url, str) or not matfix_url.strip():
		raise ConfigError("app.yaml: matfix.url is required")
	return matfix_url.rstrip("/")


def _get_matfix_request_timeout():
	request_timeout = get_config_value("matfix.request_timeout", 5)
	if isinstance(request_timeout, bool):
		raise ConfigError("app.yaml: matfix.request_timeout must be an integer")
	try:
		request_timeout = int(request_timeout)
	except (TypeError, ValueError):
		raise ConfigError("app.yaml: matfix.request_timeout must be an integer")
	if request_timeout <= 0:
		raise ConfigError("app.yaml: matfix.request_timeout must be greater than 0")
	return request_timeout


def send_matfix_message(
	api_key,
	account_id,
	destination,
	message,
	timeout=None,
	idempotency_key=None,
	matfix_url=None,
):
	resolved_matfix_url = _get_matfix_url() if matfix_url is None else matfix_url.rstrip("/")
	resolved_timeout = _get_matfix_request_timeout() if timeout is None else timeout
	url = f"{resolved_matfix_url}/v1/send"
	payload = {
		"account_id": account_id,
		"destination": destination,
		"message": message,
	}
	if idempotency_key is not None:
		payload["idempotency_key"] = idempotency_key

	body = json.dumps(payload).encode("utf-8")

	req = request.Request(
		url,
		data=body,
		method="POST",
		headers={
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
		},
	)
	with request.urlopen(req, timeout=resolved_timeout) as resp:
		return resp.status
