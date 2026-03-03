"""
Pydantic schemas for telemetry endpoint — K: Telemetry event taxonomy.

Defines:
- TelemetryEventType enum with all valid event names
- Per-event payload schemas (TypedDict-style, validated via Pydantic)
- Privacy mode — strips PII from properties before storage
- TelemetryEventRequest — inbound request model with event validation
- TelemetryEventResponse — outbound confirmation model
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------

class TelemetryEventType(str, Enum):
    """Canonical telemetry event names emitted by the Pakalon CLI."""

    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_RESTORE = "session.restore"

    # Phase execution
    PHASE_START = "phase.start"
    PHASE_COMPLETE = "phase.complete"
    PHASE_ERROR = "phase.error"
    PHASE_RETRY = "phase.retry"
    PHASE_SKIP = "phase.skip"

    # AI / Model
    MODEL_CALL = "model.call"
    MODEL_CALL_ERROR = "model.call_error"
    MODEL_TOKEN_USAGE = "model.token_usage"
    MODEL_CACHE_HIT = "model.cache_hit"

    # Tool use
    TOOL_USE = "tool.use"
    TOOL_ERROR = "tool.error"
    TOOL_PERMISSION_REQUESTED = "tool.permission_requested"
    TOOL_PERMISSION_GRANTED = "tool.permission_granted"
    TOOL_PERMISSION_DENIED = "tool.permission_denied"

    # MCP
    MCP_SERVER_ADDED = "mcp.server_added"
    MCP_SERVER_REMOVED = "mcp.server_removed"
    MCP_SERVER_ENABLED = "mcp.server_enabled"
    MCP_SERVER_DISABLED = "mcp.server_disabled"
    MCP_TOOL_INVOKED = "mcp.tool_invoked"
    MCP_TOOL_ERROR = "mcp.tool_error"

    # Hook system
    HOOK_TRIGGERED = "hook.triggered"
    HOOK_BLOCKED = "hook.blocked"
    HOOK_ERROR = "hook.error"

    # Credits
    CREDITS_DEBITED = "credits.debited"
    CREDITS_EXHAUSTED = "credits.exhausted"
    CREDITS_RESET = "credits.reset"

    # Billing / Subscription
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"
    SUBSCRIPTION_UPGRADED = "subscription.upgraded"

    # CLI commands
    COMMAND_EXECUTED = "command.executed"
    COMMAND_ERROR = "command.error"

    # Security (phase 4)
    SECURITY_SCAN_STARTED = "security.scan_started"
    SECURITY_SCAN_COMPLETE = "security.scan_complete"
    SECURITY_FINDING_HIGH = "security.finding_high"
    SECURITY_FINDING_CRITICAL = "security.finding_critical"

    # Update flow
    UPDATE_STARTED = "update.started"
    UPDATE_COMPLETE = "update.complete"
    UPDATE_ROLLBACK = "update.rollback"
    UPDATE_FAILED = "update.failed"

    # Plugin / Skills
    PLUGIN_INSTALLED = "plugin.installed"
    PLUGIN_UPDATED = "plugin.updated"
    PLUGIN_REMOVED = "plugin.removed"
    PLUGIN_ERROR = "plugin.error"

    # Errors & diagnostics
    CRASH = "crash"
    UNHANDLED_ERROR = "error.unhandled"
    PERFORMANCE_SLOW = "performance.slow"

    # ---------------------------------------------------------------------------
    # T-BE-27: Web analytics events (marketing site + web dashboard)
    # ---------------------------------------------------------------------------
    PAGE_VIEW = "page_view"
    SESSION_START_WEB = "session_start"
    FEATURE_USAGE = "feature_usage"
    CTA_CLICK = "cta_click"
    UPGRADE_CLICK = "upgrade_click"


# ---------------------------------------------------------------------------
# PII fields to strip in privacy mode
# ---------------------------------------------------------------------------

_PII_KEYS: frozenset[str] = frozenset({
    "email", "name", "full_name", "first_name", "last_name",
    "github_login", "github_username", "username",
    "api_key", "token", "secret", "password", "credential",
    "ip", "ip_address", "client_ip",
    "file_path", "project_path", "project_dir", "cwd",
    "prompt", "user_prompt", "message", "content",
})

_PII_PATTERN = re.compile(
    r"(email|password|token|secret|api.?key|credential|github.?login|file.?path|project.?dir|prompt|message|cwd)",
    re.IGNORECASE,
)


def _strip_pii(properties: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *properties* with PII fields replaced by '[redacted]'."""
    result: dict[str, Any] = {}
    for k, v in properties.items():
        if k.lower() in _PII_KEYS or _PII_PATTERN.search(k):
            result[k] = "[redacted]"
        elif isinstance(v, dict):
            result[k] = _strip_pii(v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Per-event required/optional property schemas
# ---------------------------------------------------------------------------

# Lightweight spec — the keys each event SHOULD have (not strictly enforced,
# but used to annotate/complete sparse payloads at ingestion time).
EVENT_SCHEMA_HINTS: dict[str, list[str]] = {
    TelemetryEventType.PHASE_START: ["phase_number", "phase_name"],
    TelemetryEventType.PHASE_COMPLETE: ["phase_number", "phase_name", "duration_ms"],
    TelemetryEventType.PHASE_ERROR: ["phase_number", "phase_name", "error_type", "error_message"],
    TelemetryEventType.PHASE_RETRY: ["phase_number", "retry_count", "reason"],
    TelemetryEventType.MODEL_CALL: ["model", "provider"],
    TelemetryEventType.MODEL_TOKEN_USAGE: ["model", "tokens_in", "tokens_out"],
    TelemetryEventType.MODEL_CALL_ERROR: ["model", "error_type"],
    TelemetryEventType.TOOL_USE: ["tool_name", "tool_type"],
    TelemetryEventType.TOOL_ERROR: ["tool_name", "error_type"],
    TelemetryEventType.TOOL_PERMISSION_REQUESTED: ["tool_name", "risk_level"],
    TelemetryEventType.TOOL_PERMISSION_GRANTED: ["tool_name", "decision_mode"],
    TelemetryEventType.TOOL_PERMISSION_DENIED: ["tool_name"],
    TelemetryEventType.MCP_SERVER_ADDED: ["server_name", "scope"],
    TelemetryEventType.MCP_SERVER_REMOVED: ["server_name", "scope"],
    TelemetryEventType.MCP_TOOL_INVOKED: ["server_name", "tool_name"],
    TelemetryEventType.HOOK_TRIGGERED: ["event_type", "command"],
    TelemetryEventType.HOOK_BLOCKED: ["event_type", "command", "reason"],
    TelemetryEventType.CREDITS_DEBITED: ["amount", "remaining"],
    TelemetryEventType.CREDITS_EXHAUSTED: ["plan"],
    TelemetryEventType.COMMAND_EXECUTED: ["command"],
    TelemetryEventType.SECURITY_SCAN_COMPLETE: ["tool", "findings_count", "passed"],
    TelemetryEventType.SECURITY_FINDING_HIGH: ["tool", "finding_type"],
    TelemetryEventType.SECURITY_FINDING_CRITICAL: ["tool", "finding_type"],
    TelemetryEventType.UPDATE_STARTED: ["from_version", "to_version"],
    TelemetryEventType.UPDATE_COMPLETE: ["version"],
    TelemetryEventType.UPDATE_ROLLBACK: ["from_version", "reason"],
    TelemetryEventType.PLUGIN_INSTALLED: ["plugin_name", "plugin_version"],
    TelemetryEventType.CRASH: ["error_type", "stack_trace_hash"],
    TelemetryEventType.PERFORMANCE_SLOW: ["operation", "duration_ms"],
}

# Collect valid event names as a flat set for O(1) lookup
_VALID_EVENT_NAMES: frozenset[str] = frozenset(e.value for e in TelemetryEventType)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TelemetryEventRequest(BaseModel):
    """Inbound telemetry event from CLI client."""

    event_name: str = Field(..., max_length=255, description="Event type from TelemetryEventType enum")
    properties: Optional[dict[str, Any]] = Field(None, description="Event payload — PII is stripped in privacy mode")
    cli_version: Optional[str] = Field(None, max_length=32)
    os_name: Optional[str] = Field(None, max_length=64)
    privacy_mode: Optional[bool] = Field(False, description="When true, strip PII from properties before storage")

    @field_validator("event_name")
    @classmethod
    def validate_event_name(cls, v: str) -> str:
        """Accept both canonical enum values and free-form names (with warning)."""
        # Normalise: strip whitespace, lowercase
        normalised = v.strip().lower()
        # Accept exact matches from taxonomy
        if normalised in _VALID_EVENT_NAMES:
            return normalised
        # Accept dot-separated names up to 255 chars (custom events allowed)
        if len(normalised) <= 255 and re.match(r"^[a-z0-9._-]+$", normalised):
            # Custom/unknown — allowed but flagged by presence outside taxonomy
            return normalised
        raise ValueError(
            f"Invalid event_name '{v}'. Use a value from TelemetryEventType or a dot-separated lowercase string."
        )

    @model_validator(mode="after")
    def apply_privacy_mode(self) -> "TelemetryEventRequest":
        """Strip PII from properties when privacy_mode is True."""
        if self.privacy_mode and self.properties:
            self.properties = _strip_pii(self.properties)
        return self

    def sanitized_properties(self) -> dict[str, Any]:
        """Return properties safe for storage — always strip the most sensitive keys."""
        props = dict(self.properties or {})
        # Always redact unconditionally sensitive keys regardless of privacy_mode
        for key in ("api_key", "token", "password", "secret", "credential"):
            if key in props:
                props[key] = "[redacted]"
        return props

    def is_known_event(self) -> bool:
        """Return True if the event name is in the official taxonomy."""
        return self.event_name in _VALID_EVENT_NAMES


class TelemetryEventResponse(BaseModel):
    id: str
    recorded: bool = True
    known_event: bool = True
    """False when event_name is outside the canonical taxonomy."""
