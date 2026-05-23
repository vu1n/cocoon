"""Structured errors matching the skill's failure-modes table.

Each error carries a stable `code` (class attribute) so MCP clients can
branch on it without parsing the message, plus an instance-level `detail`
dict for structured payload (e.g. the env var name for AuthMissing).
"""

from typing import Any


class CocoonError(Exception):
    code: str = "cocoon_error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class MaterializationFailed(CocoonError):
    code = "materialization_failed"


class AuthMissing(CocoonError):
    code = "auth_missing"


class SandboxUnavailable(CocoonError):
    code = "sandbox_unavailable"


class CapabilityNotFound(CocoonError):
    code = "capability_not_found"


class CatalogUnavailable(CocoonError):
    code = "catalog_unavailable"
