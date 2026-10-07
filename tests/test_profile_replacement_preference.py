# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import profile_model


class ReplacementPreferenceTest(unittest.TestCase):
    def test_old_image_discard_setting_does_not_delete_pgs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiles.json"
            saved = [
                {**profile_model.asdict(profile), "discard_image_subtitles": True}
                for profile in profile_model.default_profiles()
            ]
            path.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(profile_model, "profile_file", return_value=path):
                profiles = profile_model.load_profiles()
                self.assertTrue(all(not p.replace_downloaded_subtitle for p in profiles))
                profile_model.save_profiles(profiles)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("discard_image_subtitles" not in item for item in stored))
            self.assertTrue(all(item["replace_downloaded_subtitle"] is False for item in stored))


if __name__ == "__main__":
    unittest.main()
