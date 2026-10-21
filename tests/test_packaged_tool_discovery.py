"""Shareable builds resolve bundled/configured tools without developer folders."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pro_core as pro
import subtitle_tool_core as core


class PackagedToolDiscoveryTests(unittest.TestCase):
    def test_explicit_ffmpeg_configuration_keeps_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / 'custom-ffmpeg.exe'
            bundled = root / '_internal/tools/ffmpeg/bin/ffmpeg.exe'
            bundled.parent.mkdir(parents=True)
            configured.touch()
            bundled.touch()
            with patch.object(core, 'app_base_dir', return_value=root), \
                    patch.object(core.shutil, 'which', return_value=None):
                self.assertEqual(core.find_existing_tool(str(configured), core._DEFAULT_FFMPEG,
                                                         'ffmpeg.exe'), str(configured))

    def test_default_ffmpeg_resolves_the_embedded_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / '_internal/tools/ffmpeg/bin/ffmpeg.exe'
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            self.assertFalse(Path(core._DEFAULT_FFMPEG).is_absolute())
            with patch.object(core, 'app_base_dir', return_value=root), \
                    patch.object(core.shutil, 'which', return_value=None):
                self.assertEqual(core.find_existing_tool('', core._DEFAULT_FFMPEG,
                                                         'ffmpeg.exe'), str(bundled))

    def test_ffsubsync_prefers_bundled_but_keeps_path_fallback(self):
        bundled = pro.FFSUBSYNC_CANDIDATES[0]
        dynamic = Path('system-tools/ffsubsync.exe')
        for available, expected in (({bundled, dynamic}, bundled), ({dynamic}, dynamic)):
            with self.subTest(available=available), \
                    patch.object(pro.shutil, 'which', return_value=str(dynamic)), \
                    patch.object(Path, 'exists', autospec=True,
                                 side_effect=lambda path: path in available):
                self.assertEqual(pro._tool(pro.FFSUBSYNC_CANDIDATES, '字幕对齐引擎 ffsubsync'),
                                 str(expected))

    def test_both_bundled_whisper_model_sizes_keep_their_order(self):
        base, small = pro.WHISPER_MODEL_CANDIDATES
        self.assertEqual([base.name, small.name], ['ggml-base.bin', 'ggml-small.bin'])
        for available, expected in (({base, small}, base), ({small}, small)):
            with self.subTest(available=available), \
                    patch.object(Path, 'exists', autospec=True,
                                 side_effect=lambda path: path in available):
                self.assertEqual(pro._tool(pro.WHISPER_MODEL_CANDIDATES, 'Whisper 识别模型'),
                                 str(expected))

    def test_missing_packaged_tools_fail_clearly_without_a_machine_fallback(self):
        with patch.object(pro.shutil, 'which', return_value=None), \
                patch.object(Path, 'exists', autospec=True, return_value=False):
            for candidates, label in ((pro.FFSUBSYNC_CANDIDATES, '字幕对齐引擎 ffsubsync'),
                                      (pro.WHISPER_CLI_CANDIDATES, 'Whisper 识别引擎'),
                                      (pro.WHISPER_MODEL_CANDIDATES, 'Whisper 识别模型')):
                with self.subTest(label=label), self.assertRaisesRegex(RuntimeError, '未找到'):
                    pro._tool(candidates, label)

    def test_bundled_alass_and_decoder_environment_reach_the_subprocess(self):
        self.assertEqual(pro.ALASS_CANDIDATES[0].parts[-4:],
                         ('tools', 'alass', 'bin', 'alass-cli.exe'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / '_internal'
            alass = internal / 'tools/alass/bin/alass-cli.exe'
            ffmpeg = internal / 'tools/ffmpeg/bin/ffmpeg.exe'
            ffprobe = ffmpeg.with_name('ffprobe.exe')
            for executable in (alass, ffmpeg, ffprobe):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
            source = root / 'source.srt'
            output = root / 'work/aligned.srt'
            def subtitles(shift):
                return '\n\n'.join(
                    f'{index}\n{core.srt_time_from_milliseconds(index * 2000 + shift)} --> '
                    f'{core.srt_time_from_milliseconds(index * 2000 + 1000 + shift)}\nLine {index}.'
                    for index in range(1, 4)) + '\n'

            source.write_text(subtitles(0), encoding='utf8')
            video = str(root / 'movie.mkv')
            commands = []

            def execute(command, *, log, cancel_event, env):
                commands.append(command)
                self.assertEqual(command[0], str(alass))
                self.assertEqual(command[1:4], [video, str(source), str(output)])
                self.assertEqual(env['ALASS_FFMPEG_PATH'], str(ffmpeg))
                self.assertEqual(env['ALASS_FFPROBE_PATH'], str(ffprobe))
                self.assertTrue(Path(env['ALASS_FFMPEG_PATH']).is_file())
                self.assertTrue(Path(env['ALASS_FFPROBE_PATH']).is_file())
                self.assertIsNone(cancel_event)
                output.write_text(subtitles(250), encoding='utf8')

            # Real bundled path resolution and _tool selection run against
            # existing private files; only the external process is replaced.
            with patch.object(core, 'app_base_dir', return_value=root), \
                    patch.object(pro, 'ALASS_CANDIDATES', (alass,)), \
                    patch.object(core, 'run_command', side_effect=execute):
                self.assertEqual(pro._tool(pro.ALASS_CANDIDATES,
                                           '字幕固定偏移兜底 alass'), str(alass))
                aligned, offset = pro._alass_fixed_fallback(video, source, output,
                                                          lambda _: None, None)
            self.assertEqual((aligned, offset), (output, 250))
            self.assertEqual(len(commands), 1)
            self.assertIn('--no-split', commands[0])
            self.assertIn('--disable-fps-guessing', commands[0])
            self.assertIn('--allow-negative-timestamps', commands[0])


if __name__ == '__main__':
    unittest.main()
