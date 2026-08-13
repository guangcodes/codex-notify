"""Conservative notification-text redaction."""

from __future__ import annotations

import base64
import binascii
import json
import re


_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.IGNORECASE),
    re.compile(r"\b(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bwhsec_[A-Za-z0-9]{12,}\b", re.IGNORECASE),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*", re.IGNORECASE),
    re.compile(
        r"(?<!\w)--(?:api[-_]?key|access[-_]?key|token|password|passwd|secret|authorization)"
        r"(?:=(?=[^\s,;])|\s+(?!-))[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", re.IGNORECASE),
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@",
    re.IGNORECASE,
)
_JWT_HEADER_START_PATTERN = re.compile(r"e[wy]")
_BASE64URL_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_BASE64URL_SIGNATURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,}+")
_MAX_JWT_HEADER_SEGMENT_LENGTH = 4096
_MAX_JWT_HEADER_CANDIDATES = 32

_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:\\?[\"'])?"
    r"([A-Za-z0-9_-]++(?:\s++[A-Za-z0-9_-]++)?)"
    r"(?:\\?[\"'])?\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "accesskey",
    "token",
    "password",
    "passwd",
    "secret",
    "authorization",
)
_REDACTION_WARNING = "内容可能包含敏感信息，请回到 Codex 查看。"


def _contains_secret(value: str) -> bool:
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        return True
    if "://" in value and _CREDENTIAL_URL_PATTERN.search(value):
        return True
    if _contains_jwt(value):
        return True
    # The assignment pattern cannot match without a key/value separator. Avoid
    # retrying its leading key expression at every word boundary in long values
    # such as ``a-a-a-...``, which otherwise turns this scan quadratic.
    if ":" not in value and "=" not in value:
        return False
    for match in _ASSIGNMENT_PATTERN.finditer(value):
        normalized_key = re.sub(r"[\s_-]+", "", match.group(1)).lower()
        if any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS):
            return True
    return False


def _contains_jwt(value: str) -> bool:
    segments = value.split(".")
    for index in range(len(segments) - 2):
        payload = segments[index + 1]
        if (
            len(payload) < 3
            or not _BASE64URL_SEGMENT_PATTERN.fullmatch(payload)
            or not _BASE64URL_SIGNATURE_PATTERN.match(segments[index + 2])
        ):
            continue
        attempts = 0
        for match in _JWT_HEADER_START_PATTERN.finditer(segments[index]):
            if attempts >= _MAX_JWT_HEADER_CANDIDATES:
                return True
            attempts += 1
            header = segments[index][match.start():]
            if _is_jose_header(header):
                return True
    return False


def _is_jose_header(value: str) -> bool:
    if len(value) < 8:
        return False
    if not _BASE64URL_SEGMENT_PATTERN.fullmatch(value) or len(value) % 4 == 1:
        return False
    if len(value) > _MAX_JWT_HEADER_SEGMENT_LENGTH:
        return True
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        header = json.loads(decoded.decode("utf-8"))
    except RecursionError:
        return True
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(header, dict) and isinstance(header.get("alg"), str) and bool(
        header["alg"].strip()
    )


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def safe_summary(value: object, limit: int) -> str:
    """Return compact text suitable for a third-party notification channel."""
    if not isinstance(value, str):
        return ""
    if limit <= 0:
        return ""
    compact = re.sub(r"\s+", " ", value).strip()
    if not compact:
        return ""
    if _contains_secret(compact):
        return _truncate(_REDACTION_WARNING, limit)
    return _truncate(compact, limit)
