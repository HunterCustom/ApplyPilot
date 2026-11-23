"""Offline regression tests for Qwen's system-then-user prompt sequence."""
import unittest
from unittest.mock import Mock

from applypilot.llm import LLMClient


class QwenPromptTests(unittest.TestCase):
    def test_no_think_prefixed_to_first_user_after_system(self):
        client = LLMClient("http://127.0.0.1:1234/v1", "qwen/qwen3-14b", "")
        client._chat_compat = Mock(return_value="SCORE: 5")
        messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Score this job."},
        ]
        client.chat(messages)
        sent = client._chat_compat.call_args.args[0]
        self.assertEqual(sent[0]["content"], "Be concise.")
        self.assertTrue(sent[1]["content"].startswith("/no_think\\n"))
        self.assertEqual(messages[1]["content"], "Score this job.")
        client.close()


if __name__ == "__main__":
    unittest.main()
