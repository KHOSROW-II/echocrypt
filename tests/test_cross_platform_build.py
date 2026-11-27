import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from core import platform_paths
from scripts.build_browser_extensions import build

class PlatformBuildTests(unittest.TestCase):
    def test_user_directories_are_platform_specific(self):
        with patch.object(platform_paths.sys, "platform", "win32"), patch.dict(os.environ, {"APPDATA": "C:/Roaming", "LOCALAPPDATA": "C:/Local"}):
            self.assertEqual(platform_paths.user_config_dir().name, "ECHOCRYPT")
            self.assertEqual(platform_paths.user_config_dir().parent.name, "Roaming")
            self.assertEqual(platform_paths.user_data_dir().name, "ECHOCRYPT")
            self.assertEqual(platform_paths.user_data_dir().parent.name, "Local")
        with patch.object(platform_paths.sys, "platform", "darwin"):
            self.assertIn("Library/Application Support/ECHOCRYPT", str(platform_paths.user_config_dir()))
    def test_extension_packages_are_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            for browser in ("chrome", "edge", "firefox"):
                path = build(browser, Path(temp))
                with zipfile.ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    manifest = json.loads(archive.read("echocrypt/manifest.json"))
                    if browser == "firefox":
                        self.assertIn("gecko", manifest["browser_specific_settings"])
                    else:
                        self.assertEqual(manifest["manifest_version"], 3)

if __name__ == "__main__":
    unittest.main()
