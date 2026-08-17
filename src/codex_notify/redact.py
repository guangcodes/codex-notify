"""Conservative, context-preserving notification-text redaction."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import re


_SECRET_MASK = "[敏感信息已打码]"
_LOCAL_PATH_MASK = "[本地路径已打码]"
_MIN_SCAN_CHARS = 16_384
_MAX_SCAN_CHARS = 65_536
_SCAN_MULTIPLIER = 4

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
        r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+",
        re.IGNORECASE,
    ),
)
_CLI_SECRET_PREFIX_PATTERN = re.compile(
    r"(?<!\w)--(?:api[-_]?key|access[-_]?key|token|password|passwd|secret|authorization)"
    r"(?P<separator>=(?=[^\s,;])|\s+(?!-))",
    re.IGNORECASE,
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^\s/:?#]*:[^\s/?#]+)@",
    re.IGNORECASE,
)
_PRIVATE_KEY_BEGIN_PATTERN = re.compile(
    r"-----BEGIN (?P<label>[A-Z ]*PRIVATE KEY(?: BLOCK)?)-----",
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
_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])-?(?:\\?[\"'])?"
    r"(?P<key>[A-Za-z0-9_][A-Za-z0-9_-]*+(?:\s++[A-Za-z0-9_-]++)?)"
    r"(?:\\?[\"'])?\s*[:=]\s*+)(?!\\?[\"'])",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])-?(?:\\?[\"'])?"
    r"(?P<key>[A-Za-z0-9_][A-Za-z0-9_-]*+(?:\s++[A-Za-z0-9_-]++)?)"
    r"(?:\\?[\"'])?\s*[:=]\s*)(?P<quote>\\?[\"'])",
    re.IGNORECASE,
)
_ESCAPED_NEXT_PROPERTY_PATTERN = re.compile(
    r",\s*\\?[\"'][^\"']{1,128}\\?[\"']\s*:"
)

_JWT_HEADER_START_PATTERN = re.compile(r"e[wy]")
_BASE64URL_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_BASE64URL_SIGNATURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,}\Z")
_DOTTED_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_MAX_JWT_HEADER_SEGMENT_LENGTH = 4096
_MAX_JWT_HEADER_CANDIDATES = 32

_URI_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s`'\"<>{}，。；：、！？（）【】《》「」『』—]+",
    re.IGNORECASE,
)
_LOCAL_PATH_URI_SCHEME_NAMES = (
    "cursor",
    "cursor-nightly",
    "file",
    "sqlite",
    "vscode",
    "vscode-insiders",
    "windsurf",
    "zed",
)
_LOCAL_PATH_URI_SCHEME_PATTERN = "(?:" + "|".join(
    re.escape(name) for name in _LOCAL_PATH_URI_SCHEME_NAMES
) + ")"
_LOCAL_PATH_URI_START_PATTERN = re.compile(
    rf"\b{_LOCAL_PATH_URI_SCHEME_PATTERN}://", re.IGNORECASE
)
_URL_PARAMETER_PATTERN = re.compile(
    r"(?P<prefix>[?&#](?P<key>[^\s=&#]+)=)(?P<value>[^\s&#]*)",
    re.IGNORECASE,
)
_LOCAL_VALUE_START_PATTERN = re.compile(
    r"(?:file(?:://|%3a%2f%2f)|/|%2f|[A-Za-z](?::[/\\]|%3a%(?:2f|5c))|\\\\|%5c%5c)",
    re.IGNORECASE,
)
_UNAMBIGUOUS_LOCAL_VALUE_START_PATTERN = re.compile(
    r"(?:file(?:://|%3a%2f%2f)|[A-Za-z](?::[/\\]|%3a%(?:2f|5c))|\\\\|%5c%5c)",
    re.IGNORECASE,
)
_EMBEDDED_LOCAL_VALUE_START_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9_])file(?:://|%3a%2f%2f)|"
    r"(?<![A-Za-z0-9_])[A-Za-z](?::[/\\]|%3a%(?:2f|5c))|"
    r"\\\\|%5c%5c)",
    re.IGNORECASE,
)
_STANDALONE_ENCODED_LOCAL_PATH_START_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_%])(?:"
    r"file%3a%2f%2f(?:%2f)?|"
    r"[A-Za-z]%3a%(?:2f|5c)|"
    r"%5c%5c|"
    r"%2f(?:Users|Applications|Library|Volumes|private|tmp|var|etc|opt|home|srv|root)"
    r"(?=%2f|$))",
    re.IGNORECASE,
)
_KNOWN_LOCAL_POSIX_VALUE_START_PATTERN = re.compile(
    r"(?:/(?:Users|Applications|Library|Volumes|private|tmp|var|etc|opt|home|srv|root)"
    r"(?:/|$)|%2f(?:Users|Applications|Library|Volumes|private|tmp|var|etc|opt|home|srv|root)"
    r"(?:%2f|$))",
    re.IGNORECASE,
)
_LOCAL_PATH_PARAMETER_NAMES = frozenset(
    {"file", "path", "cwd", "dir", "directory", "output", "root", "workspace", "worktree"}
)
_LOCAL_PATH_URI_SCHEMES = frozenset(_LOCAL_PATH_URI_SCHEME_NAMES)
_WEB_URI_SCHEMES = frozenset({"http", "https"})
_ADJACENT_LOCAL_PATH_PATTERN = re.compile(
    r"(?:/(?!/)|[A-Za-z]:[/\\]|\\\\(?![\\\"']))"
)

_LOCAL_PATH_START_PATTERN = re.compile(
    r"(?P<drive>(?<![A-Za-z0-9_])[A-Za-z]:[/\\])|"
    r"(?P<unc>(?<![A-Za-z0-9_\\])\\\\(?![\\\"']))|"
    r"(?P<posix>(?<![A-Za-z0-9_/])/(?![/\s]))"
)
_PATH_SCAN_LIMIT = 2048
_PATH_HARD_BOUNDARIES = frozenset("，。；：、！？】—")
_PATH_ASCII_PROSE_BOUNDARIES = frozenset(".:!?")
_RELIABLE_FILE_EXTENSIONS = frozenset(
    {
        "bash", "cfg", "conf", "css", "csv", "db", "doc", "docx", "gif",
        "gz", "htm", "html", "ini", "jpeg", "jpg", "js", "json", "jsx",
        "lock", "log", "md", "pdf", "png", "ppt", "pptx", "py", "sh",
        "sql", "sqlite", "svg", "tar", "toml", "ts", "tsx", "txt", "webp",
        "xls", "xlsx", "xml", "yaml", "yml", "zip", "zsh",
    }
)
_FILE_EXTENSION_PATTERN = re.compile(r"\.([A-Za-z0-9]{1,16})\Z")
_LATER_FILE_EXTENSION_PATTERN = re.compile(
    r"\.([A-Za-z0-9]{1,16})(?=$|\s|[,;，。；：、！？】—\"'`)\]}])"
)
_CJK_RELATIVE_PATH_ROOTS = (
    "文档", "项目", "源码", "测试", "示例", "资源", "配置",
)
_CODEX_SLASH_COMMANDS = frozenset(
    {
        "apps", "approvals", "compact", "diff", "experimental", "feedback",
        "fork", "goal", "help", "hooks", "init", "logout", "mcp", "mention",
        "model", "new", "permissions", "plan", "ps", "quit", "resume", "review",
        "skills", "status",
    }
)
_SLASH_COMMAND_PATTERN = re.compile(
    r"/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?=$|\s|[`'\",;:!?，。；：！？）】]|\.(?![A-Za-z0-9]))"
)
_SLASH_COMMAND_CUE_PATTERN = re.compile(
    r"(?:\b(?:run|use|type|execute|invoke)"
    r"(?:\s+(?:(?:the|a)\s+)?(?:codex\s+)?command)?\s+|"
    r"(?:运行|执行|使用|输入|调用)\s*"
    r"(?:(?:Codex\s*(?:的|命令)?)|命令)?\s*)"
    r"[`'\"“‘]*$",
    re.IGNORECASE,
)
_SLASH_COMMAND_PREFIX_LIMIT = 40
_HTTP_ROUTE_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+"
    r"(?P<route>/(?!/)[^\s`'\"，。；：、！？】—,;]+)"
)


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    kind: str


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[\s_-]+", "", key).lower()
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _trim_path_end(value: str, start: int, end: int) -> int:
    while end > start and value[end - 1] in ")]\"'`":
        end -= 1
    return end


def _has_reliable_terminal_extension(value: str, start: int, end: int) -> bool:
    match = _FILE_EXTENSION_PATTERN.search(value[start:end].rstrip())
    return match is not None and match.group(1).lower() in _RELIABLE_FILE_EXTENSIONS


def _has_later_reliable_extension(value: str, start: int, end: int) -> bool:
    return any(
        match.group(1).lower() in _RELIABLE_FILE_EXTENSIONS
        for match in _LATER_FILE_EXTENSION_PATTERN.finditer(value, start, end)
    )


def _next_component_has_path_separator(
    value: str,
    start: int,
    end: int,
    *,
    cjk_punctuation_is_content: bool = False,
) -> bool:
    """Return whether the next non-space component still has path syntax."""
    cursor = start
    while cursor < end:
        character = value[cursor]
        if character.isspace() or (
            character in _PATH_HARD_BOUNDARIES
            and not cjk_punctuation_is_content
        ):
            return False
        if character in ",;\"'`)]}":
            return False
        if character in "/\\":
            return True
        cursor += 1
    return False


def _remainder_of_component_has_reliable_extension(
    value: str, start: int, end: int
) -> bool:
    component_end = start
    while component_end < end and not value[component_end].isspace():
        component_end += 1
    return _has_later_reliable_extension(value, start, component_end)


def _path_continues_after_boundary(value: str, start: int, end: int) -> bool:
    return _next_component_has_path_separator(
        value, start, end
    ) or _remainder_of_component_has_reliable_extension(value, start, end)


def _ambiguous_boundary_is_path_content(
    value: str,
    component_start: int,
    component_has_whitespace: bool,
    boundary: int,
    end: int,
) -> bool:
    if _path_continues_after_boundary(value, boundary + 1, end):
        return True
    if boundary + 1 >= end or value[boundary + 1].isspace():
        return False
    if component_has_whitespace or _has_reliable_terminal_extension(
        value, component_start, boundary
    ):
        return False
    # Extensionless terminal components have no reliable lexical end. When
    # punctuation is followed immediately by more text, fail closed and keep
    # that suffix inside the path span.
    return True


def _scan_local_path_end(value: str, start: int, ceiling: int | None = None) -> int:
    """Scan one absolute local path from a trusted start, with bounded work.

    The scanner owns lexical boundaries for POSIX, drive, UNC, and local-URI
    paths. Regexes only identify starts; they do not compete to choose an end.
    """
    end_limit = min(len(value), start + _PATH_SCAN_LIMIT)
    if ceiling is not None:
        end_limit = min(end_limit, ceiling)
    cursor = start
    component_start = start
    component_has_whitespace = False
    bracket_stack: list[str] = []
    matching = {"(": ")", "[": "]", "{": "}"}
    external_closer = matching.get(value[start - 1]) if start else None
    windows_style = (
        re.match(r"[A-Za-z]:[/\\]", value[start:]) is not None
        or value.startswith("\\\\", start)
    )

    while cursor < end_limit:
        character = value[cursor]
        if character == "\\" and not windows_style and cursor + 1 < end_limit:
            if value[cursor + 1].isspace():
                component_has_whitespace = True
            cursor += 2
            continue
        if character in "\"'`":
            if _ambiguous_boundary_is_path_content(
                value,
                component_start,
                component_has_whitespace,
                cursor,
                end_limit,
            ):
                cursor += 1
                continue
            break
        if character in matching:
            bracket_stack.append(matching[character])
            cursor += 1
            continue
        if character in ")]}":
            if bracket_stack and character == bracket_stack[-1]:
                bracket_stack.pop()
                cursor += 1
                continue
            if character == external_closer or not bracket_stack:
                if _ambiguous_boundary_is_path_content(
                    value,
                    component_start,
                    component_has_whitespace,
                    cursor,
                    end_limit,
                ):
                    cursor += 1
                    continue
                break
        if character in _PATH_HARD_BOUNDARIES:
            if not _ambiguous_boundary_is_path_content(
                value,
                component_start,
                component_has_whitespace,
                cursor,
                end_limit,
            ):
                break
            cursor += 1
            continue
        if (
            character in _PATH_ASCII_PROSE_BOUNDARIES
            and cursor + 1 < end_limit
            and value[cursor + 1].isspace()
        ):
            next_start = cursor + 1
            while next_start < end_limit and value[next_start].isspace():
                next_start += 1
            if not _next_component_has_path_separator(
                value, next_start, end_limit
            ):
                break
        if character in ",;":
            # No whitespace means the punctuation is lexically inside the
            # component (for example Client,Secret or foo;bar.txt). Punctuation
            # followed by whitespace is a prose boundary only when the next
            # component has no remaining path structure.
            if cursor + 1 >= end_limit or value[cursor + 1].isspace():
                next_start = cursor + 1
                while next_start < end_limit and value[next_start].isspace():
                    next_start += 1
                if not _next_component_has_path_separator(
                    value, next_start, end_limit
                ):
                    break
            cursor += 1
            continue
        if character.isspace():
            next_start = cursor
            while next_start < end_limit and value[next_start].isspace():
                next_start += 1
            if next_start >= end_limit:
                break
            if (
                _has_reliable_terminal_extension(value, start, cursor)
                and not _has_later_reliable_extension(
                    value, next_start, end_limit
                )
                and not _next_component_has_path_separator(
                    value, next_start, end_limit
                )
            ):
                break
            component_has_whitespace = True
            cursor = next_start
            continue
        if character == "/" or (windows_style and character == "\\"):
            component_start = cursor + 1
            component_has_whitespace = False
        cursor += 1

    if cursor >= end_limit and end_limit < (ceiling if ceiling is not None else len(value)):
        # The lexical end is unknown beyond the bounded scan. Mask through the
        # containing structure (or the summary) instead of exposing a suffix.
        return ceiling if ceiling is not None else len(value)
    while cursor > start and value[cursor - 1].isspace():
        cursor -= 1
    return cursor


def _posix_start_has_explicit_relative_context(value: str, start: int) -> bool:
    if start and value[start - 1] == ".":
        relative_start = start - 1
        if relative_start and value[relative_start - 1] == ".":
            relative_start -= 1
        if relative_start == 0 or value[relative_start - 1] not in (
            "_-." + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        ):
            return True
    if start == 0 or not ("\u3400" <= value[start - 1] <= "\u9fff"):
        return False
    if _KNOWN_LOCAL_POSIX_VALUE_START_PATTERN.match(value, start) is not None:
        return False
    for root in _CJK_RELATIVE_PATH_ROOTS:
        root_start = start - len(root)
        if (
            root_start >= 0
            and value.startswith(root, root_start)
            and (
                root_start == 0
                or not ("\u3400" <= value[root_start - 1] <= "\u9fff")
            )
        ):
            return True
    return False


def _secret_spans(value: str) -> list[_Span]:
    spans = _private_key_spans(value)
    for pattern in _SECRET_PATTERNS:
        spans.extend(_Span(match.start(), match.end(), "secret") for match in pattern.finditer(value))
    for match in _CLI_SECRET_PREFIX_PATTERN.finditer(value):
        spans.append(_value_span_after_prefix(value, match.end()))
    credential_spans: list[_Span] = []
    for match in _CREDENTIAL_URL_PATTERN.finditer(value):
        credential_spans.append(_Span(*match.span("userinfo"), "secret"))
    spans.extend(credential_spans)

    if ":" in value or "=" in value:
        quoted_assignment_spans: list[_Span] = []
        for match in _QUOTED_ASSIGNMENT_PREFIX_PATTERN.finditer(value):
            if _sensitive_key(match.group("key")):
                quoted_assignment_spans.append(
                    _quoted_value_span(
                        value, match.end("quote"), match.group("quote")
                    )
                )
        spans.extend(quoted_assignment_spans)
        protected_secret_spans = tuple(spans)
        for match in _ASSIGNMENT_PREFIX_PATTERN.finditer(value):
            if _sensitive_key(match.group("key")) and not any(
                match.start() < span.end and match.end() > span.start
                for span in protected_secret_spans
            ):
                start, end = match.end(), len(value)
                # An unquoted value has no reliable lexical boundary: commas,
                # semicolons, and sentence punctuation can all be credential
                # characters. Fail closed through the remainder of the summary.
                while end > start and value[end - 1].isspace():
                    end -= 1
                if start < end:
                    spans.append(_Span(start, end, "secret"))

    spans.extend(_jwt_spans(value))
    return spans


def _private_key_spans(value: str) -> list[_Span]:
    spans: list[_Span] = []
    position = 0
    while begin := _PRIVATE_KEY_BEGIN_PATTERN.search(value, position):
        end_marker = re.compile(
            re.escape(f"-----END {begin.group('label')}-----"), re.IGNORECASE
        )
        end = end_marker.search(value, begin.end())
        span_end = end.end() if end else len(value)
        spans.append(_Span(begin.start(), span_end, "secret"))
        if not end:
            break
        position = span_end
    return spans


def _value_span_after_prefix(value: str, start: int) -> _Span:
    end = start
    quote = ""
    unclosed_quote_boundary: int | None = None
    while end < len(value):
        character = value[end]
        if character == "\\" and end + 1 < len(value):
            end += 2
            continue
        if quote:
            if character == quote:
                quote = ""
                unclosed_quote_boundary = None
            elif (
                unclosed_quote_boundary is None
                and character in "；。"
            ):
                unclosed_quote_boundary = end
            end += 1
            continue
        if character in "\"'":
            quote = character
            unclosed_quote_boundary = None
            end += 1
            continue
        if character.isspace() or character in ";|&，；。":
            break
        end += 1
    if quote and unclosed_quote_boundary is not None:
        end = unclosed_quote_boundary
    return _Span(start, end, "secret")


def _quoted_fragment_end(value: str, start: int) -> int:
    escaped_delimiter = (
        value[start] == "\\"
        and start + 1 < len(value)
        and value[start + 1] in "\"'"
    )
    quote_at = start + 1 if escaped_delimiter else start
    if quote_at >= len(value) or value[quote_at] not in "\"'":
        return start
    quote = value[quote_at]
    cursor = quote_at + 1
    while cursor < len(value):
        candidate = value.find(quote, cursor)
        if candidate < 0:
            return len(value)
        slashes = 0
        before = candidate - 1
        while before > quote_at and value[before] == "\\":
            slashes += 1
            before -= 1
        if (escaped_delimiter and slashes == 1) or (
            not escaped_delimiter and slashes % 2 == 0
        ):
            return candidate + 1
        cursor = candidate + 1
    return len(value)


def _concatenated_secret_suffix_end(value: str, start: int) -> int:
    cursor = start
    while cursor < len(value):
        if cursor < len(value) and value[cursor] in "\"'":
            cursor = _quoted_fragment_end(value, cursor)
            continue
        if (
            cursor + 1 < len(value)
            and value[cursor] == "\\"
            and value[cursor + 1] in "\"'"
        ):
            cursor = _quoted_fragment_end(value, cursor)
            continue
        character = value[cursor]
        if character.isspace() or character in ",;|&()<>，；。)]}":
            break
        if character == "\\" and cursor + 1 < len(value):
            cursor += 2
            continue
        cursor += 1
    return cursor


def _quoted_value_span(value: str, content_start: int, delimiter: str) -> _Span:
    quote = delimiter[-1]
    escaped_delimiter = len(delimiter) == 2
    if not escaped_delimiter and value.startswith(quote * 2, content_start):
        triple_content_start = content_start + 2
        triple_end = _unescaped_sequence_start(
            value, quote * 3, triple_content_start
        )
        if triple_end is not None:
            return _Span(triple_content_start, triple_end, "secret")
        return _Span(triple_content_start, len(value), "secret")
    cursor = content_start
    while cursor < len(value):
        quote_at = value.find(quote, cursor)
        if quote_at < 0:
            break
        slashes = 0
        before = quote_at - 1
        while before >= content_start and value[before] == "\\":
            slashes += 1
            before -= 1
        escaped_quote_is_closing = (
            escaped_delimiter
            and slashes % 2 == 1
            and (
                quote_at + 1 == len(value)
                or value[quote_at + 1] in "}])）"
                or _ESCAPED_NEXT_PROPERTY_PATTERN.match(
                    value, quote_at + 1
                ) is not None
                or (
                    slashes == 1
                    and (
                        value[quote_at + 1].isspace()
                        or value[quote_at + 1] in ",;:.!?，；。：！？"
                    )
                )
            )
        )
        if escaped_quote_is_closing or (
            not escaped_delimiter and slashes % 2 == 0
        ):
            end = quote_at - 1 if escaped_delimiter else quote_at
            if (
                escaped_quote_is_closing
                and quote_at + 1 < len(value)
                and value[quote_at + 1] in ",;:.!?，；。：！？"
            ):
                return _Span(content_start, end, "secret")
            suffix_end = _concatenated_secret_suffix_end(value, quote_at + 1)
            if suffix_end > quote_at + 1:
                return _Span(
                    content_start - len(delimiter), suffix_end, "secret"
                )
            return _Span(content_start, end, "secret")
        cursor = quote_at + 1
    end = content_start
    while end < len(value) and value[end] not in "；。":
        end += 1
    return _Span(content_start, end, "secret")


def _unescaped_sequence_start(
    value: str, sequence: str, start: int
) -> int | None:
    cursor = start
    while True:
        candidate = value.find(sequence, cursor)
        if candidate < 0:
            return None
        slashes = 0
        before = candidate - 1
        while before >= start and value[before] == "\\":
            slashes += 1
            before -= 1
        if slashes % 2 == 0:
            return candidate
        cursor = candidate + 1


def _jwt_spans(value: str) -> list[_Span]:
    if value.count(".") < 2:
        return []
    segments = list(_DOTTED_SEGMENT_PATTERN.finditer(value))
    spans: list[_Span] = []
    for index in range(len(segments) - 2):
        first, payload, signature = segments[index:index + 3]
        if (
            first.end() + 1 != payload.start()
            or payload.end() + 1 != signature.start()
            or value[first.end()] != "."
            or value[payload.end()] != "."
        ):
            continue
        if (
            len(payload.group(0)) < 3
            or not _BASE64URL_SEGMENT_PATTERN.fullmatch(payload.group(0))
            or not _BASE64URL_SIGNATURE_PATTERN.fullmatch(signature.group(0))
        ):
            continue
        secret_start = _jwt_secret_start(first.group(0))
        if secret_start is not None:
            spans.append(_Span(first.start() + secret_start, signature.end(), "secret"))
    return spans


def _jwt_secret_start(header_segment: str) -> int | None:
    attempts = 0
    for match in _JWT_HEADER_START_PATTERN.finditer(header_segment):
        if attempts >= _MAX_JWT_HEADER_CANDIDATES:
            return 0
        attempts += 1
        if _is_jose_header(header_segment[match.start():]):
            return match.start()
    return None


def _is_jose_header(value: str) -> bool:
    if len(value) < 8:
        return False
    if not _BASE64URL_SEGMENT_PATTERN.fullmatch(value) or len(value) % 4 == 1:
        return False
    if len(value) > _MAX_JWT_HEADER_SEGMENT_LENGTH:
        return True
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        header = json.loads(decoded.decode("utf-8"))
    except RecursionError:
        return True
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(header, dict) and isinstance(header.get("alg"), str) and bool(
        header["alg"].strip()
    )


def _split_url_candidate(candidate: str) -> int:
    """Return the web-URL end before an adjacent absolute local path."""
    parenthesis_depth = 0
    for index, character in enumerate(candidate):
        if character == "(":
            parenthesis_depth += 1
        elif character == ")":
            if parenthesis_depth:
                parenthesis_depth -= 1
            else:
                return index
        elif character in ",;" and _ADJACENT_LOCAL_PATH_PATTERN.match(candidate, index + 1):
            return index
    return len(candidate)


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < right and end > left for left, right in ranges)


def _slash_command_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _SLASH_COMMAND_PATTERN.finditer(value):
        if match.group("name").lower() not in _CODEX_SLASH_COMMANDS:
            continue
        start = match.start()
        prefix = value[max(0, start - _SLASH_COMMAND_PREFIX_LIMIT):start]
        if (
            start == 0
            or _SLASH_COMMAND_CUE_PATTERN.search(prefix) is not None
        ):
            ranges.append(match.span())
    return ranges


def _path_span_after_protected_command(
    value: str,
    start: int,
    end: int,
    protected_commands: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Keep a command token, but redact a path-shaped suffix it overlaps."""
    for command_start, command_end in protected_commands:
        if not (start < command_end and end > command_start):
            continue
        if start != command_start or end <= command_end:
            return None
        suffix = value[command_end:end]
        if "/" in suffix or "\\" in suffix or re.search(
            r"\.[A-Za-z][A-Za-z0-9]{0,15}\Z", suffix
        ):
            path_start = command_end
            while path_start < end and value[path_start].isspace():
                path_start += 1
            return path_start, end
        return None
    return start, end


