"""Synthetic fixtures only: these tests never read the user's exports."""
import json
import io
import contextlib
import sys
import subprocess
import os
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import local_export as app
import fetch_pages as fetch
import windows_credentials as vault


def page(text='Неявка за 40 минут до вылета.', **extra):
    return dict(page_id='123', title='Demo Air (ZZ)',
                url='https://example.invalid/wiki/spaces/X/pages/123/Demo',
                full_text='ИАТА\nZZ\nНеявка\n' + text,
                expands=[{'title': 'Неявка', 'text': text}], **extra)


class ExtractTests(unittest.TestCase):
    def test_explicit_departure_and_checkin_events(self):
        for text, kind, minutes in (
            ('С момента вылета.', 'at_departure', 0),
            ('После вылета', 'after_departure', None),
            ('С момента закрытия регистрации', 'checkin_close', None),
            ('За 2 часа до вылета', 'before_departure', 120),
        ):
            with self.subTest(text=text):
                r = app.analyze_page(page(text))
                self.assertEqual(r['type'], kind)
                self.assertEqual(r['minutes'], minutes)

    def test_penalty_deadline_is_not_noshow_definition(self):
        p = page('')
        p['expands'] = [{'title': 'Возврат при неявке', 'text': 'За 40 минут до вылета.'}]
        r = app.analyze_page(p)
        self.assertIsNone(r['minutes'])
        self.assertEqual(r['type'], 'review')

    def test_table_field_is_used_without_flattening_adjacent_fields(self):
        p = page('')
        p['storage_html'] = '<table><tr><th>Неявка</th><td>За 40 минут до вылета.</td></tr><tr><th>Возврат</th><td>За 60 минут другой штраф.</td></tr></table>'
        r = app.analyze_page(p)
        self.assertEqual(r['minutes'], 40)
        self.assertNotIn('60', r['evidence']['noshow']['text'])

    def test_emoji_chunks_respect_excel_utf16_limit(self):
        text = '\U0001f680' * 20000
        chunks = app.cell_chunks(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 30000 for c in chunks))

    def test_exact_definition_has_minutes(self):
        """Only a whole, unqualified definition can become a numeric rule."""
        record = app.analyze_page(page())
        self.assertEqual(record['minutes'], 40)
        self.assertEqual(record['type'], 'before_departure')
        self.assertEqual(record['iata'], 'ZZ')

    def test_conditional_rules_are_not_replaced_with_one_number(self):
        """Different flight/tariff conditions must remain source text for review."""
        for text in ('Неявка за 40 минут до вылета, кроме тарифа FLEX.',
                     'Для внутренних рейсов за 40 минут, для международных за 60 минут до вылета.',
                     'Неявка не наступает за 40 минут до вылета.',
                     'Штраф за возврат за 40 минут до вылета.'):
            with self.subTest(text=text):
                record = app.analyze_page(page(text))
                self.assertIsNone(record['minutes'])
                self.assertEqual(record['type'], 'review')
                self.assertIn(text, record['evidence']['noshow']['text'])

    def test_missing_is_not_a_permitted_rule(self):
        record = app.analyze_page(page('',))
        self.assertEqual(record['type'], 'unknown')
        self.assertIsNone(record['minutes'])

    def test_other_sections_are_only_mentions(self):
        p = page('')
        p['full_text'] = 'Возврат\nПри неявке штраф 40 EUR.'
        p['expands'] = [{'title': 'Возврат', 'text': 'При неявке штраф 40 EUR.'}]
        r = app.analyze_page(p)
        self.assertEqual(r['type'], 'review')
        self.assertIsNone(r['minutes'])
        self.assertEqual(r['evidence']['noshow']['origin'], 'Упоминания; проверить контекст')

    def test_long_evidence_is_not_cut(self):
        text = 'Тестовая оговорка. ' * 3000
        r = app.analyze_page(page(text))
        self.assertEqual(r['evidence']['noshow']['text'], text)
        chunks = app.cell_chunks(text)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(c) <= 30000 for c in chunks))

    def test_duplicate_section_titles_preserve_both_rules(self):
        p = page()
        p['expands'].append({'title': 'Неявка', 'text': 'Неявка за 60 минут до вылета.'})
        r = app.analyze_page(p)
        self.assertIsNone(r['minutes'])
        self.assertIn('40 минут', r['evidence']['noshow']['text'])
        self.assertIn('60 минут', r['evidence']['noshow']['text'])

    def test_failed_page_stays_in_report(self):
        r = app.analyze_page({'page_id': '999', 'url': 'https://example.invalid',
                              'error': 'HTTP 403', 'detail': 'PRIVATE_ERROR_BODY'})
        self.assertEqual(r['type'], 'error')
        self.assertNotIn('PRIVATE_ERROR_BODY', json.dumps(r))

    def test_duplicate_page_ids_rejected(self):
        with self.assertRaises(app.ExportError):
            app.validate_pages({'pages': [page(), page()]})

    def test_empty_and_malformed_exports_rejected(self):
        for obj in ({'pages': []}, {'pages': ['secret']}, {'pages': [{}]}, {}):
            with self.subTest(obj=obj), self.assertRaises(app.ExportError):
                app.validate_pages(obj)

    def test_change_comparison_uses_content(self):
        old, new = page(), page('Неявка за 60 минут до вылета.')
        self.assertEqual(app.compare_pages([old], None)[0][2], 'Нет предыдущей выгрузки')
        changes = app.compare_pages([new], [old])
        self.assertEqual(changes[0][2], 'Изменена')
        self.assertEqual(app.compare_pages([old], [old]), [])


