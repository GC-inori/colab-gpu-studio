"""Application settings with optional root-level YAML configuration."""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

ENV_KEYS = {
    "server": {
        "site_url": "SITE_URL", "bind_ip": "BIND_IP", "bind_port": "BIND_PORT",
        "environment": "ENVIRONMENT", "max_upload_bytes": "MAX_UPLOAD_BYTES",
    },
    "security": {
        "studio_password": "STUDIO_PASSWORD", "encryption_key": "ENCRYPTION_KEY",
        "session_secret": "SESSION_SECRET", "studio_api_token": "STUDIO_API_TOKEN",
    },
    "gpu": {
        "enabled": "GPU_ENABLED", "real_enabled": "GPU_REAL_ENABLED", "storage": "GPU_STORAGE",
        "unit_rate": "GPU_UNIT_RATE", "daily_units": "GPU_DAILY_UNITS",
        "monthly_units": "GPU_MONTHLY_UNITS", "max_job_units": "GPU_MAX_JOB_UNITS",
        "max_runtime_minutes": "GPU_MAX_RUNTIME_MINUTES", "google_client_id": "GPU_GOOGLE_CLIENT_ID",
        "google_client_secret": "GPU_GOOGLE_CLIENT_SECRET", "google_redirect_uri": "GPU_GOOGLE_REDIRECT_URI",
    },
    "logging": {
        "enabled": "LOGGING_ENABLED", "level": "LOG_LEVEL", "file": "LOG_FILE",
        "max_bytes": "LOG_MAX_BYTES", "backup_count": "LOG_BACKUP_COUNT", "compress": "LOG_COMPRESS",
    },
}


def read_yaml_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError("config.yaml must contain a mapping")
    return value


def _dotenv_keys() -> set[str]:
    """Find keys in .env so YAML can act as fallback without overriding it."""
    if not ENV_PATH.exists():
        return set()
    keys = set()
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def sync_yaml_to_environment(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else read_yaml_config()
    dotenv_keys = _dotenv_keys()
    for section, mapping in ENV_KEYS.items():
        values = config.get(section) or {}
        if not isinstance(values, dict):
            raise ValueError(f"config.yaml section {section!r} must be a mapping")
        for key, env_name in mapping.items():
            if key not in values or env_name in dotenv_keys or env_name in os.environ:
                continue
            value = values[key]
            if isinstance(value, bool):
                encoded = "1" if value else "0"
            elif value is None:
                continue
            else:
                encoded = str(value)
            os.environ[env_name] = encoded
    return config


_yaml_config = sync_yaml_to_environment()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_PATH), extra="ignore", case_sensitive=False)
    database_url: str = "sqlite:///./data/studio.db"
    data_dir: Path = Path("./data")
    site_url: str = "http://localhost:8788"
    bind_ip: str = "127.0.0.1"
    bind_port: int = 8788
    studio_password: str = ""
    encryption_key: str = ""
    session_secret: str = ""
    environment: str = "development"
    max_upload_bytes: int = 100 * 1024 * 1024
    allowed_hosts: list[str] = []
    studio_api_token: str = ""


def set_runtime_setting(field: str, value: Any) -> None:
    """Update a setting and its environment representation in this process."""
    env_name = next((name for section in ENV_KEYS.values() for key, name in section.items() if key == field), None)
    if env_name:
        os.environ[env_name] = "1" if value is True else "0" if value is False else str(value)
    if hasattr(settings, field):
        setattr(settings, field, value)


def write_yaml_config(config: dict[str, Any]) -> None:
    """Persist config.yaml atomically, retaining unrelated configuration keys."""
    temporary = CONFIG_PATH.with_suffix(".yaml.tmp")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def write_dotenv_values(values: dict[str, Any]) -> None:
    """Update existing .env keys so legacy deployments retain runtime changes."""
    if not ENV_PATH.exists():
        return
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    pending = dict(values)
    output = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else None
        if key in pending:
            value = pending.pop(key)
            encoded = "1" if value is True else "0" if value is False else str(value)
            output.append(f"{key}={encoded}")
        else:
            output.append(line)
    if pending:
        output.extend(f"{key}={value}" for key, value in pending.items())
    temporary = ENV_PATH.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(ENV_PATH)


if os.getenv("STUDIO_OFFLINE_TEST") == "1":
    from .offline import install
    install()
    settings = Settings(_env_file=None)
else:
    settings = Settings(_env_file=None) if os.getenv("ENVIRONMENT") == "offline-test" else Settings()
