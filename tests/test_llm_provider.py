import os
import unittest
from unittest import mock

from applypilot import llm


class DescribeProviderTests(unittest.TestCase):
    def describe(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return llm.describe_provider()

    def test_local_url_wins_and_other_keys_reported_ignored(self):
        info = self.describe(LLM_URL="http://127.0.0.1:1234/v1/", LLM_MODEL="qwen/qwen3-14b",
                             GEMINI_API_KEY="secret-value")
        self.assertEqual(info["provider"], "Local")
        self.assertEqual(info["base_url"], "http://127.0.0.1:1234/v1")
        self.assertEqual(info["model"], "qwen/qwen3-14b")
        self.assertEqual(info["ignored"], ["GEMINI_API_KEY"])
        self.assertNotIn("secret-value", repr(info))

    def test_gemini_default_model(self):
        info = self.describe(GEMINI_API_KEY="x")
        self.assertEqual((info["provider"], info["model"]), ("Gemini", "gemini-2.0-flash"))

    def test_nothing_configured_raises(self):
        with self.assertRaises(RuntimeError):
            self.describe()

    def test_list_local_models_unreachable_returns_none(self):
        with mock.patch.object(llm.httpx, "get", side_effect=llm.httpx.ConnectError("down")):
            self.assertIsNone(llm.list_local_models("http://127.0.0.1:9/v1"))


if __name__ == "__main__":
    unittest.main()
