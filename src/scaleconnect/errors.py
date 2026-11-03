"""Exceptions. Each has a stable .code and a plain .message. See docs/LIBRARY.md."""

from __future__ import annotations

from typing import Any, Sequence


class ScaleConnectError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


class ConfigError(ScaleConnectError):
    code = "config_error"
    exit_code = 8


class ConfigChanged(ScaleConnectError):
    """The config file now says something this running service did not load. Nothing was sent."""

    code = "config_changed"
    exit_code = 8


class ValidationFailed(ScaleConnectError):
    code = "validation_failed"
    exit_code = 3

    def __init__(self, message: str = "", errors: Sequence[Any] = ()) -> None:
        super().__init__(message)
        self.errors = list(errors)

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["errors"] = [e.to_dict() for e in self.errors]
        return out


class CatalogNotSeeded(ScaleConnectError):
    """The service never creates a catalog. It needs one that seed.py made."""

    code = "catalog_not_seeded"
    exit_code = 8


class ItemNotFound(ScaleConnectError):
    code = "item_not_found"
    exit_code = 4


class ChangeNotFound(ScaleConnectError):
    code = "change_not_found"
    exit_code = 4


class PreviewNotFound(ScaleConnectError):
    code = "preview_not_found"
    exit_code = 4


class PreviewExpired(ScaleConnectError):
    code = "preview_expired"
    exit_code = 5


class PreviewStale(ScaleConnectError):
    code = "preview_stale"
    exit_code = 5


class PreviewAlreadyApplied(ScaleConnectError):
    code = "preview_already_applied"
    exit_code = 5


class AcknowledgementRequired(ScaleConnectError):
    code = "acknowledgement_required"
    exit_code = 6


class ReloadDifferences(ScaleConnectError):
    """A reload would change what a scale last showed, or nobody knows what it holds. Nothing was sent."""

    code = "reload_differences"
    exit_code = 6

    def __init__(self, message: str = "", differences: Sequence[Any] = (), not_known: Sequence[int] = (), last_read_at: str | None = None) -> None:
        super().__init__(message)
        self.differences = list(differences)
        self.not_known = list(not_known)
        self.last_read_at = last_read_at

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["differences"] = [d.to_dict() for d in self.differences]
        out["not_known"] = list(self.not_known)
        out["last_read_at"] = self.last_read_at
        return out


class InterruptedChange(ScaleConnectError):
    """An earlier change on these items never finished. Someone must look first."""

    code = "interrupted_change"
    exit_code = 5


class ScaleMayBeFrozen(ScaleConnectError):
    """A scale this would reach gave no reply, and nothing has read it since. Checked under the apply lock,
    only when the caller asks for it, as the web page does. Nothing was read or sent."""

    code = "scale_may_be_frozen"
    exit_code = 5

    def __init__(self, message: str = "", scales: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.scales = list(scales)

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["scales"] = list(self.scales)
        return out


class LogFailed(ScaleConnectError):
    """The change log could not be written, so nothing was sent."""

    code = "log_failed"


class BackupFailed(ScaleConnectError):
    code = "backup_failed"
    exit_code = 9


class ApplyBusy(ScaleConnectError):
    code = "apply_busy"
    exit_code = 10


class NotWriteEnabled(ScaleConnectError):
    """Raised only by scale.py. The service turns it into skipped_not_write_enabled."""

    code = "not_write_enabled"


class NotReadEnabled(ScaleConnectError):
    """Raised only by scale.py. "Not write enabled" must not mean "safe to read"."""

    code = "not_read_enabled"


class OperationNotAvailable(ScaleConnectError):
    """Raised only by scale.py. The service turns it into not_available."""

    code = "not_available"


class ProtocolError(ScaleConnectError):
    """A message could not be built safely, so nothing was sent."""

    code = "protocol_error"
