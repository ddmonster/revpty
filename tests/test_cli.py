import json
import os
import shutil
import sys
import tempfile
import unittest

from revpty.cli.main import convert_to_ws_url, load_config, _resolve_executable


class ConvertToWsUrlTests(unittest.TestCase):
    def test_http_to_ws(self):
        self.assertEqual(convert_to_ws_url("http://example.com"), "ws://example.com/revpty/ws")

    def test_https_to_wss(self):
        self.assertEqual(convert_to_ws_url("https://example.com"), "wss://example.com/revpty/ws")

    def test_ws_without_path_adds_default(self):
        self.assertEqual(convert_to_ws_url("ws://example.com"), "ws://example.com/revpty/ws")

    def test_ws_with_path_unchanged(self):
        self.assertEqual(convert_to_ws_url("ws://example.com/custom/path"), "ws://example.com/custom/path")

    def test_wss_with_path_unchanged(self):
        self.assertEqual(convert_to_ws_url("wss://example.com/custom"), "wss://example.com/custom")

    def test_no_scheme_defaults_to_ws(self):
        self.assertEqual(convert_to_ws_url("example.com"), "ws://example.com/revpty/ws")

    def test_no_scheme_with_port(self):
        self.assertEqual(convert_to_ws_url("example.com:8080"), "ws://example.com:8080/revpty/ws")

    def test_trailing_slash_removed(self):
        self.assertEqual(convert_to_ws_url("http://example.com/"), "ws://example.com/revpty/ws")

    def test_whitespace_trimmed(self):
        self.assertEqual(convert_to_ws_url("  http://example.com  "), "ws://example.com/revpty/ws")


class LoadConfigTests(unittest.TestCase):
    def test_load_toml_file(self):
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            f.write('host = "127.0.0.1"\nport = 9000\n')
            path = f.name
        try:
            config = load_config(path)
            self.assertEqual(config["host"], "127.0.0.1")
            self.assertEqual(config["port"], 9000)
        finally:
            os.unlink(path)

    def test_load_json_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"host": "10.0.0.1", "port": 8000}, f)
            path = f.name
        try:
            config = load_config(path)
            self.assertEqual(config["host"], "10.0.0.1")
            self.assertEqual(config["port"], 8000)
        finally:
            os.unlink(path)

    def test_load_nonexistent_file_raises(self):
        with self.assertRaises(SystemExit):
            load_config("/nonexistent/config.toml")

    def test_load_unknown_extension_tries_toml_then_json(self):
        with tempfile.NamedTemporaryFile(suffix=".cfg", delete=False, mode="w") as f:
            f.write('key = "toml_value"\n')
            path = f.name
        try:
            config = load_config(path)
            self.assertEqual(config["key"], "toml_value")
        finally:
            os.unlink(path)


class ResolveExecutableTests(unittest.TestCase):
    def test_resolve_existing_command(self):
        # "python3" should exist on most systems
        result = _resolve_executable("python3")
        self.assertTrue(result.endswith("python3") or "python" in result.lower())

    def test_resolve_from_sys_argv(self):
        # When command matches sys.argv[0] basename
        old_argv = sys.argv[0]
        sys.argv[0] = "/path/to/myapp"
        try:
            result = _resolve_executable("myapp")
            self.assertEqual(result, "/path/to/myapp")
        finally:
            sys.argv[0] = old_argv

    def test_resolve_returns_name_if_not_found(self):
        result = _resolve_executable("totally_nonexistent_command_xyz")
        self.assertEqual(result, "totally_nonexistent_command_xyz")