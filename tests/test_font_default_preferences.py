"""Automatic defaults must work without opening or saving the font dialog."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import profile_model as profiles


AUTO = dict(font_id='', size_mode='auto', size=0, color='keep')


class FontDefaultPreferenceTests(unittest.TestCase):
    def test_old_default_38_becomes_auto_with_font_and_color_preserved(self):
        chosen = dict(font_id='builtin:chill', path='example.ttf', sha256='example-sha',
                      family='寒蝉全圆体', ui_family='ChillRoundF', size=38, color='#ffaa12')
        for mode in (None, 'manual'):
            with self.subTest(old_mode=mode):
                original = dict(chosen)
                if mode: original['size_mode'] = mode
                result = profiles.normalize_subtitle_styles({'zh-CN': original})['zh-CN']
                self.assertEqual(result['size_mode'], 'auto')
                self.assertEqual(result['size'], 0)
                self.assertEqual(result['color'], '#FFAA12')
                for key in ('font_id', 'path', 'sha256', 'family', 'ui_family'):
                    self.assertEqual(result[key], chosen[key])
                self.assertNotIn('size_origin', result)
                self.assertEqual(original['size'], 38)

    def test_recorded_user_38_remains_manual_across_repeated_normalization(self):
        chosen = dict(font_id='builtin:lxgw', size_mode='manual', size=38,
                      size_origin='user', color='#FFFFFF')
        result = {'en': chosen}
        for _ in range(3):
            result = profiles.normalize_subtitle_styles(result)
            self.assertEqual(result['en'], chosen)

    def test_unknown_provenance_does_not_turn_old_default_into_manual(self):
        for origin in (None, '', 'legacy', 'auto', 'load', 'save'):
            with self.subTest(origin=origin):
                result = profiles.normalize_subtitle_styles({'de': dict(
                    size_mode='manual', size=38, size_origin=origin)})
                self.assertEqual(result['de'], AUTO)

    def test_distinct_old_sizes_preserve_special_choices_and_gain_user_provenance(self):
        for size in (1, 34, 37, 39, 48, 200):
            for mode in (None, 'manual'):
                with self.subTest(size=size, mode=mode):
                    chosen = dict(size=size, color='keep')
                    if mode: chosen['size_mode'] = mode
                    result = profiles.normalize_subtitle_styles({'en': chosen})['en']
                    self.assertEqual(result, dict(font_id='', size_mode='manual',
                                                 size=size, size_origin='user', color='keep'))

    def test_explicit_auto_has_no_manual_provenance_or_resolved_size(self):
        result = profiles.normalize_subtitle_styles({'ko': dict(
            size_mode='auto', size=48, size_origin='user', color='keep')})
        self.assertEqual(result['ko'], AUTO)

    def test_missing_size_and_mode_mean_auto_even_with_chosen_font(self):
        result = profiles.normalize_subtitle_styles({'ja': dict(font_id='builtin:lxgw')})
        self.assertEqual(result['ja'], dict(AUTO, font_id='builtin:lxgw'))

    def test_all_selected_languages_get_auto_without_saved_settings(self):
        languages = ['zh-CN', 'en', 'de', 'ja', 'ko', 'fr', 'ru']
        result = profiles.effective_subtitle_styles({}, languages)
        self.assertEqual(list(result), languages)
        self.assertTrue(all(style == AUTO for style in result.values()))
        result['en']['size'] = 99
        self.assertEqual(result['de'], AUTO)

    def test_selected_and_hidden_languages_stay_independent(self):
        styles = {'en': dict(size=48), 'de': dict(size=38, font_id='builtin:rubik')}
        original = copy.deepcopy(styles)
        effective = profiles.effective_subtitle_styles(styles, ['de', 'zh-CN'])
        self.assertEqual(list(effective), ['de', 'zh-CN'])
        self.assertEqual(effective['de'], dict(AUTO, font_id='builtin:rubik'))
        self.assertEqual(effective['zh-CN'], AUTO)
        self.assertEqual(styles, original)
        self.assertEqual(profiles.normalize_subtitle_styles(styles)['en']['size'], 48)

    def test_language_aliases_use_same_saved_and_effective_setting(self):
        styles = {'eng': dict(size=38, size_mode='manual', size_origin='user')}
        result = profiles.effective_subtitle_styles(styles, ['en', 'eng', 'chi'])
        self.assertEqual(list(result), ['en', 'zh-CN'])
        self.assertEqual(result['en']['size'], 38)
        self.assertEqual(result['en']['size_origin'], 'user')
        self.assertEqual(result['zh-CN'], AUTO)

    def test_empty_language_selection_enables_nothing(self):
        self.assertEqual(profiles.effective_subtitle_styles({'en': dict(size=48)}, []), {})

    def test_invalid_manual_values_are_rejected_not_clamped(self):
        for size in (True, False, 0, -1, 201, 38.5, '38', float('inf'), float('nan')):
            with self.subTest(size=size):
                self.assertEqual(profiles.normalize_subtitle_styles({'en': dict(
                    size_mode='manual', size=size, size_origin='user')}), {})

    def test_malformed_settings_do_not_break_valid_siblings(self):
        for invalid in (None, 'bad', [], dict(size_mode={}), dict(color='bad'),
                        dict(font_id=True), dict(path=123)):
            with self.subTest(invalid=invalid):
                result = profiles.normalize_subtitle_styles({'de': invalid, 'en': {}})
                self.assertEqual(result, {'en': AUTO})
        for styles in (None, [], 'bad'):
            self.assertEqual(profiles.normalize_subtitle_styles(styles), {})

    def test_existing_profile_load_migrates_in_memory_without_writing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profiles.json'
            raw = [asdict(p) for p in profiles.default_profiles()]
            raw[0]['subtitle_languages'] = ['zh-CN', 'en', 'de']
            raw[0]['subtitle_styles'] = {'de': dict(font_id='builtin:rubik', size=38, color='#aabbcc')}
            raw[1]['subtitle_styles'] = {'zh-CN': dict(size=37)}
            encoded = json.dumps(raw, ensure_ascii=False).encode('utf8')
            path.write_bytes(encoded)
            with patch.object(profiles, 'profile_file', return_value=path):
                loaded = profiles.load_profiles()
            self.assertEqual(path.read_bytes(), encoded)
            self.assertEqual(loaded[0].subtitle_languages, raw[0]['subtitle_languages'])
            self.assertEqual(loaded[0].subtitle_styles['de'], dict(AUTO, font_id='builtin:rubik', color='#AABBCC'))
            self.assertEqual(loaded[1].subtitle_styles['zh-CN']['size'], 37)
            self.assertEqual(loaded[1].subtitle_styles['zh-CN']['size_origin'], 'user')

    def test_save_and_load_use_identical_rules_and_preserve_recorded_user_38(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profiles.json'
            values = profiles.default_profiles()
            values[0].subtitle_styles = {'en': dict(size=38), 'de': dict(
                size_mode='manual', size=38, size_origin='user', color='#FFE0A3')}
            before = copy.deepcopy(values[0].subtitle_styles)
            with patch.object(profiles, 'profile_file', return_value=path):
                profiles.save_profiles(values)
                loaded = profiles.load_profiles()
            written = json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(loaded[0].subtitle_styles, written[0]['subtitle_styles'])
            self.assertEqual(loaded[0].subtitle_styles['en'], AUTO)
            self.assertEqual(loaded[0].subtitle_styles['de']['size'], 38)
            self.assertEqual(loaded[0].subtitle_styles['de']['size_origin'], 'user')
            self.assertEqual(values[0].subtitle_styles, before)

    def test_old_profile_with_no_font_field_gets_effective_auto_without_file_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profiles.json'
            raw = [asdict(p) for p in profiles.default_profiles()]
            for item in raw: item.pop('subtitle_styles')
            encoded = json.dumps(raw).encode('utf8'); path.write_bytes(encoded)
            with patch.object(profiles, 'profile_file', return_value=path):
                loaded = profiles.load_profiles()
            self.assertEqual(loaded[0].subtitle_styles, {})
            self.assertEqual(profiles.effective_subtitle_styles(
                loaded[0].subtitle_styles, loaded[0].subtitle_languages), {'zh-CN': AUTO, 'en': AUTO})
            self.assertEqual(path.read_bytes(), encoded)

    def test_profile_helper_import_and_default_resolution_do_not_import_qt(self):
        code = ('import sys, profile_model; '
                'assert profile_model.effective_subtitle_styles({}, ["en"])["en"]["size"] == 0; '
                'assert not any(name.startswith("PySide6") for name in sys.modules)')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
