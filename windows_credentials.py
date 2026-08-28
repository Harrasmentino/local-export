"""App-scoped Windows Credential Manager entries; never enumerate the vault.

Win32 contract: https://learn.microsoft.com/windows/win32/api/wincred/ns-wincred-credentialw
Generic credentials, persisted only for this user on this computer. No plaintext
file fallback and no external package or network calls.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from urllib.parse import urlsplit

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_BLOB_BYTES = 2560


class CredentialStoreError(OSError):
    """Messages deliberately contain neither target, username nor secret."""


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(wintypes.BYTE)), ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
    ]


def target_name(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise CredentialStoreError('Некорректный адрес для хранилища доступов.')
    return 'ConfluenceLocalExport:https://' + parsed.hostname.lower()


def _api():
    if os.name != 'nt':
        raise CredentialStoreError('Для сохранения доступов требуется Windows Credential Manager.')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    pointer = ctypes.POINTER(CREDENTIALW)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def load_credentials(base_url: str) -> tuple[str, str] | None:
    target, api = target_name(base_url), _api()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise CredentialStoreError('Windows не разрешила прочитать сохранённые доступы.')
    try:
        entry = pointer.contents
        if not entry.UserName or not entry.CredentialBlob or not 0 < entry.CredentialBlobSize <= MAX_BLOB_BYTES:
            raise CredentialStoreError('Запись доступов повреждена. Запустите с --update-credentials.')
        try:
            token = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode('utf-8')
        except UnicodeError:
            raise CredentialStoreError('Запись доступов повреждена. Запустите с --update-credentials.') from None
        return entry.UserName, token
    finally:
        # CredRead allocates one native block. Wipe its secret bytes before CredFree.
        entry = pointer.contents
        if entry.CredentialBlob and 0 < entry.CredentialBlobSize <= MAX_BLOB_BYTES:
            ctypes.memset(entry.CredentialBlob, 0, entry.CredentialBlobSize)
        api.CredFree(pointer)


def save_credentials(base_url: str, email: str, token: str) -> None:
    target = target_name(base_url)
    encoded = token.encode('utf-8')
    if not email or '\x00' in email or len(email.encode('utf-16-le')) // 2 > 513 or not 0 < len(encoded) <= MAX_BLOB_BYTES:
        raise CredentialStoreError('Доступы пусты или превышают лимит хранилища Windows; запись не изменена.')
    buffer = ctypes.create_string_buffer(encoded)
    entry = CREDENTIALW()
    entry.Type, entry.TargetName = CRED_TYPE_GENERIC, target
    entry.UserName = email
    entry.CredentialBlobSize = len(encoded)
    entry.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
    entry.Persist = CRED_PERSIST_LOCAL_MACHINE
    try:
        if not _api().CredWriteW(ctypes.byref(entry), 0):
            raise CredentialStoreError('Windows не разрешила сохранить доступы. В открытый файл токен не записывался.')
    finally:
        ctypes.memset(buffer, 0, len(buffer))


def delete_credentials(base_url: str) -> None:
    """Only this application's exact target; used to clean up the native QA entry."""
    target = target_name(base_url)
    if not _api().CredDeleteW(target, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
        raise CredentialStoreError('Не удалось удалить запись доступов Windows.')
