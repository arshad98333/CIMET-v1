import os
from pathlib import Path


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def is_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def get_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def present_any(*names: str) -> bool:
    return get_env(*names) is not None


def public_base_url() -> str | None:
    value = get_env("PUBLIC_BASE_URL", "SERVER_EXTERNAL_URL", "PUBLIC_HOSTNAME")
    if not value:
        return None
    value = value.strip().rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}"


def public_ws_base_url() -> str | None:
    value = public_base_url()
    if not value:
        return None
    if value.startswith("https://"):
        return "wss://" + value[len("https://") :]
    if value.startswith("http://"):
        return "ws://" + value[len("http://") :]
    return value
