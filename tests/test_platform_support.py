"""Cross-platform paths, credential dispatch and launchers; no real vault access."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import credential_store
import platform_paths


class PlatformPathTests(unittest.TestCase):
    def test_macos_uses_application_support(self):
        with (patch.object(platform_paths.sys, 'platform', 'darwin'),
              patch.object(platform_paths.Path, 'home', return_value=Path('/Users/demo'))):
            self.assertEqual(platform_paths.app_data_dir(),
                             Path('/Users/demo/Library/Application Support/ConfluenceLocalExport'))

    def test_windows_keeps_local_appdata(self):
        with (patch.object(platform_paths.sys, 'platform', 'win32'),
              patch.dict(os.environ, {'LOCALAPPDATA': r'C:\Private'}, clear=False)):
            self.assertEqual(platform_paths.app_data_dir(), Path(r'C:\Private\ConfluenceLocalExport'))


class CredentialDispatchTests(unittest.TestCase):
    def test_macos_selects_keychain_backend(self):
        backend = Mock()
        with (patch.object(credential_store.sys, 'platform', 'darwin'),
              patch.object(credential_store, 'import_module', return_value=backend) as imported):
            credential_store.save_credentials('https://example.invalid', 'demo@example.invalid', 'SECRET')
        imported.assert_called_once_with('macos_credentials')
        backend.save_credentials.assert_called_once_with(
            'https://example.invalid', 'demo@example.invalid', 'SECRET')

    def test_windows_selects_wincred_backend(self):
        backend = Mock()
        with (patch.object(credential_store.sys, 'platform', 'win32'),
              patch.object(credential_store, 'import_module', return_value=backend) as imported):
            credential_store.load_credentials('https://example.invalid')
        imported.assert_called_once_with('windows_credentials')
        backend.load_credentials.assert_called_once_with('https://example.invalid')

    def test_unsupported_platform_has_no_plaintext_fallback(self):
        with (patch.object(credential_store.sys, 'platform', 'linux'),
              self.assertRaises(credential_store.CredentialStoreError)):
            credential_store.load_credentials('https://example.invalid')


class MacLauncherTests(unittest.TestCase):
    def test_agent_launcher_uses_python3_without_secret_arguments(self):
        launcher = Path('CONFLUENCE_AGENT.sh')
        self.assertTrue(launcher.exists())
        text = launcher.read_text(encoding='utf-8')
        self.assertIn('python3', text)
        self.assertNotIn('CONF_TOKEN', text)
        self.assertNotIn('--token', text)


if __name__ == '__main__':
    unittest.main()