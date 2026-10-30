import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_shell_start import credentials


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config" / "telegram.json"

    def test_first_input_saved_privately_and_reused_without_prompt(self):
        with patch("getpass.getpass", return_value="fake-token"), patch("builtins.input", return_value="123"), patch("builtins.print"):
            self.assertEqual(credentials(self.path), ("fake-token", "123"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with patch("getpass.getpass", side_effect=AssertionError), patch("builtins.input", side_effect=AssertionError), patch("builtins.print"):
            self.assertEqual(credentials(self.path), ("fake-token", "123"))

    def test_reset_replaces_existing_login(self):
        with patch("getpass.getpass", return_value="old"), patch("builtins.input", return_value="123"), patch("builtins.print"):
            credentials(self.path)
        with patch("getpass.getpass", return_value="new"), patch("builtins.input", return_value="456"), patch("builtins.print"):
            credentials(self.path, reset=True)
        self.assertEqual(json.loads(self.path.read_text()), {"token": "new", "owner": "456"})

    def test_invalid_input_does_not_write(self):
        with patch("getpass.getpass", return_value="token"), patch("builtins.input", return_value="not-number"):
            with self.assertRaises(SystemExit):
                credentials(self.path)
        self.assertFalse(self.path.exists())

    def test_corrupt_file_error_does_not_expose_contents(self):
        self.path.parent.mkdir()
        self.path.write_text("sensitive-invalid-content")
        with self.assertRaises(SystemExit) as error:
            credentials(self.path)
        self.assertNotIn("sensitive", str(error.exception))
        self.assertIn("--reset-login", str(error.exception))