def _http_route_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _HTTP_ROUTE_PATTERN.finditer(value):
        route = match.group("route")
        if _KNOWN_LOCAL_POSIX_VALUE_START_PATTERN.match(route) is None:
            ranges.append(match.span("route"))
    return ranges


def _local_parameter_end(value: str, start: int, parsed_end: int) -> int:
    """Extend a literal local-path parameter across spaces, not parameters."""
    if value[start:start + 1] == "%":
        return parsed_end
    structural_end = len(value)
    for delimiter in "&#":
        found = value.find(delimiter, start)
        if found >= 0:
            structural_end = min(structural_end, found)
    return _scan_local_path_end(value, start, structural_end)


def _local_uri_end(value: str, start: int) -> int:
    """Bound a local URI to its path rather than an outer query or fragment."""
    structural_end = len(value)
    for delimiter in "?&#":
        found = value.find(delimiter, start)
        if found >= 0:
            structural_end = min(structural_end, found)
    return _scan_local_path_end(value, start, structural_end)


def _path_spans(value: str) -> list[_Span]:
    spans: list[_Span] = []
    protected_urls: list[tuple[int, int]] = []
    structured_path_starts: set[int] = set()

    for match in _LOCAL_PATH_URI_START_PATTERN.finditer(value):
        spans.append(
            _Span(match.start(), _local_uri_end(value, match.start()), "path")
        )

    for match in _URI_PATTERN.finditer(value):
        candidate = match.group(0)
        url_length = _split_url_candidate(candidate)
        url_start = match.start()
        url_end = url_start + url_length
        scheme = candidate[:url_length].partition(":")[0].lower()
        if scheme in _LOCAL_PATH_URI_SCHEMES:
            spans.append(_Span(url_start, _local_uri_end(value, url_start), "path"))
        elif scheme not in _WEB_URI_SCHEMES:
            local_paths = tuple(
                match
                for match in (
                    _KNOWN_LOCAL_POSIX_VALUE_START_PATTERN.search(
                        candidate[:url_length]
                    ),
                    _EMBEDDED_LOCAL_VALUE_START_PATTERN.search(
                        candidate[:url_length]
                    ),
                )
                if match is not None
            )
            if local_paths:
                start = url_start + min(local_paths, key=lambda match: match.start()).start()
                spans.append(
                    _Span(
                        start,
                        _scan_local_path_end(value, start, url_end),
                        "path",
                    )
                )
        protected_urls.append((url_start, url_end))
        url = value[url_start:url_end]
        for parameter in _URL_PARAMETER_PATTERN.finditer(url):
            key = parameter.group("key").lower()
            parameter_value = parameter.group("value")
            is_local = (
                key in _LOCAL_PATH_PARAMETER_NAMES
                and _LOCAL_VALUE_START_PATTERN.match(parameter_value) is not None
            ) or (
                _UNAMBIGUOUS_LOCAL_VALUE_START_PATTERN.match(parameter_value)
                is not None
            ) or (
                _KNOWN_LOCAL_POSIX_VALUE_START_PATTERN.match(parameter_value)
                is not None
            )
            if is_local:
                start = url_start + parameter.start("value")
                end = _local_parameter_end(
                    value, start, url_start + parameter.end("value")
                )
                spans.append(_Span(start, end, "path"))
                structured_path_starts.add(start)

    protected_commands = _slash_command_ranges(value)
    protected_routes = _http_route_ranges(value)
    protected_route_starts = {start for start, _ in protected_routes}
    for route_start, route_end in protected_routes:
        route = value[route_start:route_end]
        for parameter in _URL_PARAMETER_PATTERN.finditer(route):
            key = parameter.group("key").lower()
            parameter_value = parameter.group("value")
            is_local = (
                key in _LOCAL_PATH_PARAMETER_NAMES
                and _LOCAL_VALUE_START_PATTERN.match(parameter_value) is not None
            ) or (
                _UNAMBIGUOUS_LOCAL_VALUE_START_PATTERN.match(parameter_value)
                is not None
            ) or (
                _KNOWN_LOCAL_POSIX_VALUE_START_PATTERN.match(parameter_value)
                is not None
            )
            if is_local:
                start = route_start + parameter.start("value")
                end = _local_parameter_end(
                    value, start, route_start + parameter.end("value")
                )
                spans.append(_Span(start, end, "path"))
                structured_path_starts.add(start)

    existing_ranges = sorted(
        (span.start, span.end) for span in spans if span.start < span.end
    )
    existing_index = 0
    encoded_protected_url_index = 0
    for match in _STANDALONE_ENCODED_LOCAL_PATH_START_PATTERN.finditer(value):
        start = match.start()
        while (
            existing_index < len(existing_ranges)
            and existing_ranges[existing_index][1] <= start
        ):
            existing_index += 1
        if (
            existing_index < len(existing_ranges)
            and existing_ranges[existing_index][0] <= start
            < existing_ranges[existing_index][1]
        ):
            continue
        while (
            encoded_protected_url_index < len(protected_urls)
            and protected_urls[encoded_protected_url_index][1] <= start
        ):
            encoded_protected_url_index += 1
        if (
            encoded_protected_url_index < len(protected_urls)
            and protected_urls[encoded_protected_url_index][0] <= start
            < protected_urls[encoded_protected_url_index][1]
        ):
            continue
        spans.append(_Span(start, _scan_local_path_end(value, start), "path"))

    preclassified_ranges = sorted(
        (span.start, span.end) for span in spans if span.start < span.end
    )
    preclassified_index = 0
    protected_url_index = 0
    accepted_generic_end = 0
    for match in _LOCAL_PATH_START_PATTERN.finditer(value):
        start = match.start()

        if start < accepted_generic_end:
            continue
        while (
            preclassified_index < len(preclassified_ranges)
            and preclassified_ranges[preclassified_index][1] <= start
        ):
            preclassified_index += 1
        if (
            preclassified_index < len(preclassified_ranges)
            and preclassified_ranges[preclassified_index][0] <= start
            < preclassified_ranges[preclassified_index][1]
        ):
            continue
        while (
            protected_url_index < len(protected_urls)
            and protected_urls[protected_url_index][1] <= start
        ):
            protected_url_index += 1
        if (
            protected_url_index < len(protected_urls)
            and protected_urls[protected_url_index][0] <= start
            < protected_urls[protected_url_index][1]
        ):
            continue
        if start in structured_path_starts:
            continue

        if (
            match.lastgroup == "posix"
            and _posix_start_has_explicit_relative_context(value, start)
        ):
            continue

        end = _scan_local_path_end(value, start)
        adjusted = _path_span_after_protected_command(
            value, start, end, protected_commands
        )
        if adjusted is None:
            continue
        start, end = adjusted
        if start in protected_route_starts:
            continue
        if not _overlaps(start, end, protected_urls):
            spans.append(_Span(start, end, "path"))
            accepted_generic_end = max(accepted_generic_end, end)
    return spans


