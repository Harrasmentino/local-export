"""Deterministic local reports from newly downloaded Confluence pages only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from windows_credentials import CredentialStoreError

ROOT = Path(__file__).resolve().parent
TOPICS = {
    'noshow': ('Неявка', r'неявк|no[\s-]?show'),
    'void': ('Войды', r'аннул[яи]ц|\bvoid\b|войд'),
    'exchange': ('Штраф при обмене', r'обмен|reissu|rebook'),
    'excess': ('Штраф выше тарифа', r'штраф.{0,50}(?:выше|больше|превыш)|(?:выше|больше).{0,25}тариф'),
    'taxes': ('Невозвратные таксы', r'невозвратн.{0,15}такс'),
}
FIELD_LABELS = re.compile(r'^(?:ИАТА|IATA|Код АК|Неявка|No[- ]?show|Аннуляция|VOID|Обмен|Возврат|'
    r'Невозвратные таксы|Багаж|Дети|Контакты|Продажа|Выписка|Изменение имени|Штраф при обмене)\s*:?$', re.I)


class ExportError(Exception):
    """Messages are fixed strings safe to display without exposing source data."""


class FieldParser(HTMLParser):
    """Keep table label/value boundaries instead of guessing from flattened HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.stack.append({'cells': [], 'active': False})
        elif self.stack and tag in ('td', 'th'):
            self.stack[-1]['cells'].append([])
            self.stack[-1]['active'] = True
        elif tag in ('br', 'p', 'div', 'li'):
            self.handle_data('\n')

    def handle_endtag(self, tag):
        if tag == 'tr' and self.stack:
            row = self.stack.pop()
            self.rows.append([''.join(c).strip() for c in row['cells']])
        elif tag in ('td', 'th') and self.stack:
            self.stack[-1]['active'] = False
        elif tag in ('p', 'div', 'li'):
            self.handle_data('\n')

    def handle_data(self, data):
        for row in self.stack:
            if row['active']:
                row['cells'][-1].append(data)


def validate_pages(data: dict) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get('pages'), list) or not data['pages']:
        raise ExportError('JSON не содержит непустой список pages.')
    seen = set()
    for p in data['pages']:
        if not isinstance(p, dict) or not p.get('page_id'):
            raise ExportError('В JSON есть страница без page_id.')
        if not isinstance(p['page_id'], (str, int)) or str(p['page_id']) in seen:
            raise ExportError('В JSON повторяются page_id или неверен их формат.')
        seen.add(str(p['page_id']))
        if any(not isinstance(p.get(k, ''), str) for k in ('title', 'url', 'full_text', 'storage_html')):
            raise ExportError('Неверный формат текстовых полей страницы.')
        if not isinstance(p.get('expands', []), list) or any(
            not isinstance(e, dict) or not isinstance(e.get('text', ''), str)
            or not isinstance(e.get('title', ''), str) for e in p.get('expands', [])):
            raise ExportError('Неверный формат expand-блоков.')
    return data['pages']


def read_pages(path: Path) -> list[dict]:
    try:
        return validate_pages(json.loads(path.read_text(encoding='utf-8-sig')))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExportError('Не удалось прочитать JSON сравнения; содержимое в лог не выводится.') from error


def evidence(page: dict, key: str) -> dict:
    pattern = TOPICS[key][1]
    def definition_label(label):
        return key != 'noshow' or bool(re.fullmatch(
            r'(?:неявка|no[ -]?show|(?:начало|момент) неявки)(?:\s*\((?:no[ -]?show|неявка)\))?\s*:?',
            label.strip(), re.I))
    direct = [e['text'] for e in page.get('expands', [])
              if re.search(pattern, e.get('title', ''), re.I) and e.get('text', '').strip()]
    definition_flags = [definition_label(e.get('title', '')) for e in page.get('expands', [])
                        if re.search(pattern, e.get('title', ''), re.I) and e.get('text', '').strip()]
    fields = FieldParser()
    fields.feed(page.get('storage_html', ''))
    for row in fields.rows:
        if len(row) >= 2 and len(row[0]) < 100 and re.search(pattern, row[0], re.I):
            direct.extend(value for value in row[1:] if value.strip())
            definition_flags.extend(definition_label(row[0]) for value in row[1:] if value.strip())
    if direct:
        unique = list(dict.fromkeys(direct))
        return {'text': '\n\n'.join(unique), 'origin': 'Тематический expand / поле таблицы',
                'direct': all(definition_flags)}
    # Historic JSON has no HTML: never infer a numeric rule from guessed boundaries.
    lines = page.get('full_text', '').splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if FIELD_LABELS.fullmatch(line.strip()) and re.search(pattern, line, re.I):
            end = next((j for j in range(i + 1, len(lines)) if FIELD_LABELS.fullmatch(lines[j].strip())), len(lines))
            block = '\n'.join(lines[i + 1:end]).strip()
            if block:
                blocks.append(block)
    if blocks:
        return {'text': '\n\n'.join(dict.fromkeys(blocks)), 'origin': 'Поле в тексте; проверить границы', 'direct': False}
    mentions = [e['text'] for e in page.get('expands', []) if re.search(pattern, e.get('text', ''), re.I)]
    if not mentions:
        indexes = {j for i, line in enumerate(lines) if re.search(pattern, line, re.I)
                   and not FIELD_LABELS.fullmatch(line.strip())
                   for j in range(max(0, i - 2), min(len(lines), i + 4))}
        mentions = ['\n'.join(lines[j] for j in sorted(indexes))] if indexes else []
    return {'text': '\n\n'.join(dict.fromkeys(mentions)),
            'origin': 'Упоминания; проверить контекст' if mentions else 'Не найдено', 'direct': False}


