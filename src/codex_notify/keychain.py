"""Read and configure Feishu credentials in the macOS Keychain."""

from __future__ import annotations

import ctypes
import getpass
import json
import os
import subprocess
import sys
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from .constants import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_CREDENTIALS_SERVICE,
    KEYCHAIN_SECRET_SERVICE,
    KEYCHAIN_WEBHOOK_SERVICE,
)


class KeychainError(RuntimeError):
    pass


class KeychainItemNotFound(KeychainError):
    pass


class _KeychainInteractionNotAllowed(KeychainError):
    pass


class _NativeReadFallback(KeychainError):
    pass


KEYCHAIN_READ_TIMEOUT_SECONDS = 15
KEYCHAIN_NATIVE_READ_TIMEOUT_SECONDS = 5
SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION_FRAMEWORK = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
CF_STRING_ENCODING_UTF8 = 0x08000100
ERR_SEC_SUCCESS = 0
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300
ERR_SEC_INTERACTION_NOT_ALLOWED = -25308

_NATIVE_READ_SCRIPT = """
import base64
import sys
from codex_notify.keychain import (
    KeychainItemNotFound,
    _KeychainInteractionNotAllowed,
    _read_native_keychain_item,
)

try:
    value = _read_native_keychain_item(sys.argv[1], sys.argv[2])
except KeychainItemNotFound:
    raise SystemExit(44)
except _KeychainInteractionNotAllowed:
    raise SystemExit(77)
except Exception:
    raise SystemExit(1)
sys.stdout.buffer.write(base64.b64encode(value))
"""


@dataclass(frozen=True)
class FeishuCredentials:
    webhook_url: str
    signing_secret: str


def get_password(service: str) -> str:
    try:
        raw_value = _read_native_keychain_item_bounded(service, KEYCHAIN_ACCOUNT)
    except _NativeReadFallback:
        return _read_password_via_security_cli(service)
    try:
        value = raw_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeychainError(f"Keychain 项 {service} 不是有效 UTF-8") from exc
    if not value:
        raise KeychainError(f"Keychain 项 {service} 为空")
    return value


def _read_password_via_security_cli(service: str) -> str:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("读取 Keychain 超时") from exc
    if result.returncode != 0:
        if result.returncode == 44 or "could not be found" in result.stderr.lower():
            raise KeychainItemNotFound(f"Keychain 中缺少 {service}")
        raise KeychainError(
            f"无法读取 Keychain 项 {service}（security 退出码 {result.returncode}）"
        )
    value = result.stdout.rstrip("\r\n")
    if not value:
        raise KeychainError(f"Keychain 项 {service} 为空")
    return value


