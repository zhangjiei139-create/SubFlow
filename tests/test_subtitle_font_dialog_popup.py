import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from profile_model import PreferenceProfile
from subtitle_font_dialog import FontChoiceCombo, SubtitleFontDialog

APP=QApplication.instance() or QApplication([])


class FontDialogPopupTests(unittest.TestCase):
    def setUp(self):
        self.host=QWidget();self.host.setWindowTitle('SubFlow · popup test')
        self.host.setGeometry(20,20,1120,700);self.host.show()
        self.combo=FontChoiceCombo(self.host);self.combo.setGeometry(60,120,600,40)
        for i in range(350):self.combo.addItem(f'Font {i:03} — long family name',dict(id=i))
        APP.processEvents()

    def tearDown(self):
        self.combo.hidePopup();self.host.close();self.host.deleteLater();APP.processEvents()

    def show(self):
        with patch('subtitle_font_dialog.main_window_geometry',return_value=QRect(20,20,1120,700)):
            self.combo.showPopup()
        APP.processEvents()

    def test_popup_width_is_one_third_and_height_is_bounded(self):
        self.show()
        self.assertEqual(self.combo.popup.width(),round(1120/3))
        self.assertLessEqual(self.combo.popup.height(),700)
        self.assertLessEqual(self.combo.popup.height(),480)
        self.assertEqual(self.combo.font_list.horizontalScrollBarPolicy(),Qt.ScrollBarAlwaysOff)

    def test_scrollbar_is_visible_and_can_jump_to_bottom(self):
        self.show();bar=self.combo.font_list.verticalScrollBar()
        self.assertTrue(bar.isVisible());self.assertGreater(bar.maximum(),0)
        bar.setValue(bar.maximum());APP.processEvents()
        self.assertGreaterEqual(self.combo.font_list.indexAt(self.combo.font_list.viewport().rect().bottomLeft()).row(),340)

    def test_keyboard_selection_updates_original_combo_data(self):
        self.combo.setCurrentIndex(40);self.show()
        QTest.keyClick(self.combo.font_list,Qt.Key_Down)
        QTest.keyClick(self.combo.font_list,Qt.Key_Return)
        self.assertEqual(self.combo.currentData(),dict(id=41))
        self.assertFalse(self.combo.popup.isVisible())

    def test_escape_preserves_original_selection_and_can_reopen(self):
        self.combo.setCurrentIndex(40);self.show()
        QTest.keyClick(self.combo.font_list,Qt.Key_Down);QTest.keyClick(self.combo.font_list,Qt.Key_Escape)
        self.assertEqual(self.combo.currentIndex(),40)
        self.show();self.assertTrue(self.combo.popup.isVisible())

    def test_new_fonts_are_visible_without_rebuilding_popup(self):
        self.combo.addItem('New imported font',dict(id=999));self.show()
        self.assertEqual(self.combo.font_list.model().rowCount(),351)
        self.combo.choose_font(self.combo.model().index(350,0))
        self.assertEqual(self.combo.currentData(),dict(id=999))

    def test_dialog_has_rounded_card_and_compact_save_button(self):
        with patch('subtitle_font_dialog.system_choices',return_value=[]):
            dialog=SubtitleFontDialog(PreferenceProfile(2,'UI test',subtitle_languages=['zh-CN','en']))
        try:
            dialog.show();APP.processEvents()
            self.assertTrue(dialog.windowFlags() & Qt.FramelessWindowHint)
            self.assertEqual(dialog.save_button.height(),40)
            self.assertLessEqual(dialog.save_button.width(),132)
            self.assertEqual(dialog.save_button.objectName(),'fontSaveButton')
            self.assertEqual(dialog.grab().toImage().pixelColor(0,0).alpha(),0)
        finally:dialog.reject();dialog.deleteLater();APP.processEvents()


if __name__=='__main__':unittest.main()
