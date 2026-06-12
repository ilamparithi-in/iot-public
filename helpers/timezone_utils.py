"""
Timezone utilities for converting between arbitrary timezones and UTC.
All timestamps are stored internally as Unix UTC timestamps (seconds since epoch).
This module provides convenience functions for displaying timestamps in different timezones.
"""

from zoneinfo import ZoneInfo
from datetime import datetime
from .config_reader import get_config_value


def validate_timezone(tz_name):
    """
    Validate that a timezone name is a valid IANA timezone string.
    
    Args:
        tz_name (str): IANA timezone name (e.g., 'Asia/Kolkata', 'US/Eastern', 'UTC')
    
    Raises:
        ValueError: If the timezone is invalid or not recognized
    
    Returns:
        bool: True if valid
    """
    try:
        ZoneInfo(tz_name)
        return True
    except Exception as e:
        raise ValueError(f"Invalid timezone '{tz_name}': {e}")


def get_server_timezone():
    """
    Get the configured server timezone from app.yaml.
    Falls back to 'UTC' if not configured.
    
    Returns:
        str: IANA timezone name
    """
    tz = get_config_value("server.timezone", "UTC")
    try:
        validate_timezone(tz)
    except ValueError as e:
        # Log error but don't crash; fallback to UTC
        from .app_logging import get_logger
        logger = get_logger(__name__)
        logger.warning(f"{e}. Falling back to UTC")
        return "UTC"
    return tz


def to_utc(timestamp, from_tz):
    """
    Convert a Unix timestamp from one timezone to UTC (no-op for display, but normalizes input).
    
    Args:
        timestamp (int/float): Unix timestamp (seconds since epoch)
        from_tz (str): IANA timezone name (e.g., 'Asia/Kolkata')
    
    Returns:
        int: UTC Unix timestamp (same as input if input is already in UTC)
    
    Raises:
        ValueError: If from_tz is invalid
    """
    validate_timezone(from_tz)
    # Unix timestamps are always in UTC regardless of timezone
    # This function exists for API clarity and potential future use
    return int(timestamp)


def from_utc(utc_timestamp, to_tz):
    """
    Convert a Unix UTC timestamp to the target timezone (for display/formatting).
    
    Args:
        utc_timestamp (int/float): Unix timestamp in UTC (seconds since epoch)
        to_tz (str): IANA timezone name (e.g., 'Asia/Kolkata')
    
    Returns:
        datetime: Timezone-aware datetime object in the target timezone
    
    Raises:
        ValueError: If to_tz is invalid
    """
    validate_timezone(to_tz)
    # Create UTC datetime, then convert to target timezone
    utc_dt = datetime.fromtimestamp(utc_timestamp, tz=ZoneInfo("UTC"))
    target_dt = utc_dt.astimezone(ZoneInfo(to_tz))
    return target_dt


def format_timestamp(utc_timestamp, to_tz, format_str="%Y-%m-%d %H:%M:%S"):
    """
    Convert a UTC timestamp to a target timezone and format as a string.
    Convenience function combining from_utc() and strftime().
    
    Args:
        utc_timestamp (int/float): Unix timestamp in UTC (seconds since epoch)
        to_tz (str): IANA timezone name (e.g., 'Asia/Kolkata')
        format_str (str): strftime format string (default: "%Y-%m-%d %H:%M:%S")
    
    Returns:
        str: Formatted timestamp string in the target timezone
    
    Raises:
        ValueError: If to_tz is invalid
    """
    target_dt = from_utc(utc_timestamp, to_tz)
    return target_dt.strftime(format_str)


def convert_between_timezones(utc_timestamp, to_tz, format_str="%Y-%m-%d %H:%M:%S"):
    """
    Alias for format_timestamp(). Convert UTC timestamp to another timezone and format.
    
    Args:
        utc_timestamp (int/float): Unix timestamp in UTC
        to_tz (str): IANA timezone name
        format_str (str): strftime format string
    
    Returns:
        str: Formatted timestamp string in the target timezone
    """
    return format_timestamp(utc_timestamp, to_tz, format_str)
