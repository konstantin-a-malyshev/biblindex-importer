"""Settings loaded from environment variables (and the project's .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://www.biblindex.org"
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    bibles: str = "all"  # "all" or ids like "8,4,2"
    locale: str = "en"
    min_interval: float = 1.1
    public_min_interval: float = 0.5
    max_reset_wait: float = 2.0
    cache_path: Path = Path(".cache/biblindex.sqlite3")
    cache_ttl_days: float = 30.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> Settings:
        # Real environment variables take precedence over the .env file.
        env = {**dotenv_values(env_file), **os.environ} if env_file else dict(os.environ)

        def get(name: str, default: str) -> str:
            value = (env.get(name) or "").strip()
            return value or default

        try:
            return cls(
                base_url=get("BIBLINDEX_BASE_URL", cls.base_url).rstrip("/"),
                client_id=get("BIBLINDEX_CLIENT_ID", ""),
                client_secret=get("BIBLINDEX_CLIENT_SECRET", ""),
                username=get("BIBLINDEX_USERNAME", ""),
                password=get("BIBLINDEX_PASSWORD", ""),
                bibles=_bible_spec(get("BIBLINDEX_BIBLE_ID", cls.bibles)),
                locale=get("BIBLINDEX_LOCALE", cls.locale),
                min_interval=float(get("BIBLINDEX_MIN_INTERVAL", str(cls.min_interval))),
                public_min_interval=float(
                    get("BIBLINDEX_PUBLIC_MIN_INTERVAL", str(cls.public_min_interval))
                ),
                max_reset_wait=float(get("BIBLINDEX_MAX_RESET_WAIT", str(cls.max_reset_wait))),
                cache_path=Path(get("BIBLINDEX_CACHE_PATH", str(cls.cache_path))),
                cache_ttl_days=float(get("BIBLINDEX_CACHE_TTL_DAYS", str(cls.cache_ttl_days))),
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid value in configuration: {exc}") from exc


def _bible_spec(value: str) -> str:
    """Validate "all" or a comma-separated list of Bible ids."""
    if value.strip().casefold() == "all" or re.fullmatch(r"\s*\d+(\s*,\s*\d+)*\s*", value):
        return value.strip()
    raise ValueError(f"BIBLINDEX_BIBLE_ID must be 'all' or ids like 8,4,2, not {value!r}")
