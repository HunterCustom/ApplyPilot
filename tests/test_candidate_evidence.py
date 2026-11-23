"""Offline candidate evidence tests: safety, formatting, and compatibility."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applypilot.scoring.candidate_evidence import (
    format_candidate_evidence,
    load_candidate_evidence,
)
from applypilot.scoring.scorer import score_job


class EvidenceTests(unittest.TestCase):
    def _yaml(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "candidate_evidence.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_profile_is_optional(self):
        self.assertEqual(load_candidate_evidence(Path("nonexistent-candidate-evidence-test.yaml")), {})

    def test_disabled_profile_is_ignored(self):
        path = self._yaml("enabled: false\nprofessional_experience:\n  - 'Example work'\n")
        self.assertEqual(load_candidate_evidence(path), {})

    def test_professional_and_personal_evidence_distinguished(self):
        path = self._yaml(
            "enabled: true\nprofessional_experience:\n  - 'Maintained servers at work'\n"
            "personal_projects:\n  - 'Built a home network'\n"
        )
        result = format_candidate_evidence(load_candidate_evidence(path))
        self.assertIn("PROFESSIONAL EXPERIENCE", result)
        self.assertIn("PERSONAL PROJECTS (NOT EMPLOYMENT)", result)
        self.assertIn("Built a home network", result)

    def test_invalid_data_rejected(self):
        path = self._yaml("enabled: true\nprofessional_experience: no\n")
        with self.assertRaises(ValueError):
            load_candidate_evidence(path)

    def test_score_prompt_includes_evidence_without_changing_resume(self):
        fake_client = Mock()
        fake_client.chat.return_value = (
            "SCORE: 7\nKEYWORDS: networks\n"
            "REASONING: Professional repairs match; home lab is transferable."
        )
        job = {"title": "IT Technician", "site": "Example", "location": "NC", "full_description": "Network support"}
        with patch("applypilot.scoring.scorer.get_client", return_value=fake_client):
            score_job("Resume text", job, "USER-VERIFIED PERSONAL PROJECTS (NOT EMPLOYMENT):\n- Home network")
        messages = fake_client.chat.call_args.args[0]
        self.assertIn("Resume text", messages[1]["content"])
        self.assertIn("NOT EMPLOYMENT", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
