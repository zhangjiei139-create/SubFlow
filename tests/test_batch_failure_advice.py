"""Failure suggestions never act as admission or state decisions."""
from dataclasses import asdict
import errno
import unittest

import batch_core
import pro_core
import subtitle_tool_core as core
from profile_model import BatchPlan


class BatchFailureAdviceTests(unittest.TestCase):
    def test_only_saved_typed_candidate_handoff_recommends_current_listening(self):
        plan = BatchPlan("movie.mkv", 1, manual_review_required=True)
        before = asdict(plan)
        error = batch_core.PendingSubtitleReviewError(plan, (), {}, "manifest.json")
        self.assertIn("右键影片→核听当前字幕", batch_core.failure_advice(error))
        self.assertNotIn("待核听", str(error))
        self.assertEqual(asdict(plan), before)

    def test_similar_untyped_message_does_not_recommend_listening(self):
        advice = batch_core.failure_advice(RuntimeError("自动未确认时间轴，已有字幕可直接核听"))
        self.assertNotIn("核听当前字幕", advice)
        self.assertIn("查看详情", advice)

    def test_identity_incomplete_and_wrong_version_suggest_subtitle_selection(self):
        errors = (
            pro_core.SubtitleContentMismatchError("字幕正文与影片不同"),
            RuntimeError("候选明确属于另一影片"),
            RuntimeError("字幕不完整，不能作为时间标杆"),
            RuntimeError("字幕版本不匹配，明显错版"),
            RuntimeError("剪辑版本冲突：影片为导演剪辑版"),
            RuntimeError("候选仅匹配本片片名的局部词语，缺少同片证据"),
            RuntimeError("字幕正文混入大量非英文内容，不能作为纯英文翻译源"),
            RuntimeError("没有找到可核验的英文文本字幕来源"),
            batch_core.OfflineSubtitleUnavailableError("没有完整字幕来源"),
        )
        for error in errors:
            with self.subTest(error=error):
                advice = batch_core.failure_advice(error)
                self.assertIn("字幕选择", advice)
                self.assertIn("在线选择或本地导入", advice)
                self.assertNotIn("核听当前字幕", advice)

    def test_network_and_configuration_suggest_subtitle_service(self):
        for error in (ConnectionError("connection refused"),
                      RuntimeError("未配置 OpenSubtitles 或 SubDL API Key"),
                      RuntimeError("无法连接 SubDL：网络连接失败"),
                      RuntimeError("字幕下载额度已用完"),
                      RuntimeError("OpenSubtitles 请求失败（406）"),
                      RuntimeError("SubDL rate limit exceeded")):
            with self.subTest(error=error):
                advice = batch_core.failure_advice(error)
                self.assertIn("字幕服务", advice)
                self.assertNotIn("核听当前字幕", advice)

    def test_wrapped_network_failure_is_not_misreported_as_wrong_subtitle(self):
        error = RuntimeError("没有找到可核验的英文文本字幕来源")
        error.__cause__ = ConnectionError("connection refused")
        self.assertIn("字幕服务", batch_core.failure_advice(error))

    def test_disk_permission_and_locked_file_suggest_output_checks(self):
        for error in (OSError(errno.ENOSPC, "No space left on device"),
                      PermissionError("Access denied"),
                      RuntimeError("输出文件被占用"),
                      RuntimeError("目标磁盘空间不足")):
            with self.subTest(error=error):
                advice = batch_core.failure_advice(error)
                self.assertIn("目标目录", advice)
                self.assertIn("磁盘剩余空间", advice)
                self.assertNotIn("核听当前字幕", advice)

    def test_model_connection_failure_suggests_translation_service(self):
        for error in (core.InvalidTranslationResponseError("空响应"),
                      ConnectionError("本地模型连接提前结束"),
                      RuntimeError("翻译返回行数不一致"),
                      RuntimeError("未找到 Ollama")):
            with self.subTest(error=error):
                advice = batch_core.failure_advice(error)
                self.assertIn("翻译服务和模型", advice)
                self.assertNotIn("字幕服务", advice)
                self.assertNotIn("核听当前字幕", advice)

    def test_verifier_infrastructure_and_unknown_keep_specific_details_advice(self):
        for error in (pro_core.SubtitleVerificationToolError("取样指纹不完整"),
                      pro_core.SubtitlePreflightTimeoutError("连续VAD准备超时"),
                      TimeoutError("unknown operation timed out"),
                      RuntimeError("unexpected tool failure")):
            with self.subTest(error=error):
                self.assertEqual(batch_core.failure_advice(error), "建议查看详情，修复具体失败原因后重新处理。")

    def test_exception_chain_cycle_is_bounded(self):
        first, second = RuntimeError("one"), RuntimeError("two")
        first.__cause__, second.__cause__ = second, first
        self.assertIn("查看详情", batch_core.failure_advice(first))


if __name__ == "__main__":
    unittest.main()
