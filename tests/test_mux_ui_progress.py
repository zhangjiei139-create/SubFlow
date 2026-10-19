import unittest
from qt_workers import MuxProgressTracker


class MuxUiProgressTests(unittest.TestCase):
    def test_tool_percent_does_not_complete_unpublished_movie(self):
        state=MuxProgressTracker()
        state.update('a.mkv：mkvmerge 封装开始：主输入文件 20 GiB。')
        self.assertTrue(state.busy)
        state.update('a.mkv：mkvmerge 封装进度 63%；已用 60秒。')
        self.assertEqual(state.value,63)
        self.assertFalse(state.busy)
        state.update('a.mkv：mkvmerge 封装完成：耗时100秒。')
        state.update('a.mkv：输出验证通过：视频1。')
        self.assertTrue(state.busy)
        self.assertIn('正在验证',state.label)
        self.assertNotIn('已完成',state.label)
        state.update('a.mkv：正式输出已就绪：C:/ExampleMovies/a.mkv')
        self.assertEqual(state.value,100)
        self.assertFalse(state.busy)
        self.assertIn('已完成',state.label)

    def test_concurrent_results_are_independent(self):
        state=MuxProgressTracker()
        for name in ('a.mkv','b.mkv'):
            state.update(name+'：mkvmerge 封装开始：主输入文件10GiB。')
        state.update('a.mkv：mkvmerge 封装进度 70%；已用5秒。')
        state.update('b.mkv：mkvmerge 封装进度 12%；已用5秒。')
        self.assertIn('同时封装 2 部',state.label)
        state.update('a.mkv：正式输出已就绪：C:/ExampleMovies/a.mkv')
        self.assertEqual(state.value,12)
        self.assertIn('b.mkv',state.label)
        state.update('b.mkv：处理失败：磁盘不足')
        self.assertNotIn('已完成',state.label)
        self.assertFalse(state.busy)

    def test_remux_failure_and_retry_do_not_keep_previous_percent(self):
        state=MuxProgressTracker('movie.mkv')
        state.update('正在写入修改后的译文；原成品保留。')
        state.update('mkvmerge 封装进度 98%；已用1秒。')
        state.update('重新封装未完成：标签不符')
        self.assertIn('原成品保留',state.label)
        state.update('正在写入修改后的译文；原成品保留。')
        self.assertEqual(state.value,0)
        self.assertTrue(state.busy)
        state.finish_unpublished()
        self.assertIn('未完成',state.label)


if __name__=='__main__': unittest.main()
