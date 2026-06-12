#!/usr/bin/env python3
import os
import socket
import sys
import time
from urllib import error, request

import serial
from serial.serialutil import SerialException


SERIAL_PATH = os.getenv("ARDUINO_SERIAL_PATH")
API_BASE_URL = os.getenv("POCHINO_API_BASE_URL", "http://matfix.example.com")
DEVICE_NAME = os.getenv("POCHINO_DEVICE_NAME", socket.gethostname())


def exit_with_error(message: str, code: int) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


def call_pochino_api(action: str, timeout_seconds: int = 5) -> None:
    url = f"{API_BASE_URL.rstrip('/')}/pochino/{action}"
    req = request.Request(
        url,
        method="POST",
        headers={
            "X-Pochino-Device": DEVICE_NAME,
        },
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            if response.status != 200:
                body = response.read().decode("utf-8", errors="ignore")
                print(f"Pochino API failed for {action}: HTTP {response.status} {body}", file=sys.stderr)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print(f"Pochino API failed for {action}: HTTP {exc.code} {body}", file=sys.stderr)
    except error.URLError as exc:
        print(f"Pochino API network error for {action}: {exc.reason}", file=sys.stderr)


def main() -> None:
    if not SERIAL_PATH:
        exit_with_error("ARDUINO_SERIAL_PATH is not set", 64)

    initialized = False

    while True:
        try:
            try:
                ser = serial.Serial(SERIAL_PATH, 115200, timeout=1)
            except SerialException as exc:
                error_text = str(exc)
                if (getattr(exc, "errno", None) == 2) or "could not open port" in error_text:
                    exit_with_error(
                        f"Unable to open serial port '{SERIAL_PATH}': {error_text}",
                        67,
                    )
                raise

            while True:
                try:
                    line = ser.readline().decode(errors="ignore").strip()
                except SerialException as exc:
                    error_text = str(exc)
                    if "device reports readiness to read but returned no data" in error_text:
                        exit_with_error(
                            f"Serial device disconnected or grabbed elsewhere ({error_text}); exiting.",
                            3,
                        )
                    raise

                if line == "ON":
                    if not initialized:
                        initialized = True
                    else:
                        call_pochino_api("on")
                elif line == "OFF":
                    call_pochino_api("off")
                elif line == "HI":
                    print("Initialized, Hi!")

        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            time.sleep(15)
            raise


if __name__ == "__main__":
    main()