def _merge_spans(spans: list[_Span]) -> list[_Span]:
    valid = sorted(
        (span for span in spans if span.start < span.end),
        key=lambda span: (span.start, span.end, span.kind != "secret"),
    )
    if not valid:
        return []
    merged: list[_Span] = []
    current = valid[0]
    for span in valid[1:]:
        if span.start >= current.end:
            merged.append(current)
            current = span
            continue
        current = _Span(
            current.start,
            max(current.end, span.end),
            "secret" if "secret" in (current.kind, span.kind) else "path",
        )
    merged.append(current)
    return merged


def _render(value: str, spans: list[_Span]) -> str:
    pieces: list[str] = []
    position = 0
    for span in _merge_spans(spans):
        pieces.append(value[position:span.start])
        pieces.append(_SECRET_MASK if span.kind == "secret" else _LOCAL_PATH_MASK)
        position = span.end
    pieces.append(value[position:])
    return "".join(pieces)


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def _bounded_source(value: str, scan_limit: int) -> tuple[str, bool]:
    if len(value) <= scan_limit:
        return value, False
    source = value[:scan_limit]
    # Never expose the lexical unit that touches the scan boundary. A token
    # truncated before detection may no longer satisfy its secret/path shape.
    boundary = max(
        source.rfind(character)
        for character in (" ", "\t", "\r", "\n", ",", ";", "，", "；", "。")
    )
    return (source[: boundary + 1] if boundary >= 0 else ""), True


def safe_summary(value: object, limit: int) -> str:
    """Return compact text suitable for a third-party notification channel."""
    if not isinstance(value, str) or limit <= 0:
        return ""
    scan_limit = min(_MAX_SCAN_CHARS, max(_MIN_SCAN_CHARS, limit * _SCAN_MULTIPLIER))
    # Keep raw work bounded independently from the compact-text budget. This
    # lets semantically empty whitespace prefixes collapse without consuming
    # the entire content budget, while still refusing to scan unbounded input.
    raw_source, raw_was_truncated = _bounded_source(value, _MAX_SCAN_CHARS)
    compact = re.sub(r"\s+", " ", raw_source).strip()
    source, compact_was_truncated = _bounded_source(compact, scan_limit)
    source_was_truncated = raw_was_truncated or compact_was_truncated
    if not source:
        return _truncate("…", limit) if source_was_truncated else ""
    redacted = _render(source, _secret_spans(source) + _path_spans(source))
    if source_was_truncated:
        redacted += "…"
    return _truncate(redacted, limit)
