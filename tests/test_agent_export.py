"""Local connection window + fresh card inventory; no real credentials or network."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import agent_export as agent
from connection_window import ConnectionInput

DATABASE = 'https://example.invalid/wiki/spaces/TEST/database/42'
SOURCE = {'database_url': DATABASE, 'base_url': 'https://example.invalid', 'database_id': '42'}


def card(pid='123'):
    return {'page_id': pid, 'title': 'Demo Air ' + pid,
            'url': 'https://example.invalid/wiki/pages/viewpage.action?pageId=' + pid}


def inventory_result(ids, next_link=None):
    result = {'results': [{'content': {'id': pid, 'type': 'page', 'status': 'current',
               'title': 'Demo Air ' + pid, 'ancestors': [{'id': '42', 'type': 'database'}]}}
               for pid in ids], '_links': {}}
    if next_link:
        result['_links']['next'] = next_link
    return io.BytesIO(json.dumps(result).encode())


class InventoryTests(unittest.TestCase):
    def test_database_url_drops_tracking_but_rejects_unsafe_targets(self):
        self.assertEqual(agent.parse_source(DATABASE + '?xpis=tracking#view'), SOURCE)
        for value in ('http://example.invalid/wiki/database/42',
                      'https://u:p@example.invalid/wiki/database/42',
                      'https://example.invalid/wiki/pages/42',
                      'https://example.invalid:123/wiki/database/42'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.parse_source(value)

    def test_new_cards_are_discovered_across_every_cursor_page(self):
        """No stored links list: a new card on the next API page must be included."""
        opener = Mock()
        opener.open.side_effect = [inventory_result(['123'], '/wiki/rest/api/search?cursor=next'),
                                   inventory_result(['456'])]
        with patch.object(agent, 'build_opener', return_value=opener):
            cards = agent.discover_cards(SOURCE, 'demo', 'SYNTHETIC_SECRET')
        self.assertEqual([p['page_id'] for p in cards], ['123', '456'])
        query = parse_qs(urlsplit(opener.open.call_args.args[0].full_url).query)
        self.assertEqual(query['cql'], ['type = page AND parent = 42'])
        self.assertEqual(query['cursor'], ['next'])
        self.assertEqual(opener.open.call_args.args[0].get_header('Cache-control'), 'no-cache')

    def test_external_pagination_never_receives_credentials(self):
        opener = Mock()
        opener.open.return_value = inventory_result(['123'], 'https://evil.invalid/wiki/rest/api/search?cursor=x')
        with patch.object(agent, 'build_opener', return_value=opener), self.assertRaises(agent.AgentExportError):
            agent.discover_cards(SOURCE, 'demo', 'SYNTHETIC_SECRET')
        self.assertEqual(opener.open.call_count, 1)

    def test_duplicate_cards_or_wrong_parent_fail_instead_of_claiming_completeness(self):
        for response in (inventory_result(['123', '123']),
                         io.BytesIO(json.dumps({'results': [{'content': {'id': '123', 'type': 'page',
                           'status': 'current', 'title': 'Demo', 'ancestors': [{'id': '99'}]}}]}).encode())):
            opener = Mock()
            opener.open.return_value = response
            with patch.object(agent, 'build_opener', return_value=opener), self.assertRaises(agent.AgentExportError):
                agent.discover_cards(SOURCE, 'demo', 'SYNTHETIC_SECRET')

    def test_repeated_cursor_stops(self):
        opener = Mock()
        opener.open.side_effect = [inventory_result(['123'], '/wiki/rest/api/search?cursor=same'),
                                   inventory_result(['456'], '/wiki/rest/api/search?cursor=same')]
        with patch.object(agent, 'build_opener', return_value=opener), self.assertRaises(agent.AgentExportError):
            agent.discover_cards(SOURCE, 'demo', 'SYNTHETIC_SECRET')
        self.assertEqual(opener.open.call_count, 2)

    def test_http_errors_are_safe_and_429_is_bounded(self):
        opener = Mock()
        opener.open.side_effect = agent.HTTPError('', 429, 'PRIVATE_BODY', {'Retry-After': '999'}, None)
        with (patch.object(agent, 'build_opener', return_value=opener),
              patch.object(agent.time, 'sleep') as sleep,
              self.assertRaises(agent.SourceHTTPError) as caught):
            agent.discover_cards(SOURCE, 'demo', 'SYNTHETIC_SECRET')
        self.assertEqual(caught.exception.status, 429)
        self.assertNotIn('PRIVATE_BODY', str(caught.exception))
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [60, 60])


class LocalAgentFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'connection.json'
        self.folder = self.root / 'run'
        self.log = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(agent, 'load_credentials', return_value=('demo', 'SAVED_SECRET')).start()
        self.save = patch.object(agent, 'save_credentials').start()
        self.prompt = patch.object(agent, 'show_connection', return_value=ConnectionInput(
            DATABASE, 'demo', 'NEW_SECRET')).start()
        self.discover = patch.object(agent, 'discover_cards', return_value=[card()]).start()
        self.download = patch.object(agent, 'download', side_effect=self.fake_download).start()

    def fake_download(self, links, output, **kwargs):
        urls = Path(links).read_text(encoding='utf-8').splitlines()
        pages = []
        for url in urls:
            pid = parse_qs(urlsplit(url).query)['pageId'][0]
            pages.append({**card(pid), 'full_text': 'Свежие условия обмена.',
                          'storage_html': '<p>Свежие условия обмена.</p>',
                          'expands': [{'title': 'Обмен', 'text': 'Свежие условия обмена.'}],
                          'fetched_at': '2026-08-28T12:00:00Z', 'version': 3})
        Path(output).write_text(json.dumps({'pages': pages}), encoding='utf-8')
        return pages

    def configure(self):
        self.config.write_text(json.dumps({'database_url': DATABASE}), encoding='utf-8')

    def run_export(self, **kwargs):
        with contextlib.redirect_stdout(self.log):
            return agent.prepare(self.config, self.folder, **kwargs)

    def test_first_run_opens_local_window_and_keeps_credentials_out_of_files(self):
        self.run_export()
        self.prompt.assert_called_once()
        self.save.assert_called_once_with('https://example.invalid', 'demo', 'NEW_SECRET')
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn('NEW_SECRET', path.read_text(encoding='utf-8'))
        self.assertNotIn('NEW_SECRET', self.log.getvalue())
        self.assertFalse((self.folder / 'INCOMPLETE.txt').exists())
        manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['downloaded_pages'], 1)
        index = json.loads((self.folder / 'index.json').read_text(encoding='utf-8'))
        saved_page = json.loads((self.folder / index['pages'][0]['file']).read_text(encoding='utf-8'))
        self.assertEqual(saved_page['full_text'], 'Свежие условия обмена.')

    def test_saved_connection_uses_no_window_and_no_console_input(self):
        self.configure()
        with patch('builtins.input') as console_input:
            self.run_export()
        self.prompt.assert_not_called()
        console_input.assert_not_called()
        self.assertEqual(self.download.call_args.kwargs['token'], 'SAVED_SECRET')

    def test_401_during_inventory_requests_replacement_locally_and_restarts(self):
        self.configure()
        self.discover.side_effect = [agent.SourceHTTPError(401), [card()], [card()]]
        self.run_export()
        self.assertEqual(self.discover.call_count, 3)
        self.prompt.assert_called_once()
        self.assertTrue(self.prompt.call_args.kwargs['lock_source'])
        self.save.assert_called_once_with('https://example.invalid', 'demo', 'NEW_SECRET')

    def test_401_during_card_download_rediscovers_all_cards(self):
        self.configure()
        def download_again(*args, **kwargs):
            if download_again.first:
                download_again.first = False
                return [{**card(), 'error': 'HTTP 401'}]
            return self.fake_download(*args, **kwargs)
        download_again.first = True
        self.download.side_effect = download_again
        self.run_export()
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(self.discover.call_count, 3)
        self.assertEqual(self.download.call_args.kwargs['token'], 'NEW_SECRET')

    def test_second_401_preserves_old_vault_record(self):
        self.configure()
        self.discover.side_effect = agent.SourceHTTPError(401)
        with self.assertRaises(agent.SourceHTTPError):
            self.run_export()
        self.assertEqual(self.discover.call_count, 2)
        self.prompt.assert_called_once()
        self.save.assert_not_called()
        self.assertTrue((self.folder / 'INCOMPLETE.txt').exists())

    def test_403_does_not_open_replacement_window(self):
        self.configure()
        self.discover.side_effect = agent.SourceHTTPError(403)
        with self.assertRaises(agent.SourceHTTPError):
            self.run_export()
        self.prompt.assert_not_called()
        self.save.assert_not_called()

    def test_changing_membership_does_not_publish_partial_bundle(self):
        self.configure()
        self.discover.side_effect = [[card()], [card(), card('456')]]
        with self.assertRaises(agent.AgentExportError):
            self.run_export()
        self.assertTrue((self.folder / 'INCOMPLETE.txt').exists())
        self.assertFalse((self.folder / 'manifest.json').exists())
        self.save.assert_not_called()

    def test_new_run_uses_new_inventory_instead_of_previous_links(self):
        self.configure()
        self.run_export()
        self.folder = self.root / 'second-run'
        self.discover.return_value = [card(), card('456')]
        self.run_export()
        index = json.loads((self.folder / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual([p['page_id'] for p in index['pages']], ['123', '456'])

    def test_cancel_does_not_fall_back_to_chat_or_plaintext(self):
        self.prompt.return_value = None
        with self.assertRaises(agent.AgentExportError):
            self.run_export()
        self.discover.assert_not_called()
        self.save.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_partial_card_download_blocks_publication(self):
        self.configure()
        self.download.side_effect = None
        self.download.return_value = [{**card(), 'error': 'HTTP 404'}]
        with self.assertRaises(agent.AgentExportError):
            self.run_export()
        self.assertFalse((self.folder / 'manifest.json').exists())
        self.save.assert_not_called()

    def test_secret_is_not_in_connection_repr(self):
        self.assertNotIn('HIDDEN', repr(ConnectionInput(DATABASE, 'demo', 'HIDDEN')))

    def test_missing_credentials_open_window_even_when_source_is_saved(self):
        self.configure()
        with patch.object(agent, 'load_credentials', return_value=None):
            self.run_export()
        self.prompt.assert_called_once()
        self.assertEqual(self.prompt.call_args.kwargs['database_url'], DATABASE)

    def test_explicit_configure_opens_window_without_reading_token_from_environment(self):
        self.configure()
        with patch.dict('os.environ', {'CONF_EMAIL': 'environment', 'CONF_TOKEN': 'ENV_SECRET'}):
            self.run_export(configure=True)
        self.prompt.assert_called_once()
        self.assertFalse(self.prompt.call_args.kwargs['lock_source'])
        self.assertEqual(self.download.call_args.kwargs['token'], 'NEW_SECRET')

    def test_vault_write_failure_keeps_bundle_incomplete(self):
        self.configure()
        self.save.side_effect = agent.CredentialStoreError('Synthetic denied write')
        with self.assertRaises(agent.CredentialStoreError):
            self.run_export()
        self.assertTrue((self.folder / 'INCOMPLETE.txt').exists())
        self.assertFalse((self.folder / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
