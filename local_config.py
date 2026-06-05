import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent
_LOCAL_CONFIG_PATHS = (
    _REPO_ROOT / "secrets" / "local_config.json",
    _REPO_ROOT / "local_config.json",
)
_MISSING = object()


@lru_cache(maxsize=1)
def _load_local_config() -> dict[str, Any]:
    for path in _LOCAL_CONFIG_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in local config file: {path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Local config file must contain a JSON object: {path}")
        return data
    return {}


def get_config(name: str, default: Any = _MISSING) -> Any:
    local_config = _load_local_config()
    if name in local_config:
        return local_config[name]
    if name in os.environ:
        return os.environ[name]
    if default is not _MISSING:
        return default
    paths = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in _LOCAL_CONFIG_PATHS)
    raise KeyError(
        f"Missing config '{name}'. Set the environment variable or add it to {paths}."
    )


def load_json_config(name: str) -> dict[str, Any]:
    value = get_config(name)
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise RuntimeError(f"Config '{name}' must be a JSON object or string")

    raw = value.strip()
    if raw.startswith("{"):
        return json.loads(raw)

    path = Path(raw)
    if not path.is_absolute():
        path = (_REPO_ROOT / path).resolve()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    if raw.endswith(".json"):
        raise FileNotFoundError(f"Config '{name}' points to a missing file: {path}")

    return json.loads(raw)