def analyze_page(page: dict) -> dict:
    title = page.get('title') or ('Страница ' + str(page['page_id']))
    iata = re.search(r'(?:\bIATA\b|ИАТА)\s*[:\-]?\s*([A-Z0-9]{2})\b', page.get('full_text', ''))
    iata = iata or re.search(r'\(([A-Z0-9]{2})\)\s*$', title)
    r = {'id': str(page['page_id']), 'airline': title, 'iata': iata.group(1) if iata else '',
         'url': page.get('url', ''), 'type': 'unknown', 'minutes': None,
         'moment': 'Не найдено', 'status': 'Нет данных; проверить', 'evidence': {}}
    if 'error' in page:
        r.update(type='error', moment='Страница не выгружена', status='Ошибка источника')
        r['evidence'] = {k: {'text': '', 'origin': 'Ошибка источника', 'direct': False} for k in TOPICS}
        return r
    r['evidence'] = {k: evidence(page, k) for k in TOPICS}
    ns = r['evidence']['noshow']
    if ns['text']:
        r.update(type='review', moment='Нужна проверка', status='Нужна проверка исходной формулировки')
        # Whole definition only: no qualifiers, negation or generic refund deadlines.
        match = re.fullmatch(r'(?:(?:неявка|no[ -]?show)\s*(?:[:—-]\s*)?(?:наступает\s+)?)?за\s+'
                            r'(\d{1,4})\s+(минут\w*|час\w*)\s+до\s+(?:вылета|отправления)\s*\.?',
                            ns['text'].strip(), re.I)
        if ns['direct'] and match:
            minutes = int(match.group(1)) * (60 if match.group(2).lower().startswith('час') else 1)
            r.update(type='before_departure', minutes=minutes, moment=f'За {minutes} мин до вылета',
                     status='Явная формулировка; не экспертная проверка')
        elif ns['direct']:
            normalized = re.sub(r'\s+', ' ', ns['text']).strip().rstrip('.').casefold()
            events = {
                'с момента вылета': ('at_departure', 0, 'С момента вылета'),
                'в момент вылета': ('at_departure', 0, 'В момент вылета'),
                'после вылета': ('after_departure', None, 'После вылета'),
                'с момента закрытия регистрации': ('checkin_close', None, 'С момента закрытия регистрации'),
                'после закрытия регистрации': ('checkin_close', None, 'После закрытия регистрации'),
            }
            if normalized in events:
                kind, minutes, moment = events[normalized]
                r.update(type=kind, minutes=minutes, moment=moment,
                         status='Явная формулировка; не экспертная проверка')
    return r


def cell_chunks(text: str) -> list[str]:
    # Excel's limit is in UTF-16 code units, not Python Unicode characters.
    chunks, part, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 30000:
            chunks.append(''.join(part))
            part, units = [], 0
        part.append(char)
        units += size
    if part or not chunks:
        chunks.append(''.join(part))
    return chunks


