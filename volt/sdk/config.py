"""Configuration: API key resolution, safe storage and base URL validation."""

from __future__ import annotations

import os
import re
import stat
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

from .errors import ConfigError

DEFAULT_BASE_URL = "https://api.voltagegpu.com/api"
API_KEY_ENV_VARS = ("VOLT_API_KEY", "VOLTAGEGPU_API_KEY")
BASE_URL_ENV_VAR = "VOLT_BASE_URL"
API_KEYS_PAGE = "https://voltagegpu.com/api-keys"

# Keys look like volt_<32 hex>_<32 hex>; the server only requires the prefix,
# so the client stays tolerant but refuses anything that could smuggle a
# header break or whitespace into a request.
_API_KEY_RE = re.compile(r"volt_[A-Za-z0-9_-]{8,200}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOW_CUSTOM_BASE_URL_ENV_VAR = "VOLT_ALLOW_CUSTOM_BASE_URL"


def default_config_path() -> Path:
    """``~/.volt/config.ini`` (kept from earlier releases)."""
    return Path.home() / ".volt" / "config.ini"


def is_valid_api_key(key: str) -> bool:
    return bool(_API_KEY_RE.fullmatch(key or ""))


def mask_api_key(key: str) -> str:
    """Show only enough of a key to tell two keys apart."""
    if not key:
        return "(none)"
    return f"volt_****{key[-4:]}" if len(key) > 12 else "****"


def validate_base_url(url: str, allow_custom: bool = False) -> str:
    """Return a normalised base URL, refusing anything that would leak the key.

    The API key travels in a header on every request: plain HTTP is only
    accepted for a local development server, and a host outside
    voltagegpu.com needs an explicit opt-in (VOLT_ALLOW_CUSTOM_BASE_URL=1), so
    a poisoned environment variable cannot quietly send the key elsewhere.
    """
    candidate = (url or "").strip().rstrip("/")
    parts = urlsplit(candidate)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"Invalid base URL {candidate!r}: expected https://host/path.")
    if parts.username or parts.password:
        raise ConfigError("The base URL must not contain credentials.")
    host = parts.hostname.lower()
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ConfigError("Refusing a plain http:// base URL: the API key would be sent unencrypted. Use https://.")
    ours = host == "voltagegpu.com" or host.endswith(".voltagegpu.com") or host in _LOCAL_HOSTS
    if not ours and not allow_custom:
        raise ConfigError(
            f"Refusing to send your API key to {host}: only voltagegpu.com hosts are allowed. "
            f"Set {ALLOW_CUSTOM_BASE_URL_ENV_VAR}=1 if this is intended."
        )
    return candidate


def _load(path: Path) -> ConfigParser:
    """Parse the config file without ever echoing its content in an error
    (configparser messages quote the offending line, which may be the key)."""
    parser = ConfigParser()
    if path.exists():
        try:
            parser.read(path, encoding="utf-8")
        except Exception as exc:  # configparser raises several unrelated types
            raise ConfigError(f"Cannot read {path} ({type(exc).__name__}). Fix or delete it, then run `voltagegpu login`.") from None
    return parser


def _write_private(path: Path, parser: ConfigParser) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)  # an existing 0644 file is tightened before the key is written
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        parser.write(handle)


def _read_key_file(path: Path) -> Optional[str]:
    value = _load(path).get("api", "api_key", fallback=None)
    return value.strip() if value else None


def permissions_warning(path: Path) -> Optional[str]:
    """A warning when the key file is readable by other users (POSIX only)."""
    if sys.platform.startswith("win") or not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"{path} is readable by other users (mode {oct(mode)}). Fix it with: chmod 600 {path}"
    return None


def save_api_key(key: str, path: Optional[Path] = None) -> Path:
    """Write the key to the config file, readable by the current user only."""
    if not is_valid_api_key(key):
        raise ConfigError(f"That does not look like a VoltageGPU API key (volt_...). Create one at {API_KEYS_PAGE}.")
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    parser = _load(target)
    if not parser.has_section("api"):
        parser.add_section("api")
    parser.set("api", "api_key", key)
    # O_CREAT with 0o600 (+ fchmod) so the file is never readable by others, not even briefly.
    # On Windows the file inherits the ACL of the user profile folder.
    _write_private(target, parser)
    return target


def remove_api_key(path: Optional[Path] = None) -> bool:
    """Remove the stored key. Returns True if one was removed."""
    target = path or default_config_path()
    if not target.exists():
        return False
    parser = _load(target)
    if not parser.has_option("api", "api_key"):
        return False
    parser.remove_option("api", "api_key")
    _write_private(target, parser)
    return True


@dataclass(frozen=True)
class Config:
    """Resolved client configuration."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    source: str = "argument"

    def __repr__(self) -> str:  # never print the key, even in a traceback
        return f"Config(api_key={mask_api_key(self.api_key)!r}, base_url={self.base_url!r}, source={self.source!r})"

    @property
    def masked_key(self) -> str:
        return mask_api_key(self.api_key)

    @classmethod
    def load(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        config_path: Optional[Path] = None,
    ) -> "Config":
        """Resolve the key: explicit argument, then environment, then config file."""
        environ = os.environ if env is None else env
        key, source = (api_key, "argument") if api_key else (None, "")
        if not key:
            for name in API_KEY_ENV_VARS:
                # A vgpu_ value is an inference key (public docs): never sent to the pod API.
                if environ.get(name) and not environ[name].strip().startswith("vgpu_"):
                    key, source = environ[name].strip(), f"environment ({name})"
                    break
        if not key:
            path = config_path or default_config_path()
            key = _read_key_file(path)
            source = f"file ({path})" if key else ""
        if not key:
            raise ConfigError(
                "No API key found. Create one at "
                f"{API_KEYS_PAGE}, then run `voltagegpu login` or export VOLT_API_KEY=volt_..."
            )
        if not is_valid_api_key(key):
            raise ConfigError(f"The API key from {source} is malformed: VoltageGPU keys start with volt_.")
        url = validate_base_url(
            base_url or environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL,
            allow_custom=environ.get(ALLOW_CUSTOM_BASE_URL_ENV_VAR) == "1",
        )
        return cls(api_key=key, base_url=url, source=source)


__all__ = [
    "API_KEYS_PAGE",
    "API_KEY_ENV_VARS",
    "Config",
    "DEFAULT_BASE_URL",
    "default_config_path",
    "is_valid_api_key",
    "mask_api_key",
    "permissions_warning",
    "remove_api_key",
    "save_api_key",
    "validate_base_url",
]
