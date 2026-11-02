"""Reads ~/.config/scaleconnect/config.toml. Addresses and secrets live only there.

A scale with no explicit true write_enabled flag is not write enabled.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_PORT = 50036
_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_PLACEHOLDER_SECRET = "change-me"
_CALLER = re.compile(r"[A-Za-z0-9 ._@/-]{1,64}")
SCALELINK_NOT_AVAILABLE = "not_available_here"
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_CONNECT_WAIT_SECONDS = 30.0
_BUSY_GAP_SECONDS = 15.0  # a scale ignores connections for about 10 to 11 seconds after a write
_REPLY_WAIT_FLOOR = 5.0  # a 3 second limit is what made ScaleLink fail


def default_config_path() -> Path:
    return Path.home() / ".config" / "scaleconnect" / "config.toml"


def _default_state_dir() -> Path:
    return Path.home() / ".config" / "scaleconnect"


@dataclass(frozen=True)
class ScaleConfig:
    name: str
    address: str
    port: int = DEFAULT_PORT
    write_enabled: bool = False
    serial: str | None = None
    read_enabled: bool = False


@dataclass(frozen=True)
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class Settings:
    reply_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    connect_attempts: int = 5
    connect_retry_wait_seconds: float = 5.0
    batch_size: int = 5
    read_pace_seconds: float = 0.2
    read_back_window_seconds: float = 45.0
    read_back_retry_seconds: float = 2.0
    description_max_length: int = 35
    preview_ttl_seconds: int = 900
    state_dir: Path = field(default_factory=_default_state_dir)
    catalog_path: Path | None = None
    backup_dir: Path | None = None
    scalelink_db_path: Path | None = None
    scalelink_db_copy: str | None = None

    def __post_init__(self) -> None:
        state_dir = Path(self.state_dir).expanduser()
        object.__setattr__(self, "state_dir", state_dir)
        catalog = Path(self.catalog_path).expanduser() if self.catalog_path else state_dir / "catalog.sqlite3"
        object.__setattr__(self, "catalog_path", catalog)
        backups = Path(self.backup_dir).expanduser() if self.backup_dir else state_dir / "backups"
        object.__setattr__(self, "backup_dir", backups)
        if self.scalelink_db_path:
            object.__setattr__(self, "scalelink_db_path", Path(self.scalelink_db_path).expanduser())
        times = (
            self.reply_timeout_seconds,
            self.connect_timeout_seconds,
            self.connect_retry_wait_seconds,
            self.read_pace_seconds,
            self.read_back_window_seconds,
            self.read_back_retry_seconds,
        )
        if not all(math.isfinite(value) for value in times):
            raise ConfigError("Timeouts and waits must be ordinary numbers.")
        if self.reply_timeout_seconds <= 0 or self.connect_timeout_seconds <= 0:
            raise ConfigError("Timeouts must be more than 0 seconds.")
        if min(times) < 0:
            raise ConfigError("Retry waits cannot be negative.")
        if self.scalelink_db_copy not in (None, SCALELINK_NOT_AVAILABLE):
            raise ConfigError(f"scalelink_db_copy can only be {SCALELINK_NOT_AVAILABLE}.")
        if not 1 <= self.connect_attempts <= 100:
            raise ConfigError("connect_attempts must be from 1 to 100.")
        if not 1 <= self.batch_size <= 5:
            raise ConfigError("batch_size must be from 1 to 5. Only 5 is proven by a read back.")
        if not 1 <= self.description_max_length <= 200:
            raise ConfigError("description_max_length must be from 1 to 200.")
        if self.preview_ttl_seconds <= 0:
            raise ConfigError("preview_ttl_seconds must be more than 0.")


def connect_retry_span_seconds(settings: Settings) -> float:
    """The real time connect retries cover if every refusal comes back at once."""
    wait = settings.connect_retry_wait_seconds
    total = 0.0
    for _ in range(settings.connect_attempts - 1):
        total += wait
        wait = min(wait * 2, MAX_CONNECT_WAIT_SECONDS)
    return total


@dataclass(frozen=True)
class Config:
    scales: tuple[ScaleConfig, ...] = ()
    settings: Settings = field(default_factory=Settings)
    pin: str | None = field(default=None, repr=False)
    mcp_token: str | None = field(default=None, repr=False)
    mcp_caller: str | None = None
    web: ListenConfig = field(default_factory=lambda: ListenConfig(port=8080))
    mcp: ListenConfig = field(default_factory=lambda: ListenConfig(port=8765))
    path: Path | None = None
    warnings: tuple[str, ...] = ()


# Gone. A record is never resent automatically. An old config still loads, with a warning.
_REMOVED_SETTINGS = frozenset({"unknown_retries", "unknown_retry_wait_seconds"})
_NUMBER_SETTINGS = {
    "reply_timeout_seconds": float,
    "connect_timeout_seconds": float,
    "connect_retry_wait_seconds": float,
    "read_pace_seconds": float,
    "read_back_window_seconds": float,
    "read_back_retry_seconds": float,
}
_INT_SETTINGS = {
    "connect_attempts",
    "batch_size",
    "description_max_length",
    "preview_ttl_seconds",
}
_PATH_SETTINGS = {"state_dir", "catalog_path", "backup_dir", "scalelink_db_path"}


def _parse_settings(raw: dict[str, Any]) -> Settings:
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _NUMBER_SETTINGS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"Setting {key} must be a number.")
            kwargs[key] = float(value)
        elif key in _INT_SETTINGS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"Setting {key} must be a whole number.")
            kwargs[key] = value
        elif key == "scalelink_db_copy":
            if not isinstance(value, str):
                raise ConfigError("Setting scalelink_db_copy must be text.")
            kwargs[key] = value
        elif key in _PATH_SETTINGS:
            if not isinstance(value, str):
                raise ConfigError(f"Setting {key} must be text.")
            kwargs[key] = Path(value)
        else:
            raise ConfigError(f"Unknown setting {key}.")
    return Settings(**kwargs)


def _flag(entry: dict[str, Any], key: str, name: str, warnings: list[str]) -> bool:
    flag = entry.get(key, False)
    if flag is not True and flag is not False:
        warnings.append(f"Scale {name}: {key} is not true or false, so it stays off.")
    return flag is True


def _parse_scales(raw: Any) -> tuple[tuple[ScaleConfig, ...], list[str]]:
    if not isinstance(raw, list):
        raise ConfigError("The config needs [[scales]] entries.")
    scales: list[ScaleConfig] = []
    warnings: list[str] = []
    seen: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    serials: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("Each [[scales]] entry must be a table.")
        name = entry.get("name")
        address = entry.get("address")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigError("Each scale needs a name of letters, digits, _ or - only.")
        if name.casefold() in seen:
            raise ConfigError(f"Scale {name} is listed twice.")
        seen.add(name.casefold())
        if not isinstance(address, str) or not address.strip():
            raise ConfigError(f"Scale {name} needs an address.")
        port = entry.get("port", DEFAULT_PORT)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"Scale {name} has a bad port.")
        endpoint = (address.strip().casefold(), port)
        if endpoint in endpoints:
            raise ConfigError(f"Scale {name} shares an address with another entry. Each scale needs its own.")
        endpoints.add(endpoint)
        write_enabled = _flag(entry, "write_enabled", name, warnings)
        read_enabled = _flag(entry, "read_enabled", name, warnings)
        serial = entry.get("serial")
        if serial is not None and (not isinstance(serial, str) or not serial.strip()):
            raise ConfigError(f"Scale {name} has a bad serial.")
        if serial is not None:
            if serial in serials:
                raise ConfigError(f"Scale {name} shares a serial with another entry. Each scale has its own.")
            serials.add(serial)
        if (write_enabled or read_enabled) and not serial:
            raise ConfigError(f"Scale {name} is enabled, so it needs a serial in the config.")
        scales.append(
            ScaleConfig(
                name=name,
                address=address,
                port=port,
                write_enabled=write_enabled,
                serial=serial,
                read_enabled=read_enabled,
            )
        )
    return tuple(scales), warnings

def listen_problem(host: Any) -> str | None:
    """Why a host cannot be listened on, or None. Only one real IPv4 address will do.

    A name can resolve to several addresses, and a wildcard opens every network the PC has.
    """
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        return f"must be one IPv4 address, such as 192.0.2.10. Names and wildcards are not allowed, and {host!r} is not one."
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return f"must be one real address of this PC, and {host!r} is not."
    return None


def _parse_listen(raw: Any, section: str, default_port: int, warnings: list[str]) -> ListenConfig:
    if raw is None:
        return ListenConfig(port=default_port)
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] must be a table.")
    host = raw.get("host", "127.0.0.1")
    port = raw.get("port", default_port)
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(f"[{section}] needs text for host and a whole number for port.")
    problem = listen_problem(host)
    if problem:
        raise ConfigError(f"[{section}] host {problem}")
    if ipaddress.IPv4Address(host) in _TAILSCALE:
        warnings.append(f"[{section}] host is a Tailscale address, so this listens on the tailnet, not the store network.")
    return ListenConfig(host=host, port=port)


def _require_private(path: Path, handle: Any) -> None:
    """On Linux the file holds the PIN and token, so only its owner may read it. Windows has no such modes."""
    if os.name == "posix" and os.fstat(handle.fileno()).st_mode & 0o077:
        raise ConfigError(f"Other users can read the config file at {path}. Run: chmod 600 {path}")


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Path order: argument, SCALECONNECT_CONFIG variable, the default path."""
    chosen = path or os.environ.get("SCALECONNECT_CONFIG") or default_config_path()
    resolved = Path(chosen).expanduser()
    try:
        with open(resolved, "rb") as handle:
            _require_private(resolved, handle)
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"No config file at {resolved}. See config.example.toml.") from None
    except OSError as exc:
        raise ConfigError(f"Cannot read the config file at {resolved}: {exc.strerror}.") from None
    except tomllib.TOMLDecodeError:
        # The parser's text can quote a line of the file, which may hold a secret.
        raise ConfigError(f"The config file at {resolved} is not valid TOML.") from None

    scales, warnings = _parse_scales(raw.get("scales", []))
    settings_raw = raw.get("settings", {})
    if not isinstance(settings_raw, dict):
        raise ConfigError("[settings] must be a table.")
    auth = raw.get("auth", {})
    if not isinstance(auth, dict):
        raise ConfigError("[auth] must be a table.")
    pin = auth.get("pin")
    token = auth.get("mcp_token")
    if pin == _PLACEHOLDER_SECRET:
        pin = None
        warnings.append("The PIN is still the example value, so it counts as not set.")
    if token == _PLACEHOLDER_SECRET:
        token = None
        warnings.append("The MCP token is still the example value, so it counts as not set.")
    if pin is not None and not isinstance(pin, str):
        raise ConfigError("[auth] pin must be text, in quotes.")
    if token is not None and not isinstance(token, str):
        raise ConfigError("[auth] mcp_token must be text, in quotes.")
    for key in sorted(_REMOVED_SETTINGS & set(settings_raw)):
        warnings.append(f"Setting {key} is no longer used, because a record is never resent automatically. Remove it.")
    caller = auth.get("mcp_caller")
    if caller is not None:
        if not isinstance(caller, str) or not _CALLER.fullmatch(caller.strip()):
            raise ConfigError("[auth] mcp_caller must be a short name of letters, digits, spaces and . _ @ / -")
        caller = caller.strip()
    settings = _parse_settings({k: v for k, v in settings_raw.items() if k not in _REMOVED_SETTINGS})
    if settings.reply_timeout_seconds < _REPLY_WAIT_FLOOR:
        warnings.append("The reply wait is under 5 seconds. A real scale can pause for over 3 seconds.")
    if connect_retry_span_seconds(settings) < _BUSY_GAP_SECONDS:
        warnings.append("Connect retries cover under 15 seconds if refusals come at once. A scale can ignore connections for 11 seconds after a write.")
    if settings.read_back_window_seconds < _BUSY_GAP_SECONDS:
        warnings.append("The read back window is under 15 seconds. A scale can ignore connections for 11 seconds after a write.")
    return Config(
        scales=scales,
        settings=settings,
        pin=pin,
        mcp_token=token,
        mcp_caller=caller,
        web=_parse_listen(raw.get("web"), "web", 8080, warnings),
        mcp=_parse_listen(raw.get("mcp"), "mcp", 8765, warnings),
        path=resolved,
        warnings=tuple(warnings),
    )

