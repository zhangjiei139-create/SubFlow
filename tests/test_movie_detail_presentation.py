import unittest
import batch_core as batch
from profile_model import BatchPlan

class MovieDetailTests(unittest.TestCase):
    def test_failed_detail_shows_single_identity_cause_advice_and_report(self):
        plan=BatchPlan('C:/ExampleMovies/Silent Hill (2012).mkv',1)
        plan.audio_tracks='英文 / DTS-HD（主音轨），英文 / DTS'
        plan.subtitles='英文 / PGS、英文 / SRT、英文 / SRT、德语 / SRT'
        plan.output_path='C:/ExampleMovies/Silent Hill (2012).SF.mkv'
        plan.summary='处理失败；建议：右键影片→字幕选择'
        plan.detail=('影片规格：片长 1:34:47\n影片：Silent Hill (2012).mkv\n套用：偏好 1 · 简中+英文+德文\n'
            '音轨：保留全部2条\n字幕：保留英文、德文；新增简中\n提示：评论字幕不用于正片\n'
            '输出：'+plan.output_path+'\n字幕失败报告：C:/ExampleMovies/report.txt\n\n'
            '处理失败：旧错误\n\n处理建议：旧建议\n\n处理失败：AttributeError: suffix\n\n处理建议：请保留报告并修复程序。')
        original=plan.detail
        text=batch.movie_detail_text(plan,'处理失败')
        for prefix in ('影片：','路径：','状态：','套用偏好：','失败原因：','处理建议：','输出：','字幕失败报告：'):
            self.assertEqual(sum(line.startswith(prefix) for line in text.splitlines()),1,prefix)
        self.assertNotIn('旧错误',text);self.assertNotIn('旧建议',text)
        self.assertNotIn('右键影片→字幕选择',text)
        self.assertIn('英文 / SRT ×2',text)
        self.assertIn('字幕方案：保留英文、德文；新增简中',text)
        self.assertIn('评论字幕不用于正片',text)
        self.assertEqual(plan.detail,original)

    def test_normal_analysis_still_keeps_scheme_and_unstructured_explanations(self):
        plan=BatchPlan('C:/ExampleMovies/Movie.mkv',2)
        plan.summary='新增中文字幕'
        plan.detail='影片规格：1080p\n其他核验说明仍须保留'
        text=batch.movie_detail_text(plan,'等待处理')
        self.assertIn('处理方案：新增中文字幕',text)
        self.assertIn('其他核验说明仍须保留',text)
        self.assertNotIn('失败原因：',text)

    def test_actual_qml_detail_dialog_uses_the_single_formatted_document(self):
        import json
        from PySide6.QtCore import QObject
        from PySide6.QtQml import QQmlExpression,QQmlEngine
        from tests.test_qml_paired_context_menu import QmlPairedContextMenuTests
        scene=QmlPairedContextMenuTests();scene.setUpClass();scene.setUp();self.addCleanup(scene.tearDown)
        plan=scene.controller.batch_plans[str(scene.movies[0])]
        plan.summary='处理失败；建议：检查程序'
        plan.task_state='failed';plan.task_status_label='处理失败'
        plan.detail='影片：movie-0.mkv\n套用：偏好1\n\n处理失败：程序错误\n\n处理建议：检查程序'
        scene.backend._refresh_batch_items()
        item=scene.backend.batchItems[0]
        dialog=scene.window.findChild(QObject,'batchDetailDialog')
        self.assertIsNotNone(dialog)
        expression=QQmlExpression(QQmlEngine.contextForObject(dialog),dialog,'showDetails('+json.dumps(item)+')')
        expression.evaluate();self.assertFalse(expression.hasError(),expression.error().toString())
        scene.app.processEvents()
        area=scene.window.findChild(QObject,'movieDetailText')
        self.assertIsNotNone(area)
        self.assertEqual(area.property('text'),item['detailText'])
        self.assertEqual(area.property('text').count('处理建议：'),1)
