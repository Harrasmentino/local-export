"""Select the native credential vault without a plaintext fallback."""
from importlib import import_module
import sys

from credential_common import CredentialStoreError, MAX_BLOB_BYTES, target_name


def _backend():
    if sys.platform == 'win32':
        return import_module('windows_credentials')
    if sys.platform == 'darwin':
        return import_module('macos_credentials')
    raise CredentialStoreError('Поддерживаются Windows Credential Manager и macOS Keychain.')


def credential_store_label() -> str:
    if sys.platform == 'win32':
        return 'Windows Credential Manager'
    if sys.platform == 'darwin':
        return 'macOS Keychain'
    return 'системное хранилище доступов'


def load_credentials(base_url: str) -> tuple[str, str] | None:
    return _backend().load_credentials(base_url)


def save_credentials(base_url: str, email: str, token: str) -> None:
    _backend().save_credentials(base_url, email, token)


def delete_credentials(base_url: str) -> None:
    _backend().delete_credentials(base_url)