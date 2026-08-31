"""Fresh Confluence downloads using stdlib, verified TLS and no AI APIs."""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import ssl
import time
import warnings
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from credential_store import credential_store_label, load_credentials, save_credentials


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError('', code, 'Redirect refused', headers, fp)


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Требуется HTTPS-адрес без логина и пароля в URL.')
    return 'https://' + parsed.hostname.lower()


def page_id_from_url(url: str) -> str | None:
    match = re.search(r'/pages/(\d+)(?:/|$|[?#])|[?&](?:pageId|contentId)=(\d+)(?:&|$)', url)
    return next((v for v in match.groups() if v), None) if match else None


def validate_link(url: str, base_url: str) -> str:
    if origin(url) != origin(base_url):
        raise ValueError('Ссылка относится к другому серверу.')
    pid = page_id_from_url(url)
    if not pid:
        raise ValueError('В ссылке нет pageId.')
    return pid


def strip_tags(html_text: str) -> str:
    text = re.sub(r'<(script|style)\b[^>]*>[\s\S]*?</\1>', '', html_text, flags=re.I)
    text = re.sub(r'</(?:p|div|li|tr|h[1-6]|td|th)\s*>|<br\s*/?>', '\n', text, flags=re.I)
    text = unescape(re.sub(r'<[^>]+>', ' ', text))
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n', text).strip()


def find_balanced(html: str, tag: str, start_at: int = 0,
                  open_pattern: str | None = None) -> tuple[int, int, int] | None:
    first = re.search(open_pattern or rf'<{tag}\b[^>]*>', html[start_at:], re.I)
    if not first:
        return None
    start, end = start_at + first.start(), start_at + first.end()
    if html[start:end].rstrip().endswith('/>'):
        return find_balanced(html, tag, end, open_pattern)
    depth = 1
    for match in re.finditer(rf'</?{tag}\b[^>]*>', html[end:], re.I):
        token = match.group()
        if token.startswith('</'):
            depth -= 1
        elif not token.rstrip().endswith('/>'):
            depth += 1
        if depth == 0:
            return start, end, end + match.start()
    return None


def extract_expands(storage_html: str) -> list[dict]:
    expands, pos = [], 0
    while True:
        found = find_balanced(storage_html, 'ac:structured-macro', pos,
            r'''<ac:structured-macro\b[^>]*ac:name=["']expand["'][^>]*>''')
        if not found:
            break
        body = storage_html[found[1]:found[2]]
        title = re.search(r'''<ac:parameter\b[^>]*ac:name=["']title["'][^>]*>(.*?)</ac:parameter>''', body, re.I | re.S)
        rich = find_balanced(body, 'ac:rich-text-body')
        text = body[rich[1]:rich[2]] if rich else body
        expands.append({'index': len(expands) + 1,
                        'title': strip_tags(title.group(1)) if title else '',
                        'text': strip_tags(text)})
        pos = found[2] + len('</ac:structured-macro>')
    return expands


def load_links(path: Path) -> list[str]:
    links = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if not links:
        raise ValueError('Файл ссылок пуст.')
    return list(dict.fromkeys(links))


