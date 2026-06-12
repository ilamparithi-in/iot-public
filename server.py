from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import logging
import os
import signal
from pathlib import Path
from urllib.parse import urlsplit

from helpers.app_logging import get_logger, setup_logging
from helpers.config_reader import ConfigError, load_yaml_config


logger = get_logger(__name__)
MODULE_DIR = os.path.join(os.path.dirname(__file__), "modules")
routes = {}
server = None


def _coerce_log_level(level_name):
    if not isinstance(level_name, str):
        return logging.INFO

    resolved = getattr(logging, level_name.upper(), None)
    if isinstance(resolved, int):
        return resolved

    return logging.INFO


def _resolve_log_file_path(log_file):
    if not log_file:
        return None

    path = Path(log_file)
    if path.is_absolute():
        return str(path)

    return str((Path(__file__).resolve().parent / path).resolve())


def load_routes():
    routes.clear()

    for filename in os.listdir(MODULE_DIR):
        if not filename.endswith(".py"):
            continue

        module_name = filename[:-3]
        file_path = os.path.join(MODULE_DIR, filename)

        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            logger.error("Skipping %s: module spec/loader not available", filename)
            continue

        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to load module from %s", filename)
            continue

        route = getattr(module, "ROUTE", None)
        handler = getattr(module, "handle", None)

        if route and handler:
            routes[route] = handler
            logger.info("Loaded route %s from %s", route, filename)
        else:
            logger.warning("Skipping %s: missing ROUTE or handle", filename)

class RequestHandler(BaseHTTPRequestHandler):
    @staticmethod
    def _find_handler(path):
        handler = routes.get(path)
        if handler:
            return handler

        for route, candidate in sorted(routes.items(), key=lambda item: len(item[0]), reverse=True):
            if path.startswith(route + "/"):
                return candidate

        return None

    def log_message(self, format, *args):
        logger.info("HTTP %s - %s", self.address_string(), format % args)

    def _dispatch(self):
        logger.info("Incoming %s %s", self.command, self.path)
        request_path = urlsplit(self.path).path
        handler = self._find_handler(request_path)

        if handler:
            logger.debug("Dispatching handler for %s", request_path)
            handler(self)
            return

        logger.warning("No route found for %s", request_path)
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()


def _shutdown(signum=None, _frame=None):
    global server

    signal_name = signal.Signals(signum).name if signum is not None else "KEYBOARD_INTERRUPT"
    logger.info("Shutdown requested via %s", signal_name)

    if server is not None:
        server.shutdown()


def main():
    global server

    host = "0.0.0.0"
    port = 8000
    log_level = logging.INFO
    log_file = None
    timezone = "UTC"

    try:
        config = load_yaml_config("app.yaml")

        server_config = config.get("server", {}) if isinstance(config.get("server", {}), dict) else {}
        logging_config = config.get("logging", {}) if isinstance(config.get("logging", {}), dict) else {}

        host = server_config.get("host", host)
        port = int(server_config.get("port", port))
        timezone = server_config.get("timezone", timezone)
        log_level = _coerce_log_level(logging_config.get("level", "INFO"))
        log_file = _resolve_log_file_path(logging_config.get("file"))
    except (ConfigError, ValueError, TypeError) as exc:
        print(f"Config load failed, using defaults: {exc}")

    setup_logging(log_file=log_file, level=log_level, timezone=timezone)
    load_routes()

    server = HTTPServer((host, port), RequestHandler)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("IoT server listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown()
    finally:
        if server is not None:
            server.server_close()
        logger.info("IoT server has stopped")


if __name__ == "__main__":
    main()