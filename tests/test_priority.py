"""Offline tests for opt-in scoring priority."""
import unittest
from applypilot.scoring.priority import prioritize_jobs, priority_score


CONFIG = {
    "enabled": True,
    "preferred_titles": ["it support", "field service"],
    "secondary_titles": ["network engineer"],
    "preferred_locations": ["burlington", "north carolina"],
    "deprioritize_titles": ["clinical support"],
    "senior_titles": ["senior", "manager"],
    "international_locations": ["australia", "vietnam"],
}


class PriorityTests(unittest.TestCase):
    def test_local_it_first(self):
        jobs = [
            {"title": "Senior Network Engineer", "location": "Australia, Remote"},
            {"title": "IT Support Technician", "location": "Burlington, NC"},
            {"title": "Clinical Support Tech", "location": "Burlington, NC"},
        ]
        ranked = prioritize_jobs(jobs, CONFIG)
        self.assertEqual(ranked[0]["title"], "IT Support Technician")
        self.assertEqual(len(ranked), len(jobs))

    def test_unknown_location_not_excluded(self):
        job = {"title": "Field Service Technician", "location": ""}
        self.assertIsInstance(priority_score(job, CONFIG), int)

    def test_disabled_preserves_order(self):
        jobs = [{"title": "A"}, {"title": "B"}]
        self.assertEqual(prioritize_jobs(jobs, {}), jobs)


if __name__ == "__main__":
    unittest.main()