def fingerprint(page: dict) -> str:
    content = {k: page.get(k) for k in ('title', 'full_text', 'expands', 'error')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compare_pages(current: list[dict], previous: list[dict] | None) -> list[list]:
    if previous is None:
        return [['', '', 'Нет предыдущей выгрузки', 'Сравнение не выполнялось']]
    before = {str(p['page_id']): p for p in previous}
    after = {str(p['page_id']): p for p in current}
    rows = []
    for key in dict.fromkeys([*after, *before]):
        p = after.get(key, before.get(key))
        change = ('Добавлена' if key not in before else 'Отсутствует в новой выгрузке' if key not in after
                  else 'Изменена' if fingerprint(before[key]) != fingerprint(after[key]) else '')
        if change:
            rows.append([p.get('title', ''), key, change, p.get('url', '')])
    return rows


def excerpt(text: str) -> str:
    if not text:
        return 'Не найдено'
    return text if len(text) <= 400 else text[:360] + '\n[Полностью: «Исходные фрагменты» и source_snapshot.json]'


def sheet(name, headers, rows, widths=None):
    return {'name': name, 'headers': headers, 'rows': rows, 'widths': widths}


def make_payload(pages: list[dict], previous: list[dict] | None = None) -> dict:
    records = [analyze_page(p) for p in pages]
    summary, voids, exchanges, excess, nsrows, source_rows = [], [], [], [], [], []
    for r in records:
        e, prefix = r['evidence'], [r['airline'], r['iata']]
        summary.append(prefix + [r['moment'], excerpt(e['void']['text']), excerpt(e['exchange']['text']),
                                 excerpt(e['excess']['text']), r['status'], r['url']])
        voids.append(prefix + [excerpt(e['void']['text']), e['void']['origin'], r['url']])
        exchanges.append(prefix + [excerpt(e['exchange']['text']), e['exchange']['origin'], r['url']])
        excess.append(prefix + [excerpt(e['excess']['text']), excerpt(e['taxes']['text']), r['url']])
        nsrows.append(prefix + [r['moment'], r['type'], r['minutes'], r['status'],
                               excerpt(e['noshow']['text']), e['noshow']['origin'], r['url']])
        for key, value in e.items():
            if value['text']:
                for part, chunk in enumerate(cell_chunks(value['text']), 1):
                    source_rows.append([r['airline'], r['id'], TOPICS[key][0], part, chunk, value['origin'], r['url']])
    notes = [
        ['Метод', 'Локальный поиск по заголовкам и ключевым словам. ИИ не используется. Формулировки не являются экспертной проверкой.'],
        ['Не найдено', 'Отсутствие совпадения не означает отсутствия правила. Проверьте статью.'],
        ['Неявка', 'Число минут заполняется только для простой явной формулировки целиком. Условия и исключения требуют ручной проверки.'],
        ['Открытые источники и ФАП', 'Не обновлялись и не включены в новые выводы. Предыдущие файлы остаются отдельным архивом.'],
        ['Полный текст', 'Сводки показывают фрагменты. Полный найденный текст — на листе «Исходные фрагменты», полные страницы — source_snapshot.json.'],
        ['Актуальность', 'При каждом запуске все страницы скачиваются заново. Ошибка любой страницы блокирует отчёты. Время загрузки и версии — source_snapshot.json.'],
        ['Изменения', 'Сравнивается текст с предыдущим JSON, если он задан. Старый JSON никогда не заменяет свежие страницы.'],
        ['Конфиденциальность', 'Файлы содержат исходные данные. Не отправляйте их ИИ. Синхронизация папок ОС настраивается отдельно.'],
    ]
    ns = sheet('Неявка', ['Авиакомпания', 'ИАТА', 'Момент неявки', 'type (для кода)',
         'minutes_before_departure', 'Надёжность', 'Формулировка из статьи', 'Откуда', 'Ссылка'], nsrows,
         [25, 10, 26, 22, 24, 38, 65, 35, 45])
    sources = sheet('Исходные фрагменты', ['Авиакомпания', 'page_id', 'Тема', 'Часть', 'Полный фрагмент', 'Откуда', 'Ссылка'],
                    source_rows, [25, 14, 26, 10, 90, 35, 45])
    how = sheet('Как читать', ['Поле', 'Пояснение'], notes, [32, 110])
    groups = [[t, None, None] for t in sorted({r['moment'] for r in records})]
    distribution = sheet('Распределение', ['Момент неявки', 'Сколько АК', 'Доля'], groups, [38, 18, 18])
    distribution['distribution'] = True
    return {'records': records, 'workbooks': [
        {'filename': 'aviakompanii_svodka.xlsx', 'sheets': [
            sheet('Сводка', ['Авиакомпания', 'ИАТА', 'Момент неявки', 'Войд — исходный фрагмент',
                  'Штраф при обмене', 'Если штраф выше тарифа', 'Статус неявки', 'Ссылка'], summary,
                  [25, 10, 26, 60, 60, 60, 38, 45]),
            sheet('Войды', ['Авиакомпания', 'ИАТА', 'Правила аннуляции (VOID)', 'Откуда', 'Ссылка'], voids, [25, 10, 80, 35, 45]),
            sheet('Штраф при обмене', ['Авиакомпания', 'ИАТА', 'Формулировка из статьи', 'Откуда', 'Ссылка'], exchanges, [25, 10, 80, 35, 45]),
            sheet('Штраф выше тарифа', ['Авиакомпания', 'ИАТА', 'Правило из статьи', 'Невозвратные таксы', 'Ссылка'], excess, [25, 10, 65, 65, 45]),
            ns, sheet('Что изменилось', ['Авиакомпания', 'page_id', 'Изменение', 'Ссылка / пояснение'], compare_pages(pages, previous), [25, 15, 36, 70]), sources, how]},
        {'filename': 'neyavka.xlsx', 'sheets': [ns, distribution,
            {**sources, 'rows': [row for row in source_rows if row[2] == 'Неявка']}, how]}]}


def block_network() -> None:
    def audit(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto'):
            raise ExportError('Сеть запрещена на этапе формирования отчётов.')
    sys.addaudithook(audit)


def runtime_paths() -> tuple[Path, Path]:
    base = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies'
    node = Path(os.environ.get('LOCAL_EXPORT_NODE', str(base / 'node/bin/node.exe')))
    modules = Path(os.environ.get('LOCAL_EXPORT_MODULES', str(base / 'node/node_modules')))
    if not node.is_file() or not (modules / '@oai/artifact-tool/package.json').is_file():
        raise ExportError('Не найден локальный Node.js / artifact-tool. См. README_LOCAL.md; автоматической установки нет.')
    return node, modules


def write_reports(payload: dict, folder: Path) -> None:
    from local_documents import write_documents
    node, modules = runtime_paths()
    junction = ROOT / '_local/node_modules'
    if not junction.exists() or junction.resolve() != modules.resolve():
        raise ExportError('Нет корректной локальной ссылки на библиотеки. Запустите VYGRUZKA.bat.')
    source = folder / 'report_data.json'
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in
           ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY', 'CONF_EMAIL', 'NODE_OPTIONS'))}
    proc = subprocess.run([str(node), str(ROOT / '_local/export_xlsx.mjs'), str(source), str(folder)],
                          cwd=ROOT, env=env, capture_output=True, timeout=600)
    if proc.returncode:
        raise ExportError('Не удалось сформировать Excel. Содержимое ошибок библиотеки скрыто для защиты данных.')
    write_documents(payload['records'], folder)


def require_complete_fresh(pages: list[dict]) -> None:
    validate_pages({'pages': pages})
    if any('error' in p or not p.get('fetched_at') for p in pages):
        raise ExportError('Не все страницы получены заново. Отчёты НЕ созданы; старые данные НЕ использованы.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Свежие отчёты из Confluence без передачи данных ИИ.')
    parser.add_argument('--output-dir', type=Path, help='Родительская папка; каждый запуск создаёт отдельный каталог.')
    parser.add_argument('--previous', type=Path, help='Старый JSON только для сравнения изменений.')
    parser.add_argument('--links', type=Path, default=ROOT / 'links.txt')
    parser.add_argument('--update-credentials', action='store_true', help='Ввести новые доступы вместо сохранённых.')
    args = parser.parse_args()
    folder = None
    try:
        runtime_paths()
        parent = args.output_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ConfluenceLocalExport/exports'
        folder = parent.resolve() / (datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + '_' + uuid.uuid4().hex[:6])
        folder.mkdir(parents=True, exist_ok=False)
        (folder / 'INCOMPLETE.txt').write_text('Сборка не завершена. Не использовать как готовый отчёт.', encoding='utf-8')
        previous = read_pages(args.previous) if args.previous else None
        from fetch_pages import download_authenticated
        print('Загрузка ВСЕХ страниц заново. Только Confluence; без ИИ и без кэша.')
        pages = download_authenticated(args.links, folder / 'source_snapshot.json',
                                       update_credentials=args.update_credentials)
        require_complete_fresh(pages)
        block_network()
        print(f'Локальная обработка: {len(pages)} страниц. Сеть заблокирована.')
        payload = make_payload(pages, previous)
        write_reports(payload, folder)
        info = {'mode': 'fresh_only', 'pages': len(pages), 'source_errors': 0,
                'built_at': datetime.now().astimezone().isoformat(),
                'needs_review': sum(r['type'] in ('review', 'unknown') for r in payload['records']),
                'network_during_report': 'blocked', 'ai_used': False}
        (folder / 'report_info.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        (folder / 'INCOMPLETE.txt').unlink()
        print(f'Сформировано: 2 Excel и 2 Word. Требуют проверки: {info["needs_review"]}.')
        print('Результаты: ' + str(folder))
        return 0
    except (ExportError, CredentialStoreError) as error:
        print('Ошибка: ' + str(error))
    except (OSError, ValueError, EOFError, ImportError, subprocess.SubprocessError):
        print('Ошибка доступа, записи или библиотек. См. README_LOCAL.md. Исходные данные в лог не выводятся.')
    except KeyboardInterrupt:
        print('Остановлено. Незавершённая папка помечена INCOMPLETE.txt.')
    if folder:
        print('Незавершённые результаты: ' + str(folder))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
