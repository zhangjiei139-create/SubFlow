"""UI regression for automatic/manual size and independently retained fonts."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from profile_model import PreferenceProfile
from subtitle_font_dialog import AdaptiveSizeInput, SubtitleFontDialog, VideoSubtitlePreview
import subtitle_fonts as fonts

APP = QApplication.instance() or QApplication([])
LANGUAGES = ['zh-CN', 'en', 'de', 'ja', 'ko']


class AdaptiveFontDialogTests(unittest.TestCase):
    def setUp(self):
        self.system = patch('subtitle_font_dialog.system_choices', return_value=[])
        self.system.start()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        APP.sendPostedEvents(None, 0)
        APP.processEvents()
        self.system.stop()

    def dialog(self, languages=LANGUAGES, styles=None, slot=1):
        profile = PreferenceProfile(slot, 'Font test', subtitle_languages=list(languages))
        profile.subtitle_styles = styles or {}
        result = SubtitleFontDialog(profile)
        self.dialogs.append(result)
        return result

    def enter_size(self, size, value):
        size.edit.selectAll()
        QTest.keyClicks(size.edit, str(value))

    def test_all_selected_languages_start_in_independent_automatic_mode(self):
        dialog = self.dialog()
        self.assertEqual(list(dialog.rows), LANGUAGES)
        for code, (combo, size, color) in dialog.rows.items():
            with self.subTest(language=code):
                self.assertEqual(size.edit.text(), '默认')
                self.assertEqual(dialog.selection(code)['size_mode'], 'auto')
                self.assertEqual(size.value(), 0)
                self.assertTrue(size.isEnabled())
                self.assertTrue(color.isEnabled())
                self.assertEqual(color.text(), '保持原色')
                self.assertEqual(combo.currentData()['id'], '')

    def test_all_languages_allow_manual_size_even_keeping_original_font(self):
        dialog = self.dialog()
        for code in LANGUAGES:
            with self.subTest(language=code):
                self.enter_size(dialog.rows[code][1], 48)
                style = dialog.selection(code)
                self.assertEqual(style['size'], 48)
                self.assertEqual(style['size_mode'], 'manual')
                self.assertEqual(style['font_id'], '')
        dialog.save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        for code in LANGUAGES:
            self.assertEqual(dialog.result_styles[code]['size'], 48)
            self.assertEqual(dialog.result_styles[code]['size_mode'], 'manual')
            self.assertEqual(dialog.result_styles[code]['size_origin'], 'user')

    def test_default_save_keeps_automatic_for_every_visible_language(self):
        dialog = self.dialog()
        dialog.save()
        for code in LANGUAGES:
            self.assertEqual(dialog.result_styles[code],
                             dict(font_id='', size_mode='auto', size=0, color='keep'))

    def test_existing_numeric_choice_is_never_replaced_with_automatic(self):
        dialog = self.dialog(['de'], {'de': dict(font_id='builtin:rubik', size=34, color='#FFE0A3')})
        self.assertEqual(dialog.rows['de'][1].value(), 34)
        self.assertEqual(dialog.selection('de')['size_mode'], 'manual')
        dialog.save()
        self.assertEqual(dialog.result_styles['de']['size'], 34)
        self.assertEqual(dialog.result_styles['de']['color'], '#FFE0A3')

    def test_old_automatically_filled_38_opens_as_default_without_clicking(self):
        styles = {
            code: dict(font_id='builtin:lxgw', size_mode='manual', size=38,
                       color='#FFFFFF') for code in ('zh-CN', 'en', 'de')}
        dialog = self.dialog(['zh-CN', 'en', 'de'], styles)
        for code in dialog.rows:
            with self.subTest(language=code):
                self.assertEqual(dialog.rows[code][1].edit.text(), '默认')
                self.assertEqual(dialog.selection(code)['size_mode'], 'auto')
                self.assertNotIn('size_origin', dialog.selection(code))
        dialog.save()
        for code in dialog.rows:
            self.assertEqual(dialog.result_styles[code]['size'], 0)
            self.assertEqual(dialog.result_styles[code]['size_mode'], 'auto')
            self.assertEqual(dialog.result_styles[code]['font_id'], 'builtin:lxgw')
            self.assertEqual(dialog.result_styles[code]['color'], '#FFFFFF')
        # The dialog never overwrites the caller's preference until accepted
        # by the actual application save path.
        self.assertEqual(dialog.profile.subtitle_styles, styles)

    def test_freshly_typed_38_is_manual_and_stays_manual_when_reopened(self):
        dialog = self.dialog(['en'])
        size = dialog.rows['en'][1]
        self.enter_size(size, 38)
        self.assertEqual(dialog.selection('en')['size_origin'], 'user')
        dialog.save()
        saved = dialog.result_styles
        other = self.dialog(['en'], saved)
        self.assertEqual(other.rows['en'][1].edit.text(), '38')
        self.assertEqual(other.selection('en')['size_mode'], 'manual')
        self.assertEqual(other.selection('en')['size_origin'], 'user')

    def test_programmatic_set_value_does_not_claim_to_be_a_user_edit(self):
        size = AdaptiveSizeInput()
        size.setValue(38)
        self.assertIsNone(size.size_origin)
        size.setValue(38, size_origin='user')
        self.assertEqual(size.size_origin, 'user')
        size.setValue(0)
        self.assertIsNone(size.size_origin)
        size.deleteLater()

    def test_default_reset_removes_manual_origin_from_saved_profile(self):
        dialog = self.dialog(['en'], {'en': dict(font_id='', size_mode='manual',
                                                size=38, size_origin='user', color='keep')})
        size = dialog.rows['en'][1]
        self.assertEqual(size.value(), 38)
        self.assertEqual(size.reset.text(), '恢复默认')
        size.reset.click()
        dialog.save()
        self.assertEqual(dialog.result_styles['en'],
                         dict(font_id='', size_mode='auto', size=0, color='keep'))

    def test_font_and_color_changes_do_not_turn_default_into_manual(self):
        dialog = self.dialog(['en'])
        combo, size, color = dialog.rows['en']
        index = next(i for i, item in enumerate(dialog.choices) if item['id'] == 'builtin:rubik')
        combo.setCurrentIndex(index)
        color.setText('#12ABEF')
        dialog.save()
        self.assertEqual(dialog.result_styles['en']['size_mode'], 'auto')
        self.assertNotIn('size_origin', dialog.result_styles['en'])

    def test_arrows_are_explicit_and_start_from_this_rows_default_estimate(self):
        dialog = self.dialog()
        with patch('subtitle_style_engine.automatic_size', return_value=(32, {'ass_font_scale': 1.0})):
            for code in LANGUAGES:
                with self.subTest(language=code):
                    size = dialog.rows[code][1]
                    size.estimate = None
                    self.assertEqual(size.up.text(), '▲')
                    self.assertEqual(size.down.text(), '▼')
                    size.up.click()
                    self.assertEqual(size.value(), 33)
                    self.assertEqual(size.size_origin, 'user')
                    size.down.click()
                    self.assertEqual(size.value(), 32)
                    size.reset.click()
                    self.assertEqual(size.edit.text(), '默认')
                    self.assertEqual(size.value(), 0)
                    self.assertIsNone(size.size_origin)

    def test_invalid_typed_sizes_block_save_instead_of_being_silently_clamped(self):
        dialog = self.dialog(['en'])
        size = dialog.rows['en'][1]
        with patch('subtitle_font_dialog.QMessageBox.warning') as warning:
            for value in ('0', '201', '-5', '38.5', '', 'abc'):
                with self.subTest(value=value):
                    size.edit.setText(value)
                    dialog.save()
                    self.assertIsNone(dialog.result_styles)
                    self.assertEqual(size.edit.text(), value)
            self.assertEqual(warning.call_count, 6)
        for value in (1, 38, 48, 200):
            size.setValue(value)
            self.assertEqual(size.value(), value)

    def test_boundary_arrow_does_not_switch_to_automatic(self):
        size = AdaptiveSizeInput()
        size.setValue(1); size.down.click(); self.assertEqual(size.value(), 1)
        size.setValue(200); size.up.click(); self.assertEqual(size.value(), 200)
        self.assertEqual(size.edit.text(), '200')
        size.deleteLater()

    def test_color_can_be_changed_without_changing_font_or_size_mode(self):
        dialog = self.dialog(['de'])
        dialog.rows['de'][2].setText('#12ABEF')
        style = dialog.selection('de')
        self.assertEqual(style['font_id'], '')
        self.assertEqual(style['color'], '#12ABEF')
        self.assertEqual(style['size_mode'], 'auto')

    def test_preview_uses_ui_family_separately_from_player_family(self):
        dialog = self.dialog(['zh-CN'])
        index = next(i for i, item in enumerate(dialog.choices) if item['id'] == 'builtin:chill')
        dialog.rows['zh-CN'][0].setCurrentIndex(index)
        info = fonts.resolve_font(dialog.selection('zh-CN'))
        self.assertEqual(dialog.preview.reference_font.family(), info['ui_family'])
        self.assertNotEqual(info['family'], info['ui_family'])
        from subtitle_style_engine import ass_font_scale
        self.assertEqual(dialog.preview.ass_scale, ass_font_scale(info['path']))
        self.enter_size(dialog.rows['zh-CN'][1], 38)
        dialog.save()
        self.assertEqual(dialog.result_styles['zh-CN']['family'], info['family'])

    def test_preview_translates_ass_units_back_to_em_pixels(self):
        preview = VideoSubtitlePreview()
        try:
            preview.resize(1304, 150)
            preview.set_reference(QFont('Arial'), 150, '#FFFFFF', ass_scale=2.5)
            self.assertEqual(preview.reference_size, 150)
            self.assertEqual(preview.font().pixelSize(), 60)
        finally:
            preview.deleteLater()

    def test_preview_manual_size_has_no_hidden_44_pixel_cap(self):
        preview = VideoSubtitlePreview()
        preview.resize(1304, 150)
        preview.set_reference(QFont('Arial'), 38, '#FFFFFF')
        small = preview.font().pixelSize()
        preview.set_reference(QFont('Arial'), 100, '#FFFFFF')
        large = preview.font().pixelSize()
        self.assertEqual(small, 38)
        self.assertEqual(large, 100)
        preview.resize(664, 150); preview.update_scale()
        self.assertEqual(preview.font().pixelSize(), 50)
        preview.deleteLater()

    def test_application_skin_cannot_override_preview_font_or_size(self):
        original = APP.styleSheet()
        preview = VideoSubtitlePreview()
        try:
            APP.setStyleSheet('QWidget {font-family: Arial; font-size: 12px;}')
            preview.resize(1304, 150)
            preview.set_reference(QFont('Times New Roman'), 100, '#12ABEF')
            preview.show(); APP.processEvents()
            self.assertEqual(preview.font().pixelSize(), 100)
            self.assertEqual(preview.font().family(), 'Times New Roman')
            self.assertIn('color:#12ABEF', preview.styleSheet())
        finally:
            preview.close(); preview.deleteLater(); APP.setStyleSheet(original)

    def test_profile_filtering_and_hidden_language_settings_are_preserved(self):
        hidden = dict(font_id='builtin:bree', size_mode='manual', size=33, color='#FFFFFF')
        dialog = self.dialog(['de'], {'en': hidden}, slot=3)
        self.assertEqual(list(dialog.rows), ['de'])
        self.enter_size(dialog.rows['de'][1], 48); dialog.save()
        self.assertEqual(dialog.result_styles['en'], dict(hidden, size_origin='user'))
        self.assertEqual(dialog.profile.subtitle_styles, {'en': hidden})
        other = self.dialog(['de'], slot=4)
        self.assertEqual(other.rows['de'][1].value(), 0)

    def test_missing_font_and_missing_glyphs_do_not_save_as_if_applied(self):
        dialog = self.dialog(['zh-CN'])
        index = next(i for i, item in enumerate(dialog.choices) if item['id'] == 'builtin:rubik')
        dialog.rows['zh-CN'][0].setCurrentIndex(index)
        self.assertIn('缺少', dialog.notice.text())
        with patch('subtitle_font_dialog.QMessageBox.warning') as warning:
            dialog.save()
            self.assertIsNone(dialog.result_styles)
            self.assertIn('缺字', warning.call_args.args[2])
        with patch('subtitle_font_dialog.fonts.resolve_font', side_effect=ValueError('字体文件已经变化')):
            dialog.preview_language('zh-CN')
            self.assertIn('已经变化', dialog.notice.text())
            with patch('subtitle_font_dialog.QMessageBox.warning') as warning:
                dialog.save()
                self.assertIsNone(dialog.result_styles)
                self.assertIn('已经变化', warning.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