def catalog_path_for(config_path: str | os.PathLike[str] | None = None) -> Path:
    """The one rule for where the catalog lives. The service and seed both use it.

    It is the catalog path from the config file. With no config file, it is the default
    path under the running user's home folder.
    """
    chosen = config_path or os.environ.get("SCALECONNECT_CONFIG") or default_config_path()
    if Path(chosen).expanduser().exists():
        return Path(load_config(chosen).settings.catalog_path)
    return Path(Settings().catalog_path)


def restrict_unofficial(config: Config) -> Config:
    """Rule 3 names the local config. A config from any other path can reach only this PC.

    So an edited temp file passed with --config or SCALECONNECT_CONFIG cannot turn on a
    real scale. Tests that build a Config directly do not pass through here.
    """
    if config.path is None or config.path.resolve() == default_config_path().resolve():
        return config
    scales = []
    warnings = list(config.warnings)
    for scale in config.scales:
        enabled = scale.write_enabled or scale.read_enabled
        if enabled and scale.address.strip().casefold() not in _LOOPBACK:
            scale = replace(scale, write_enabled=False, read_enabled=False)
            warnings.append(f"Scale {scale.name}: turned off, because this config is not the local config file.")
        scales.append(scale)
    return replace(config, scales=tuple(scales), warnings=tuple(warnings))

def require_backup_settings(config: Config) -> None:
    """Rule 5 wants a ScaleLink database copy. The real config must say how, out loud."""
    if config.path is None or config.path.resolve() != default_config_path().resolve():
        return
    settings = config.settings
    enabled = any(s.write_enabled is True for s in config.scales)
    if enabled and settings.scalelink_db_path is None and settings.scalelink_db_copy != SCALELINK_NOT_AVAILABLE:
        raise ConfigError(
            f"A write enabled scale needs a backup plan. Set scalelink_db_path, or set scalelink_db_copy to {SCALELINK_NOT_AVAILABLE}."
        )
