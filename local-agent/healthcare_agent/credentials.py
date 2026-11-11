"""Read only this application's OpenRouter key; never persist it in the project."""

from __future__ import annotations

import ctypes
import os
import sys

SERVICE = b"exl-healthcare-integration.openrouter"
ACCOUNT = b"local-agent"


class CredentialError(RuntimeError):
    pass


def _security():
    if sys.platform != "darwin":
        raise CredentialError("macOS Keychain is unavailable; use OPENROUTER_API_KEY or --prompt-key.")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
    ]
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core.CFRelease.argtypes = [ctypes.c_void_p]
    return security, core


def store_keychain_key(key: str) -> None:
    """Called only by the user's explicit interactive configure command."""
    if not key or key != key.strip() or "\x00" in key:
        raise CredentialError("API key is empty or contains invalid whitespace.")
    security, core = _security()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, None, None, ctypes.byref(item)
    )
    secret = key.encode("utf-8")
    try:
        if status == 0:
            status = security.SecKeychainItemModifyAttributesAndData(item, None, len(secret), secret)
        elif status == -25300:  # errSecItemNotFound
            status = security.SecKeychainAddGenericPassword(
                None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, len(secret), secret, None
            )
        if status != 0:
            raise CredentialError(f"Keychain write was not completed (OSStatus {status}).")
    finally:
        if item.value:
            core.CFRelease(item)


def load_api_key() -> tuple[str | None, str]:
    """Return the secret only to the caller in memory, and its non-secret source."""
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key, "environment"
    if sys.platform != "darwin":
        return None, "not_configured"
    security, core = _security()
    size = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
        ctypes.byref(size), ctypes.byref(data), ctypes.byref(item),
    )
    try:
        if status == -25300:
            return None, "not_configured"
        if status != 0:
            raise CredentialError(f"Keychain access was not completed (OSStatus {status}).")
        return ctypes.string_at(data, size.value).decode("utf-8"), "macos_keychain"
    finally:
        if data.value:
            security.SecKeychainItemFreeContent(None, data)
        if item.value:
            core.CFRelease(item)