def load_credentials() -> FeishuCredentials:
    try:
        raw = get_password(KEYCHAIN_CREDENTIALS_SERVICE)
    except KeychainItemNotFound:
        return FeishuCredentials(
            webhook_url=get_password(KEYCHAIN_WEBHOOK_SERVICE),
            signing_secret=get_password(KEYCHAIN_SECRET_SERVICE),
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeychainError("Keychain 中的飞书凭据格式无效") from exc
    if not isinstance(payload, dict):
        raise KeychainError("Keychain 中的飞书凭据格式无效")
    webhook_url = payload.get("webhook_url")
    signing_secret = payload.get("signing_secret")
    if not isinstance(webhook_url, str) or not isinstance(signing_secret, str):
        raise KeychainError("Keychain 中的飞书凭据格式无效")
    return FeishuCredentials(
        webhook_url=webhook_url,
        signing_secret=signing_secret,
    )


def prompt_secret(label: str) -> str:
    value = getpass.getpass(f"请输入{label}（输入不会显示）：").strip()
    if not value:
        raise KeychainError(f"{label}不能为空")
    return value


def store_credentials(credentials: FeishuCredentials) -> None:
    payload = json.dumps(
        {
            "webhook_url": credentials.webhook_url,
            "signing_secret": credentials.signing_secret,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _set_password_via_security_framework(
        KEYCHAIN_CREDENTIALS_SERVICE,
        "Codex Notify - 飞书凭据",
        payload,
    )


def _set_password_via_security_framework(service: str, label: str, value: str) -> None:
    """Write arbitrary-length Keychain data without argv or temporary files."""
    _write_native_keychain_item(
        service,
        KEYCHAIN_ACCOUNT,
        label,
        value.encode("utf-8"),
    )


def _configure_native_functions(security: object, core_foundation: object) -> None:
    security.SecItemCopyMatching.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecItemCopyMatching.restype = ctypes.c_int32
    security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecItemUpdate.restype = ctypes.c_int32
    security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecItemAdd.restype = ctypes.c_int32
    core_foundation.CFStringCreateWithBytes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_long,
        ctypes.c_uint32,
        ctypes.c_bool,
    ]
    core_foundation.CFStringCreateWithBytes.restype = ctypes.c_void_p
    core_foundation.CFDataCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_long,
    ]
    core_foundation.CFDataCreate.restype = ctypes.c_void_p
    core_foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
    core_foundation.CFDataGetLength.restype = ctypes.c_long
    core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    core_foundation.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
    core_foundation.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    core_foundation.CFDictionaryCreate.restype = ctypes.c_void_p
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None


class _CFObjectArena:
    def __init__(self, core_foundation: object) -> None:
        self.core_foundation = core_foundation
        self.references: list[int] = []

    def __enter__(self) -> _CFObjectArena:
        return self

    def __exit__(self, *_: object) -> None:
        for reference in reversed(self.references):
            self.core_foundation.CFRelease(reference)

    def _track(self, reference: int | None) -> int:
        if not reference:
            raise KeychainError("无法分配 Keychain 原生对象")
        self.references.append(reference)
        return reference

    def string(self, value: str) -> int:
        encoded = value.encode("utf-8")
        buffer = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
        return self._track(
            self.core_foundation.CFStringCreateWithBytes(
                None,
                buffer,
                len(encoded),
                CF_STRING_ENCODING_UTF8,
                False,
            )
        )

    def data(self, value: bytes) -> int:
        buffer = (ctypes.c_uint8 * len(value)).from_buffer_copy(value)
        return self._track(
            self.core_foundation.CFDataCreate(None, buffer, len(value))
        )

    def dictionary(self, pairs: list[tuple[int, int]]) -> int:
        keys = (ctypes.c_void_p * len(pairs))(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * len(pairs))(*(value for _, value in pairs))
        return self._track(
            self.core_foundation.CFDictionaryCreate(
                None,
                keys,
                values,
                len(pairs),
                None,
                None,
            )
        )


def _security_constant(security: object, name: str) -> int:
    try:
        value = ctypes.c_void_p.in_dll(security, name).value
    except ValueError as exc:
        raise KeychainError(f"Security.framework 缺少 {name}") from exc
    if not value:
        raise KeychainError(f"Security.framework 常量 {name} 为空")
    return value


def _load_native_frameworks() -> tuple[object, object]:
    try:
        security = ctypes.CDLL(SECURITY_FRAMEWORK)
        core_foundation = ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK)
    except OSError as exc:
        raise KeychainError("无法加载 macOS Keychain 原生框架") from exc
    _configure_native_functions(security, core_foundation)
    return security, core_foundation


def _copy_keychain_item_data(
    security: object,
    core_foundation: object,
    query: int,
) -> bytes:
    result = ctypes.c_void_p()
    status = security.SecItemCopyMatching(query, ctypes.byref(result))
    if status == ERR_SEC_ITEM_NOT_FOUND:
        raise KeychainItemNotFound("Keychain 中缺少指定项目")
    if status == ERR_SEC_INTERACTION_NOT_ALLOWED:
        raise _KeychainInteractionNotAllowed(
            "Security.framework 不允许非交互式读取该 Keychain 项"
        )
    if status != ERR_SEC_SUCCESS:
        raise KeychainError(f"读取飞书凭据失败（Security.framework 状态 {status}）")
    if not result.value:
        raise KeychainError("Security.framework 未返回 Keychain 数据")

    try:
        length = core_foundation.CFDataGetLength(result.value)
        if length <= 0:
            return b""
        pointer = core_foundation.CFDataGetBytePtr(result.value)
        if not pointer:
            raise KeychainError("Security.framework 返回的 Keychain 数据无效")
        return ctypes.string_at(pointer, length)
    finally:
        core_foundation.CFRelease(result.value)


