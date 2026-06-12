from helpers.app_logging import get_logger


ROUTE = "/status"
logger = get_logger(__name__)

def handle(handler):
    logger.info("Status check requested")
    handler.send_response(200)
    handler.end_headers()
    handler.wfile.write(b"alive lol")