def save(pages: list[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump({'total': len(pages), 'fetched_at': datetime.now(timezone.utc).isoformat(),
                       'pages': pages}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def download(links_path: Path, output: Path, *, email: str, token: str,
             base_url: str = '') -> list[dict]:
    links = load_links(links_path)
    base = origin(base_url or links[0])
    # Validate every target before sending credentials. Never reuse a cached page.
    targets = [(url, validate_link(url, base)) for url in links]
    targets = list({pid: (url, pid) for url, pid in targets}.values())
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    authorization = 'Basic ' + base64.b64encode(f'{email}:{token}'.encode()).decode('ascii')
    pages = []
    for index, (url, pid) in enumerate(targets, 1):
        request = Request(f'{base}/wiki/rest/api/content/{pid}?expand=body.storage,title,version',
            headers={'Accept': 'application/json', 'Authorization': authorization,
                     'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
        result = {'url': url, 'page_id': pid}
        for attempt in range(3):
            try:
                with opener.open(request, timeout=40) as response:
                    body = json.load(response)
                storage = body['body']['storage']['value']
                if not isinstance(storage, str) or not isinstance(body.get('title'), str):
                    raise ValueError('Invalid response schema')
                version = body.get('version', {})
                if not isinstance(version, dict) or str(body.get('id', pid)) != pid:
                    raise ValueError('Invalid page identity/version')
                result.update(title=body['title'], full_text=strip_tags(storage),
                              expands=extract_expands(storage), storage_html=storage,
                              fetched_at=datetime.now(timezone.utc).isoformat(),
                              version=version.get('number'), updated_at=version.get('when'))
                break
            except HTTPError as error:
                if error.code == 429 and attempt < 2:
                    delay = str(error.headers.get('Retry-After', '10'))
                    time.sleep(min(60, max(1, int(delay))) if delay.isdigit() else 10)
                    continue
                result['error'] = f'HTTP {error.code}'
                break
            except (URLError, TimeoutError, OSError):
                result['error'] = 'network_error'
                break
            except (ValueError, KeyError, TypeError):
                result['error'] = 'invalid_response'
                break
        pages.append(result)
        print(f'Страница {index}/{len(targets)}: ' + ('ошибка' if 'error' in result else 'OK'), flush=True)
        if result.get('error') in ('HTTP 401', 'HTTP 403'):
            pages.extend({'url': u, 'page_id': p, 'error': 'not_fetched_auth'} for u, p in targets[index:])
            break
        if index < len(targets):
            time.sleep(0.3)
    save(pages, output)
    return pages


def credentials(base_url: str, *, force_prompt: bool = False, email_hint: str = '') -> tuple[str, str]:
    if not force_prompt:
        stored = load_credentials(base_url)
        if stored:
            print('Используются сохранённые доступы из ' + credential_store_label() + '.')
            return stored
        email = os.environ.get('CONF_EMAIL', '')
        token = os.environ.get('CONF_TOKEN', '')
    else:
        # A rejected environment token must not silently override a replacement.
        email, token = '', ''
    if not email:
        prompt = 'Email Confluence (Enter — прежний): ' if email_hint else 'Email Confluence: '
        email = input(prompt).strip() or email_hint
    if not token:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                token = getpass.getpass('API-токен (ввод скрыт; пустой ввод — отмена): ')
        except getpass.GetPassWarning:
            raise ValueError('Не удалось включить скрытый ввод. Запусти программу в обычном локальном терминале.') from None
    if not email or not token:
        raise ValueError('Ввод доступов отменён.')
    return email, token


def download_authenticated(links_path: Path, output: Path, *, update_credentials: bool = False) -> list[dict]:
    links = load_links(links_path)
    base = origin(os.environ.get('CONF_BASE_URL') or links[0])
    for link in links:
        validate_link(link, base)
    email_hint = ''
    for attempt in range(2):
        email, token = credentials(base, force_prompt=update_credentials or attempt > 0,
                                   email_hint=email_hint)
        email_hint = email
        pages = download(links_path, output, email=email, token=token, base_url=base)
        rejected = any(p.get('error') == 'HTTP 401' for p in pages)
        if rejected:
            del token
            if attempt == 0:
                print('Confluence отклонил авторизацию (401). Токен мог истечь или быть отозван.')
                print('Введите новые доступы; пустой токен отменит запуск. Все страницы будут загружены заново.')
                continue
            print('Новые доступы тоже отклонены. Повтор остановлен; сохранённая запись не изменена.')
        elif any(p.get('error') == 'HTTP 403' for p in pages):
            print('Доступ к странице запрещён (403). Проверьте права; автоматической замены токена нет.')
            print('Для ручной замены запусти загрузчик с --update-credentials.')
        if pages and all('error' not in p and p.get('fetched_at') for p in pages):
            save_credentials(base, email, token)
            print('Доступы сохранены в ' + credential_store_label() + ' для следующих запусков.')
        return pages
    raise RuntimeError('Unreachable authentication state')


if __name__ == '__main__':
    from local_export import main
    raise SystemExit(main())
