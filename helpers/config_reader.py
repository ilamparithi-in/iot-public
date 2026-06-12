from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


CONFIG_DIR = Path(__file__).resolve().parent.parent / ".config"


class ConfigError(Exception):
    pass


def _resolve_config_path(file_name: str) -> Path:
    if not file_name.endswith((".yaml", ".yml")):
        raise ConfigError("Config file must use .yaml or .yml extension")

    config_path = (CONFIG_DIR / file_name).resolve()

    if not str(config_path).startswith(str(CONFIG_DIR.resolve())):
        raise ConfigError("Config file path must stay inside .config/")

    return config_path


def load_yaml_config(file_name: str = "app.yaml") -> dict[str, Any]:
    config_path = _resolve_config_path(file_name)

    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Root YAML object in {config_path} must be a mapping")

    return data


def get_config_value(key: str, default: Any = None, file_name: str = "app.yaml") -> Any:
    config = load_yaml_config(file_name=file_name)

    if not key:
        return config

    current: Any = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]

    return current
