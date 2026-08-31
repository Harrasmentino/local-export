"""App-scoped credentials in the current user's macOS Keychain."""
from __future__ import annotations

import ctypes
import json
import sys

from credential_common import CredentialStoreError, MAX_BLOB_BYTES, target_name, validate_credentials

_ERR_SEC_ITEM_NOT_FOUND = -25300
_ACCOUNT = b'credentials'
_MAX_PAYLOAD_BYTES = MAX_BLOB_BYTES + 2048


def _api():
    if sys.platform != 'darwin':
        raise CredentialStoreError('Для macOS Keychain требуется запуск на Mac.')
    try:
        security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    except OSError:
        raise CredentialStoreError('Не удалось открыть системный macOS Keychain.') from None
    uint32, pointer = ctypes.c_uint32, ctypes.c_void_p
    security.SecKeychainFindGenericPassword.argtypes = [
        pointer, uint32, ctypes.c_char_p, uint32, ctypes.c_char_p,
        ctypes.POINTER(uint32), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        pointer, uint32, ctypes.c_char_p, uint32, ctypes.c_char_p,
        uint32, pointer, ctypes.POINTER(pointer)]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint32, pointer]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    security.SecKeychainItemDelete.argtypes = [pointer]
    security.SecKeychainItemDelete.restype = ctypes.c_int32
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None
    return security, core


def _service(base_url: str) -> bytes:
    return target_name(base_url).encode('utf-8')


def _find(base_url: str, include_password: bool):
    security, core = _api()
    service = _service(base_url)
    length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(service), service, len(_ACCOUNT), _ACCOUNT,
        ctypes.byref(length) if include_password else None,
        ctypes.byref(data) if include_password else None, ctypes.byref(item))
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return security, core, None, length, data
    if status != 0 or not item.value:
        raise CredentialStoreError('macOS не разрешила прочитать сохранённые доступы.')
    return security, core, item, length, data


def _encode(email: str, token: str) -> bytearray:
    validate_credentials(email, token)
    payload = bytearray(json.dumps({'email': email, 'token': token}, ensure_ascii=False,
                                   separators=(',', ':')).encode('utf-8'))
    if len(payload) > _MAX_PAYLOAD_BYTES:
        for index in range(len(payload)):
            payload[index] = 0
        raise CredentialStoreError('Доступы превышают лимит macOS Keychain; запись не изменена.')
    return payload


def _decode(payload: bytearray) -> tuple[str, str]:
    try:
        value = json.loads(payload.decode('utf-8'))
        if not isinstance(value, dict) or set(value) != {'email', 'token'}:
            raise ValueError()
        email, token = value['email'], value['token']
        if not isinstance(email, str) or not isinstance(token, str):
            raise ValueError()
        validate_credentials(email, token)
        return email, token
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise CredentialStoreError('Запись доступов macOS Keychain повреждена. Запусти с --update-credentials.') from None


def load_credentials(base_url: str) -> tuple[str, str] | None:
    security, core, item, length, data = _find(base_url, True)
    if item is None:
        return None
    payload = bytearray()
    try:
        if not data.value or not 0 < length.value <= _MAX_PAYLOAD_BYTES:
            raise CredentialStoreError('Запись доступов macOS Keychain повреждена. Запусти с --update-credentials.')
        payload = bytearray(ctypes.string_at(data, length.value))
        return _decode(payload)
    finally:
        for index in range(len(payload)):
            payload[index] = 0
        if data.value and length.value:
            ctypes.memset(data, 0, length.value)
        security.SecKeychainItemFreeContent(None, data)
        core.CFRelease(item)


def save_credentials(base_url: str, email: str, token: str) -> None:
    payload = _encode(email, token)
    service = _service(base_url)
    security, core, item, _length, _data = _find(base_url, False)
    native = (ctypes.c_ubyte * len(payload)).from_buffer(payload)
    try:
        if item is None:
            status = security.SecKeychainAddGenericPassword(
                None, len(service), service, len(_ACCOUNT), _ACCOUNT,
                len(payload), ctypes.cast(native, ctypes.c_void_p), None)
        else:
            status = security.SecKeychainItemModifyAttributesAndData(
                item, None, len(payload), ctypes.cast(native, ctypes.c_void_p))
        if status != 0:
            raise CredentialStoreError('macOS не разрешила сохранить доступы. В открытый файл токен не записывался.')
    finally:
        ctypes.memset(native, 0, len(payload))
        if item is not None:
            core.CFRelease(item)


def delete_credentials(base_url: str) -> None:
    security, core, item, _length, _data = _find(base_url, False)
    if item is None:
        return
    try:
        if security.SecKeychainItemDelete(item) != 0:
            raise CredentialStoreError('Не удалось удалить запись доступов macOS Keychain.')
    finally:
        core.CFRelease(item)