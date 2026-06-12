import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo


_LOGGING_CONFIGURED = False


class _TimezoneFormatter(logging.Formatter):
    """Custom formatter that applies timezone to log timestamps."""
    
    def __init__(self, fmt=None, datefmt=None, timezone='UTC'):
        super().__init__(fmt, datefmt)
        self.timezone = timezone
    
    def formatTime(self, record, datefmt=None):
        """Format the time using the configured timezone."""
        try:
            dt = datetime.fromtimestamp(record.created, tz=ZoneInfo('UTC'))
            target_dt = dt.astimezone(ZoneInfo(self.timezone))
            if datefmt:
                return target_dt.strftime(datefmt)
            else:
                return target_dt.isoformat()
        except (ValueError, Exception):
            # Fall back to default formatting if timezone is invalid
            return super().formatTime(record, datefmt)


def setup_logging(log_file: Optional[str] = None, level: int = logging.INFO, timezone: str = 'UTC') -> None:
    global _LOGGING_CONFIGURED

    if _LOGGING_CONFIGURED:
        return

    base_dir = os.path.dirname(os.path.dirname(__file__))
    logs_dir = os.path.join(base_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    if log_file is None:
        log_file = os.path.join(logs_dir, "iot.log")

    formatter = _TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        timezone=timezone,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=0,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