def _read_native_keychain_item(service: str, account: str) -> bytes:
    security, core_foundation = _load_native_frameworks()
    class_key = _security_constant(security, "kSecClass")
    generic_password = _security_constant(security, "kSecClassGenericPassword")
    service_key = _security_constant(security, "kSecAttrService")
    account_key = _security_constant(security, "kSecAttrAccount")
    return_data_key = _security_constant(security, "kSecReturnData")
    match_limit_key = _security_constant(security, "kSecMatchLimit")
    match_limit_one = _security_constant(security, "kSecMatchLimitOne")
    authentication_ui_key = _security_constant(security, "kSecUseAuthenticationUI")
    authentication_ui_fail = _security_constant(
        security,
        "kSecUseAuthenticationUIFail",
    )
    return_data_value = _security_constant(core_foundation, "kCFBooleanTrue")

    with _CFObjectArena(core_foundation) as arena:
        query = arena.dictionary(
            [
                (class_key, generic_password),
                (service_key, arena.string(service)),
                (account_key, arena.string(account)),
                (return_data_key, return_data_value),
                (match_limit_key, match_limit_one),
                (authentication_ui_key, authentication_ui_fail),
            ]
        )
        return _copy_keychain_item_data(security, core_foundation, query)


def _read_native_keychain_item_bounded(service: str, account: str) -> bytes:
    environment = os.environ.copy()
    library_root = str(Path(__file__).resolve().parent.parent)
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{library_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else library_root
    )
    process = subprocess.Popen(
        [sys.executable, "-P", "-c", _NATIVE_READ_SCRIPT, service, account],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    try:
        stdout, _ = process.communicate(timeout=KEYCHAIN_NATIVE_READ_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        Thread(target=process.wait, daemon=True).start()
        raise _NativeReadFallback("原生 Keychain 读取超时") from exc
    if process.returncode == 44:
        raise KeychainItemNotFound(f"Keychain 中缺少 {service}")
    if process.returncode == 77:
        raise _NativeReadFallback("原生 Keychain 读取需要交互授权")
    if process.returncode != 0:
        raise KeychainError(
            f"无法读取 Keychain 项 {service}（原生子进程退出码 {process.returncode}）"
        )
    try:
        return b64decode(stdout, validate=True)
    except ValueError as exc:
        raise KeychainError("原生 Keychain 读取返回无效数据") from exc


def _upsert_keychain_item(
    security: object,
    query: int,
    attributes_to_update: int,
    attributes_to_add: int,
) -> None:
    status = security.SecItemUpdate(query, attributes_to_update)
    if status == ERR_SEC_ITEM_NOT_FOUND:
        status = security.SecItemAdd(attributes_to_add, None)
        if status == ERR_SEC_DUPLICATE_ITEM:
            status = security.SecItemUpdate(query, attributes_to_update)
    if status != ERR_SEC_SUCCESS:
        raise KeychainError(f"写入飞书凭据失败（Security.framework 状态 {status}）")


def _write_native_keychain_item(
    service: str,
    account: str,
    label: str,
    value: bytes,
) -> None:
    security, core_foundation = _load_native_frameworks()

    class_key = _security_constant(security, "kSecClass")
    generic_password = _security_constant(security, "kSecClassGenericPassword")
    service_key = _security_constant(security, "kSecAttrService")
    account_key = _security_constant(security, "kSecAttrAccount")
    label_key = _security_constant(security, "kSecAttrLabel")
    value_key = _security_constant(security, "kSecValueData")

    with _CFObjectArena(core_foundation) as arena:
        service_value = arena.string(service)
        account_value = arena.string(account)
        label_value = arena.string(label)
        data_value = arena.data(value)
        query = arena.dictionary(
            [
                (class_key, generic_password),
                (service_key, service_value),
                (account_key, account_value),
            ]
        )
        attributes_to_update = arena.dictionary(
            [
                (label_key, label_value),
                (value_key, data_value),
            ]
        )
        attributes_to_add = arena.dictionary(
            [
                (class_key, generic_password),
                (service_key, service_value),
                (account_key, account_value),
                (label_key, label_value),
                (value_key, data_value),
            ]
        )
        _upsert_keychain_item(
            security,
            query,
            attributes_to_update,
            attributes_to_add,
        )
