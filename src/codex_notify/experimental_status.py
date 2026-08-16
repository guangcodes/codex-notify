"""Fail-silent experimental reads of minimal global Codex status."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .app_server_metadata import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDOUT_BYTES,
    _LineReader,
    _terminate,
    find_bundled_codex,
)
from .paths import AppPaths
from .redact import safe_summary


FEATURE_REQUEST_USER_INPUT = "request-user-input"
FEATURE_MCP_AUTH = "mcp-auth"
FEATURE_RATE_LIMITS = "rate-limits"
EXPERIMENTAL_FEATURES = (
    FEATURE_REQUEST_USER_INPUT,
    FEATURE_MCP_AUTH,
    FEATURE_RATE_LIMITS,
)
QUERY_FEATURES = frozenset({FEATURE_MCP_AUTH, FEATURE_RATE_LIMITS})

_AUTH_STATUSES = {"unknown", "unsupported", "notLoggedIn", "bearerToken", "oAuth"}
_RATE_LIMIT_REACHED_TYPES = {
    "rate_limit_reached",
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
}
_MCP_RESULT_FIELDS = {"data", "nextCursor"}
_MCP_SERVER_FIELDS = {
    "authStatus",
    "name",
    "pluginId",
    "resourceTemplates",
    "resources",
    "serverInfo",
    "tools",
}
_RATE_RESULT_FIELDS = {"rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"}
_RATE_SNAPSHOT_FIELDS = {
    "credits",
    "individualLimit",
    "limitId",
    "limitName",
    "planType",
    "primary",
    "rateLimitReachedType",
    "secondary",
    "spendControlReached",
}
_RATE_WINDOW_FIELDS = {"resetsAt", "usedPercent", "windowDurationMins"}
_MAX_MCP_SERVERS = 200
_MAX_RATE_BUCKETS = 100


@dataclass(frozen=True)
class ExperimentalCapability:
    available: bool
    reason: str


@dataclass(frozen=True)
class McpAuthObservation:
    signal_key: str
    display_name: str
    auth_status: str

    @classmethod
    def from_name(cls, name: str, auth_status: str) -> "McpAuthObservation":
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        return cls(digest, safe_summary(name, 80), auth_status)


@dataclass(frozen=True)
class RateLimitObservation:
    signal_key: str
    reached_type: str | None
    cooldown_key: str

    @classmethod
    def normal(cls, bucket: str) -> "RateLimitObservation":
        return cls(_safe_key(bucket), None, _cooldown(bucket, None, None, None))


@dataclass(frozen=True)
class ExperimentalSnapshot:
    mcp_auth: tuple[McpAuthObservation, ...] | None = None
    rate_limits: tuple[RateLimitObservation, ...] | None = None


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cooldown(
    bucket: str,
    reached_type: str | None,
    primary_reset: int | None,
    secondary_reset: int | None,
) -> str:
    canonical = json.dumps(
        [bucket, reached_type, primary_reset, secondary_reset],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contains_enum(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        enum = value.get("enum")
        if isinstance(enum, list) and expected in enum:
            return True
        return any(_contains_enum(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_enum(item, expected) for item in value)
    return False


def _contains_object_property(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict) and expected in properties:
            return True
        return any(
            _contains_object_property(item, expected) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_object_property(item, expected) for item in value)
    return False


def _read_schema_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _unavailable_capabilities() -> dict[str, ExperimentalCapability]:
    return {
        feature: ExperimentalCapability(False, "bundled Codex schema 不可用或不兼容")
        for feature in EXPERIMENTAL_FEATURES
    }


def read_experimental_capabilities(
    output_directory: Path,
) -> dict[str, ExperimentalCapability]:
    """Read capability flags from an already generated schema directory."""
    unavailable = _unavailable_capabilities()
    root = output_directory / "v2"
    hook_schema = _read_schema_object(root / "ConfigRequirementsReadResponse.json")
    request_schema = _read_schema_object(output_directory / "ToolRequestUserInputParams.json")
    mcp_params = _read_schema_object(root / "ListMcpServerStatusParams.json")
    mcp_response = _read_schema_object(root / "ListMcpServerStatusResponse.json")
    rate_response = _read_schema_object(root / "GetAccountRateLimitsResponse.json")
    request_available = (
        hook_schema is not None
        and request_schema is not None
        and _contains_object_property(hook_schema, "PreToolUse")
        and request_schema.get("title") == "ToolRequestUserInputParams"
    )
    mcp_available = (
        mcp_params is not None
        and mcp_response is not None
        and _contains_enum(mcp_params, "toolsAndAuthOnly")
        and _contains_enum(mcp_response, "notLoggedIn")
    )
    rate_available = rate_response is not None and all(
        _contains_enum(rate_response, value) for value in _RATE_LIMIT_REACHED_TYPES
    )
    return {
        FEATURE_REQUEST_USER_INPUT: ExperimentalCapability(
            request_available,
            "精确 PreToolUse matcher 与 request_user_input schema 可用"
            if request_available
            else unavailable[FEATURE_REQUEST_USER_INPUT].reason,
        ),
        FEATURE_MCP_AUTH: ExperimentalCapability(
            mcp_available,
            "mcpServerStatus/list toolsAndAuthOnly 与 notLoggedIn schema 可用"
            if mcp_available
            else unavailable[FEATURE_MCP_AUTH].reason,
        ),
        FEATURE_RATE_LIMITS: ExperimentalCapability(
            rate_available,
            "account/rateLimits/read 与 reached 类型 schema 可用"
            if rate_available
            else unavailable[FEATURE_RATE_LIMITS].reason,
        ),
    }


def probe_experimental_capabilities(
    binary: Path | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
    output_directory: Path | None = None,
) -> dict[str, ExperimentalCapability]:
    unavailable = _unavailable_capabilities()
    if binary is None:
        return unavailable
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if output_directory is None:
            temporary = tempfile.TemporaryDirectory(prefix="codex-notify-experimental-schema-")
            output_directory = Path(temporary.name)
        result = run(
            [
                str(binary),
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(output_directory),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if result.returncode != 0:
            return unavailable
        return read_experimental_capabilities(output_directory)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, UnicodeError):
        return unavailable
    finally:
        if temporary is not None:
            temporary.cleanup()


def _identifier(value: object, *, maximum: int = 256) -> str | None:
    return (
        value
        if isinstance(value, str) and value and "\0" not in value and len(value) <= maximum
        else None
    )


def _parse_mcp_page(value: object) -> tuple[list[McpAuthObservation], str | None] | None:
    if not isinstance(value, dict) or not set(value).issubset(_MCP_RESULT_FIELDS):
        return None
    data = value.get("data")
    if not isinstance(data, list) or len(data) > 50:
        return None
    observations: list[McpAuthObservation] = []
    for raw in data:
        if (
            not isinstance(raw, dict)
            or not {"name", "authStatus", "tools", "resources", "resourceTemplates"}.issubset(raw)
            or not set(raw).issubset(_MCP_SERVER_FIELDS)
        ):
            return None
        name = _identifier(raw.get("name"))
        auth_status = raw.get("authStatus")
        if (
            name is None
            or auth_status not in _AUTH_STATUSES
            or not isinstance(raw.get("tools"), dict)
            or raw.get("resources") != []
            or raw.get("resourceTemplates") != []
        ):
            return None
        observations.append(McpAuthObservation.from_name(name, auth_status))
    next_cursor = value.get("nextCursor")
    if next_cursor is not None and _identifier(next_cursor, maximum=4096) is None:
        return None
    return observations, next_cursor


def _parse_window(value: object) -> int | None | object:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value).issubset(_RATE_WINDOW_FIELDS):
        return _INVALID
    used = value.get("usedPercent")
    reset = value.get("resetsAt")
    duration = value.get("windowDurationMins")
    if (
        isinstance(used, bool)
        or not isinstance(used, (int, float))
        or (isinstance(used, float) and not math.isfinite(used))
        or reset is not None
        and (isinstance(reset, bool) or not isinstance(reset, int) or reset < 0)
        or duration is not None
        and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0)
    ):
        return _INVALID
    return reset


_INVALID = object()


def _parse_rate_snapshot(value: object, bucket: str) -> RateLimitObservation | None:
    if not isinstance(value, dict) or not set(value).issubset(_RATE_SNAPSHOT_FIELDS):
        return None
    limit_id = value.get("limitId")
    if limit_id is not None:
        if _identifier(limit_id) is None or bucket != "default" and limit_id != bucket:
            return None
    reached = value.get("rateLimitReachedType")
    if reached is not None and reached not in _RATE_LIMIT_REACHED_TYPES:
        return None
    primary_reset = _parse_window(value.get("primary"))
    secondary_reset = _parse_window(value.get("secondary"))
    if primary_reset is _INVALID or secondary_reset is _INVALID:
        return None
    identity = bucket if bucket != "default" else limit_id or "default"
    return RateLimitObservation(
        _safe_key(identity),
        reached,
        _cooldown(identity, reached, primary_reset, secondary_reset),
    )


def _parse_rate_result(value: object) -> tuple[RateLimitObservation, ...] | None:
    if (
        not isinstance(value, dict)
        or "rateLimits" not in value
        or not set(value).issubset(_RATE_RESULT_FIELDS)
    ):
        return None
    by_id = value.get("rateLimitsByLimitId")
    raw_items: list[tuple[str, object]]
    if by_id is not None:
        if not isinstance(by_id, dict) or len(by_id) > _MAX_RATE_BUCKETS:
            return None
        raw_items = (
            list(by_id.items())
            if by_id
            else [("default", value.get("rateLimits"))]
        )
    else:
        raw_items = [("default", value.get("rateLimits"))]
    observations: list[RateLimitObservation] = []
    for bucket, raw in raw_items:
        if _identifier(bucket) is None:
            return None
        observation = _parse_rate_snapshot(raw, bucket)
        if observation is None:
            return None
        observations.append(observation)
    return tuple(observations)


class ExperimentalStatusReader:
    def __init__(
        self,
        binary: Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._popen = popen

    def read(self, features: set[str]) -> ExperimentalSnapshot:
        selected = set(features) & QUERY_FEATURES
        if not selected:
            return ExperimentalSnapshot()
        process = None
        try:
            process = self._popen(
                [str(self.binary), "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if process.stdin is None or process.stdout is None:
                return ExperimentalSnapshot()
            deadline = time.monotonic() + self.timeout_seconds
            lines = _LineReader(process.stdout)
            total_bytes = 0
            request_id = 1

            def send(method: str, params: dict[str, object] | None = None, *, notification: bool = False) -> None:
                payload: dict[str, object] = {"method": method}
                if params is not None:
                    payload["params"] = params
                if not notification:
                    payload["id"] = request_id
                process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                process.stdin.flush()

            def response(expected_id: int) -> dict[str, object] | None:
                nonlocal total_bytes
                while time.monotonic() < deadline:
                    line = lines.readline(deadline)
                    if line is None:
                        return None
                    total_bytes += len(line.encode("utf-8", errors="replace"))
                    if total_bytes > MAX_STDOUT_BYTES:
                        return None
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        return None
                    if not isinstance(message, dict) or message.get("id") != expected_id:
                        continue
                    result = message.get("result")
                    if "error" in message or not isinstance(result, dict):
                        return None
                    return result
                return None

            send(
                "initialize",
                {
                    "clientInfo": {"name": "codex-notify-experimental-status", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            if response(request_id) is None:
                return ExperimentalSnapshot()
            send("initialized", {}, notification=True)

            mcp_auth: tuple[McpAuthObservation, ...] | None = None
            if FEATURE_MCP_AUTH in selected:
                observations: list[McpAuthObservation] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                valid = True
                while valid:
                    request_id += 1
                    params: dict[str, object] = {"detail": "toolsAndAuthOnly", "limit": 50}
                    if cursor is not None:
                        params["cursor"] = cursor
                    send("mcpServerStatus/list", params)
                    result = response(request_id)
                    parsed = _parse_mcp_page(result)
                    if parsed is None:
                        valid = False
                        break
                    page, next_cursor = parsed
                    observations.extend(page)
                    if len(observations) > _MAX_MCP_SERVERS:
                        valid = False
                        break
                    if next_cursor is None:
                        break
                    if next_cursor in seen_cursors:
                        valid = False
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                if valid and len({item.signal_key for item in observations}) == len(observations):
                    mcp_auth = tuple(observations)

            rate_limits: tuple[RateLimitObservation, ...] | None = None
            if FEATURE_RATE_LIMITS in selected:
                request_id += 1
                send("account/rateLimits/read")
                rate_limits = _parse_rate_result(response(request_id))
            return ExperimentalSnapshot(mcp_auth, rate_limits)
        except Exception:
            return ExperimentalSnapshot()
        finally:
            if process is not None:
                _terminate(process)


def read_experimental_status(
    paths: AppPaths,
    features: set[str],
    *,
    binary: Path | None = None,
) -> ExperimentalSnapshot:
    binary = binary or find_bundled_codex()
    if binary is None:
        return ExperimentalSnapshot()
    paths.ensure_runtime_dirs()
    lock_path = paths.data_dir / "app-server-probe.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ExperimentalSnapshot()
        return ExperimentalStatusReader(binary).read(features)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
