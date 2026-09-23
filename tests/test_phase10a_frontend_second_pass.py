from pathlib import Path
import unittest


class Phase10AFrontendSecondPassTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1] / "src" / "archive_index" / "web"
        self.html = (self.root / "index.html").read_text(encoding="utf-8")
        self.management = (self.root / "app-file-management.js").read_text(encoding="utf-8")
        self.details = (self.root / "app-details.js").read_text(encoding="utf-8")
        self.file_management_backend = (self.root.parent / "file_management.py").read_text(encoding="utf-8")

    def test_file_management_is_header_action_with_structured_sections(self):
        self.assertLess(self.html.index('id="file-management-button"'), self.html.index('id="configure-workspace"'))
        self.assertLess(self.html.index('id="configure-workspace"'), self.html.index('id="index"'))
        self.assertNotIn("Settings JSON", self.html)
        self.assertNotIn("Rules JSON", self.html)
        self.assertNotIn('id="file-management-output"', self.html)
        for marker in ("data-file-management-tab=\"rules\"", "data-file-management-tab=\"profiles\"", "data-file-management-tab=\"plan\"", "Analyze plan", "Custom Ruleset"):
            self.assertIn(marker, self.html + self.management)

    def test_rules_use_semantic_controls_and_no_executor(self):
        for marker in ("representation_class", "selection_state", "delete", "source_disposition", "destination_status", "executor"):
            self.assertIn(marker, self.management + self.file_management_backend)
        self.assertIn('"available": False', self.file_management_backend)

    def test_representation_eye_and_actual_comparison_controls_exist(self):
        for marker in ("data-representation-view", "View representation", "comparison-preview", "Side by side", "Slider", "PSNR", "SharedImageCamera"):
            self.assertIn(marker, self.details + (self.root.parent / "api" / "comparison.py").read_text(encoding="utf-8"))
        self.assertNotIn("bindComparisonTransform", self.details)


if __name__ == "__main__":
    unittest.main()
