import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "discord_bridge", Path(__file__).resolve().parent.parent / "discord_bridge.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_text = _mod.extract_text
split_reply = _mod.split_reply
is_allowed = _mod.is_allowed


class ExtractTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(extract_text("hello"), "hello")

    def test_dict_result_key(self):
        self.assertEqual(extract_text({"result": "ok"}), "ok")

    def test_dict_response_key(self):
        self.assertEqual(extract_text({"response": "hi"}), "hi")

    def test_dict_text_key(self):
        self.assertEqual(extract_text({"text": "yo"}), "yo")

    def test_dict_message_key(self):
        self.assertEqual(extract_text({"message": "hey"}), "hey")

    def test_dict_content_key(self):
        self.assertEqual(extract_text({"content": "sup"}), "sup")

    def test_list_of_content_blocks(self):
        payload = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        self.assertEqual(extract_text(payload), "a\nb")

    def test_nested_dict(self):
        self.assertEqual(extract_text({"result": {"text": "deep"}}), "deep")

    def test_non_string_returns_json(self):
        result = extract_text(42)
        self.assertEqual(result, "42")

    def test_empty_dict_returns_json(self):
        result = extract_text({})
        self.assertEqual(result, "{}")


class SplitReplyTests(unittest.TestCase):
    def test_under_limit(self):
        text = "x" * 1999
        self.assertEqual(split_reply(text), [text])

    def test_at_limit(self):
        text = "x" * 2000
        self.assertEqual(split_reply(text), [text])

    def test_over_limit(self):
        text = "x" * 3000
        result = split_reply(text)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 2000)
        self.assertEqual(len(result[1]), 1000)

    def test_empty_string(self):
        self.assertEqual(split_reply(""), [""])


class IsAllowedTests(unittest.TestCase):
    def setUp(self):
        self._orig_all = _mod.ALLOW_ALL_USERS
        self._orig_users = _mod.ALLOWED_USERS
        self._orig_channels = _mod.ALLOWED_CHANNELS

    def tearDown(self):
        _mod.ALLOW_ALL_USERS = self._orig_all
        _mod.ALLOWED_USERS = self._orig_users
        _mod.ALLOWED_CHANNELS = self._orig_channels

    def _set(self, *, allow_all=False, users=None, channels=None):
        _mod.ALLOW_ALL_USERS = allow_all
        _mod.ALLOWED_USERS = set(users) if users else set()
        _mod.ALLOWED_CHANNELS = set(channels) if channels else set()

    def test_no_restrictions(self):
        self._set()
        self.assertTrue(is_allowed("123", "456"))

    def test_user_allow_list_blocks(self):
        self._set(users=["111", "222"])
        self.assertFalse(is_allowed("999", "456"))

    def test_user_allow_list_allows(self):
        self._set(users=["111", "222"])
        self.assertTrue(is_allowed("111", "456"))

    def test_channel_allow_list_blocks(self):
        self._set(channels=["100"])
        self.assertFalse(is_allowed("123", "999"))

    def test_channel_allow_list_allows(self):
        self._set(channels=["100"])
        self.assertTrue(is_allowed("123", "100"))

    def test_allow_all_users_bypasses(self):
        self._set(allow_all=True, users=["111"], channels=["100"])
        self.assertTrue(is_allowed("999", "999"))


if __name__ == "__main__":
    unittest.main()