class FetchTests(unittest.TestCase):
    def test_report_network_guards_block_before_connection(self):
        python = subprocess.run([sys.executable, '-X', 'utf8', '-c',
            "import local_export,socket; local_export.block_network(); "
            "socket.getaddrinfo('example.invalid',443)"], cwd=app.ROOT, capture_output=True, text=True)
        self.assertNotEqual(python.returncode, 0)
        self.assertIn('ExportError', python.stderr)
        node, _ = app.runtime_paths()
        result = subprocess.run([str(node), '--input-type=module', '-e',
            "import './_local/network_off.mjs'; import net from 'node:net'; net.connect(443,'example.invalid');"],
            cwd=app.ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('NETWORK_DISABLED_FOR_LOCAL_REPORTS', result.stderr)

    def test_every_download_requests_new_content_and_keeps_version(self):
        """A preexisting output must not cause any page to be served from cache."""
        with tempfile.TemporaryDirectory() as folder:
            links, output = Path(folder) / 'links.txt', Path(folder) / 'pages.json'
            links.write_text('https://example.invalid/wiki/pages/123\n', encoding='utf-8')
            output.write_text('{"pages": [{"page_id": "123", "title": "STALE"}]}', encoding='utf-8')
            opener = Mock()
            body = {'id': '123', 'title': 'FRESH', 'body': {'storage': {'value': '<p>NEW</p>'}},
                    'version': {'number': 7, 'when': '2026-08-28'}}
            opener.open.side_effect = [io.BytesIO(json.dumps(body).encode()), io.BytesIO(json.dumps(body).encode())]
            with patch.object(fetch, 'build_opener', return_value=opener):
                first = fetch.download(links, output, email='synthetic', token='synthetic')
                second = fetch.download(links, output, email='synthetic', token='synthetic')
            self.assertEqual(opener.open.call_count, 2)
            self.assertEqual(first[0]['title'], 'FRESH')
            self.assertEqual(second[0]['version'], 7)
            self.assertTrue(second[0]['fetched_at'])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header('Cache-control'), 'no-cache')
            self.assertNotIn('synthetic', output.read_text(encoding='utf-8'))

    def test_partial_failure_blocks_finished_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch('fetch_pages.credentials', return_value=('demo', 'secret')),
                  patch('fetch_pages.load_links', return_value=['https://example.invalid/wiki/pages/123']),
                  patch.dict(os.environ, {'CONF_BASE_URL': 'https://example.invalid'}),
                  patch('fetch_pages.download', return_value=[page(error='HTTP 403')]),
                  patch.object(app, 'write_reports') as writer,
                  patch.object(sys, 'argv', ['export', '--output-dir', folder]),
                  contextlib.redirect_stdout(io.StringIO()) as log):
                self.assertEqual(app.main(), 1)
            writer.assert_not_called()
            self.assertNotIn('secret', log.getvalue())
            self.assertEqual(len(list(Path(folder).glob('*/INCOMPLETE.txt'))), 1)

    def test_auth_failure_marks_all_unfetched_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            links, output = Path(folder) / 'links.txt', Path(folder) / 'pages.json'
            links.write_text('https://example.invalid/wiki/pages/123\nhttps://example.invalid/wiki/pages/456', encoding='utf-8')
            opener = Mock()
            opener.open.side_effect = fetch.HTTPError('', 403, 'PRIVATE_SERVER_TEXT', {}, None)
            with patch.object(fetch, 'build_opener', return_value=opener):
                pages = fetch.download(links, output, email='demo', token='secret')
            self.assertEqual(len(pages), 2)
            self.assertEqual(opener.open.call_count, 1)
            self.assertTrue(all('error' in p for p in pages))
            self.assertNotIn('PRIVATE_SERVER_TEXT', output.read_text(encoding='utf-8'))

    def test_old_and_partial_data_are_rejected(self):
        """Always fresh means no cached fallback and no partial finished report."""
        for pages in ([page()], [page(fetched_at='2026-08-28T12:00:00Z', error='HTTP 403')]):
            with self.subTest(pages=pages), self.assertRaises(app.ExportError):
                app.require_complete_fresh(pages)

    def test_fresh_complete_pages_are_accepted(self):
        app.require_complete_fresh([page(fetched_at='2026-08-28T12:00:00Z')])

    def test_credentials_never_read_from_scripts(self):
        self.assertFalse(hasattr(fetch, 'creds_from_neighbour_scripts'))

    def test_remote_target_is_single_https_origin(self):
        for url in ('http://example.invalid/wiki/pages/123',
                    'https://evil.invalid/wiki/pages/123',
                    'https://u:p@example.invalid/wiki/pages/123'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch.validate_link(url, 'https://example.invalid')

    def test_known_page_urls(self):
        for url in ('https://example.invalid/wiki/pages/123',
                    'https://example.invalid/wiki/viewpage.action?pageId=123'):
            self.assertEqual(fetch.validate_link(url, 'https://example.invalid'), '123')

    def test_redirect_is_not_followed(self):
        with self.assertRaises(fetch.HTTPError):
            fetch.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')

    def test_html_preserves_nested_expand(self):
        storage = '<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">Неявка</ac:parameter><ac:rich-text-body><p>За 40 минут.</p><ac:structured-macro ac:name="note"><ac:rich-text-body><p>Кроме FLEX.</p></ac:rich-text-body></ac:structured-macro></ac:rich-text-body></ac:structured-macro>'
        expands = fetch.extract_expands(storage)
        self.assertEqual(len(expands), 1)
        self.assertIn('Кроме FLEX.', expands[0]['text'])

    def test_atomic_snapshot_does_not_destroy_previous_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'pages.json'
            path.write_text('original', encoding='utf-8')
            with patch('os.replace', side_effect=OSError('synthetic')):
                with self.assertRaises(OSError):
                    fetch.save([page()], path)
            self.assertEqual(path.read_text(encoding='utf-8'), 'original')


class CredentialFlowTests(unittest.TestCase):
    """All vault operations are mocked; never read the real user's credentials."""

    def test_saved_credentials_beat_stale_environment_and_need_no_prompt(self):
        with (patch.object(fetch, 'load_credentials', return_value=('demo', 'stored')) as read,
              patch.dict(os.environ, {'CONF_EMAIL': 'old', 'CONF_TOKEN': 'expired'}, clear=True),
              patch('builtins.input') as email_prompt,
              patch('getpass.getpass') as token_prompt):
            self.assertEqual(fetch.credentials('https://example.invalid'), ('demo', 'stored'))
        read.assert_called_once_with('https://example.invalid')
        email_prompt.assert_not_called()
        token_prompt.assert_not_called()

    def test_first_use_prompts_but_does_not_save_unverified_token(self):
        with (patch.object(fetch, 'load_credentials', return_value=None),
              patch.object(fetch, 'save_credentials') as save,
              patch.dict(os.environ, {}, clear=True),
              patch('builtins.input', return_value='demo@example.invalid'),
              patch('getpass.getpass', return_value='new-token')):
            self.assertEqual(fetch.credentials('https://example.invalid'), ('demo@example.invalid', 'new-token'))
        save.assert_not_called()

    def test_force_prompt_ignores_saved_and_environment_token(self):
        with (patch.object(fetch, 'load_credentials') as read,
              patch.dict(os.environ, {'CONF_TOKEN': 'expired'}, clear=True),
              patch('builtins.input', return_value=''),
              patch('getpass.getpass', return_value='replacement')):
            self.assertEqual(fetch.credentials('https://example.invalid', force_prompt=True,
                                              email_hint='demo'), ('demo', 'replacement'))
        read.assert_not_called()

    def test_hidden_input_never_falls_back_to_echo(self):
        import getpass
        import warnings
        def fallback(*args):
            warnings.warn('synthetic', getpass.GetPassWarning)
            return 'would-echo'
        with (patch('getpass.getpass', side_effect=fallback),
              patch('builtins.input', return_value='demo'),
              self.assertRaises(ValueError)):
            fetch.credentials('https://example.invalid', force_prompt=True)

    def run_flow(self, responses, *, update=False):
        fresh = page(fetched_at='2026-08-28T12:00:00Z')
        with (patch.object(fetch, 'load_links', return_value=['https://example.invalid/wiki/pages/123']),
              patch.object(fetch, 'credentials', side_effect=[('demo', 'old'), ('demo', 'new')]) as creds,
              patch.object(fetch, 'download', side_effect=responses) as download,
              patch.object(fetch, 'save_credentials') as save,
              patch.dict(os.environ, {}, clear=True),
              contextlib.redirect_stdout(io.StringIO()) as log):
            result = fetch.download_authenticated(Path('unused'), Path('unused-output'), update_credentials=update)
        self.assertNotIn('"old"', log.getvalue())
        self.assertNotIn('"new"', log.getvalue())
        return result, creds, download, save, fresh

    def test_401_prompts_once_and_restarts_whole_download(self):
        """An expired token must not repeat forever or reuse a partial old snapshot."""
        fresh = page(fetched_at='2026-08-28T12:00:00Z')
        result, creds, download, save, _ = self.run_flow([[page(error='HTTP 401')], [fresh]])
        self.assertEqual(result, [fresh])
        self.assertEqual(download.call_count, 2)
        self.assertEqual(download.call_args_list[1].kwargs['token'], 'new')
        self.assertTrue(creds.call_args_list[1].kwargs['force_prompt'])
        save.assert_called_once_with('https://example.invalid', 'demo', 'new')

    def test_second_401_stops_and_does_not_replace_saved_credentials(self):
        result, creds, download, save, _ = self.run_flow([[page(error='HTTP 401')], [page(error='HTTP 401')]])
        self.assertEqual(download.call_count, 2)
        self.assertEqual(creds.call_count, 2)
        self.assertEqual(result[0]['error'], 'HTTP 401')
        save.assert_not_called()

    def test_403_network_error_and_429_do_not_trigger_token_rotation(self):
        for error in ('HTTP 403', 'network_error', 'HTTP 429'):
            with self.subTest(error=error):
                result, creds, download, save, _ = self.run_flow([[page(error=error)]])
                self.assertEqual(result[0]['error'], error)
                self.assertEqual(creds.call_count, 1)
                self.assertEqual(download.call_count, 1)
                save.assert_not_called()

    def test_valid_first_input_is_saved_after_complete_download(self):
        fresh = page(fetched_at='2026-08-28T12:00:00Z')
        _, creds, _, save, _ = self.run_flow([[fresh]], update=True)
        self.assertTrue(creds.call_args.kwargs['force_prompt'])
        save.assert_called_once_with('https://example.invalid', 'demo', 'old')

    def test_mixed_hosts_rejected_before_vault_access(self):
        with (patch.object(fetch, 'load_links', return_value=[
                'https://example.invalid/wiki/pages/123', 'https://other.invalid/wiki/pages/456']),
              patch.object(fetch, 'credentials') as creds,
              patch.dict(os.environ, {}, clear=True),
              self.assertRaises(ValueError)):
            fetch.download_authenticated(Path('unused'), Path('unused-output'))
        creds.assert_not_called()


class CredentialStoreTests(unittest.TestCase):
    def test_write_uses_generic_local_machine_entry_and_utf8_bytes(self):
        seen = {}
        def write(pointer, flags):
            entry = ctypes.cast(pointer, ctypes.POINTER(vault.CREDENTIALW)).contents
            seen.update(target=entry.TargetName, email=entry.UserName, type=entry.Type,
                        persist=entry.Persist, flags=flags,
                        secret=ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize))
            return True
        api = Mock()
        api.CredWriteW.side_effect = write
        with patch.object(vault, '_api', return_value=api):
            vault.save_credentials('https://example.invalid', 'demo@example.invalid', 'TEST_токен')
        self.assertEqual(seen, {'target': 'ConfluenceLocalExport:https://example.invalid',
            'email': 'demo@example.invalid', 'type': 1, 'persist': 2, 'flags': 0,
            'secret': 'TEST_токен'.encode('utf-8')})

    def test_not_found_and_denied_are_distinct(self):
        api = Mock()
        api.CredReadW.return_value = False
        with (patch.object(vault, '_api', return_value=api),
              patch.object(ctypes, 'get_last_error', return_value=1168)):
            self.assertIsNone(vault.load_credentials('https://example.invalid'))
        with (patch.object(vault, '_api', return_value=api),
              patch.object(ctypes, 'get_last_error', return_value=5),
              self.assertRaises(vault.CredentialStoreError)):
            vault.load_credentials('https://example.invalid')

    def test_oversized_secret_is_rejected_without_changing_vault(self):
        with patch.object(vault, '_api') as api, self.assertRaises(vault.CredentialStoreError):
            vault.save_credentials('https://example.invalid', 'demo', 'x' * 2561)
        api.assert_not_called()

    def test_vault_entry_is_scoped_to_confluence_origin(self):
        self.assertNotEqual(vault.target_name('https://a.invalid'), vault.target_name('https://b.invalid'))
        self.assertEqual(vault.target_name('https://A.invalid:443/'), vault.target_name('https://a.invalid'))
        for url in ('http://example.invalid', 'https://user:pass@example.invalid',
                    'https://example.invalid/other', 'https://example.invalid?token=x'):
            with self.subTest(url=url), self.assertRaises(vault.CredentialStoreError):
                vault.target_name(url)


if __name__ == '__main__':
    unittest.main()
