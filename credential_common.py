"""Shared credential validation; platform backends never use plaintext files."""
from urllib.parse import urlsplit

MAX_BLOB_BYTES = 2560


class CredentialStoreError(OSError):
    """Messages deliberately contain neither target, username nor secret."""


def target_name(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise CredentialStoreError('Некорректный адрес для хранилища доступов.')
    return 'ConfluenceLocalExport:https://' + parsed.hostname.lower()


def validate_credentials(email: str, token: str) -> bytes:
    encoded = token.encode('utf-8')
    if (not email or '\x00' in email or len(email.encode('utf-16-le')) // 2 > 513
            or not 0 < len(encoded) <= MAX_BLOB_BYTES or '\x00' in token):
        raise CredentialStoreError('Доступы пусты или превышают лимит системного хранилища; запись не изменена.')
    return encoded