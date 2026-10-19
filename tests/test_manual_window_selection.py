"""Manual listening-window selection; synthetic cues/activity, no media tools.

VAD fixtures describe activity patterns only. Passing these tests does not mean
that a music signal has been identified as dialogue or that sync was verified.
"""
import unittest
import tempfile
from pathlib import Path

import manual_window_selection as selection
import subtitle_tool_core as core


def cue(start, end, text="Please leave the door open."):
    return core.SubtitleEvent(
        core.srt_time_from_milliseconds(round(start * 1000)),
        core.srt_time_from_milliseconds(round(end * 1000)),
        text,
    )


def balanced(start=0, text="Please leave the door open."):
    return [cue(start + at, start + at + 2.5, text)
            for at in (3, 7, 13, 19, 33, 37, 43, 49)]


def at_start(options, start):
    return next(option for option in options if option["start"] == start)


class DialogueTextTest(unittest.TestCase):
    def test_pure_sound_effects_and_marked_lyrics_do_not_count_as_dialogue(self):
        for text in ("", "[MUSIC]", "[gunshots]", "[音乐]", "♪ We sail away ♪", "♫", "[MUSIC]\n♪ We sail away ♪"):
            with self.subTest(text=text):
                self.assertEqual(selection.dialogue_text(text).strip(), "")

    def test_mixed_effect_and_dialogue_keeps_the_spoken_line(self):
        for text, spoken in (("[gunshots]\nRun to the door!", "Run to the door!"),
                             ("[音乐]\n我们现在就走。", "我们现在就走。"),
                             ("♪ We sail away ♪\nLeave now.", "Leave now.")):
            with self.subTest(text=text):
                cleaned = selection.dialogue_text(text)
                self.assertIn(spoken, cleaned)
                self.assertNotIn("gunshots", cleaned.lower())
                self.assertNotIn("音乐", cleaned)
                self.assertNotIn("We sail away", cleaned)
                self.assertNotIn("♪", cleaned)

    def test_normal_parenthetical_dialogue_is_not_discarded(self):
        text = "I will come (if I can)."
        self.assertIn("if I can", selection.dialogue_text(text))
        text = "（我再想想）还是先走吧。"
        self.assertIn("我再想想", selection.dialogue_text(text))
        self.assertIn("还是先走吧", selection.dialogue_text(text))
        self.assertIn("I love this music.", selection.dialogue_text("(I love this music.)"))

    def test_godzilla_sdh_effects_do_not_count_but_named_dialogue_survives(self):
        for text in ("[Kong groans]", "[sniffing]", "[whimpers]", "[mini-Kong yelps]",
                     "[in sign language]", "[chattering violently]"):
            self.assertFalse(selection.dialogue_text(text),text)
        self.assertEqual(selection.dialogue_text("[Ilene] That arm looks bad."),"That arm looks bad.")

    def test_explicit_singing_is_not_dialogue_but_switch_back_to_speech_is(self):
        for label in ('[singing]','(singing)','[sings]','[Anna singing]','（唱歌）'):
            self.assertFalse(selection.dialogue_text(label+' We sail away'),label)
        self.assertEqual(selection.dialogue_text('[singing] We sail away [speaking] Leave now.'),'Leave now.')
        self.assertIn('I love singing',selection.dialogue_text('(I love singing.)'))

    def test_dialogue_does_not_require_latin_words_or_english(self):
        for text in ("No!", "我们现在就走。", "「先に行って。」", "먼저 가세요.", "Vámonos ahora."):
            with self.subTest(text=text):
                self.assertTrue(selection.dialogue_text(text).strip())

    def test_markup_is_not_itself_dialogue(self):
        self.assertEqual(selection.dialogue_text("<i>[MUSIC]</i>").strip(), "")
        self.assertIn("Run now!", selection.dialogue_text("<i>Run now!</i>"))

    def test_file_parser_flattened_mixed_lyrics_preserve_dialogue(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'mixed.srt'
            path.write_text('1\n00:00:02,000 --> 00:00:04,000\n♪ We sail away ♪\nLeave now.\n',encoding='utf8')
            event=core.parse_subtitle(path)[0]
        self.assertEqual(selection.dialogue_text(event.text),'Leave now.')


class ManualWindowShortlistTest(unittest.TestCase):
    def test_balanced_window_is_preferred_with_observable_metrics(self):
        option = at_start(selection.shortlist_windows(balanced(), 540, 1), 0)
        self.assertEqual(option["dialogue_count"], 8)
        self.assertEqual(option["cue_count"], 8)
        self.assertEqual(list(option["half_counts"]), [4, 4])
        self.assertAlmostEqual(option["occupied_seconds"], 20)
        self.assertEqual(option["gap_count"], 4)
        self.assertEqual(option["tier"], 0)

    def test_zero_one_and_four_cues_do_not_fall_back_to_sparse_windows(self):
        for count in (0, 1, 4):
            with self.subTest(count=count):
                events = [cue(at, at + 2.5) for at in (3, 13, 33, 43)[:count]]
                self.assertEqual(selection.shortlist_windows(events, 540, 1), [])

    def test_six_dialogues_are_available_only_as_backup(self):
        events = [cue(at, at + 2.5) for at in (3, 11, 19, 33, 41, 49)]
        option = at_start(selection.shortlist_windows(events, 540, 1), 0)
        self.assertEqual(option["dialogue_count"], 6)
        self.assertEqual(list(option["half_counts"]), [3, 3])
        self.assertEqual(option["tier"], 1)

    def test_total_count_does_not_hide_an_almost_empty_first_half(self):
        events = [cue(at, at + 1.5) for at in (3, 33, 36, 39, 42, 45, 48, 51)]
        self.assertEqual(selection.shortlist_windows(events, 540, 1), [])

    def test_minimum_two_dialogues_per_half_is_a_backup_not_preferred(self):
        events = [cue(at, at + 2) for at in (3, 9, 15, 21, 35, 45)]
        option = at_start(selection.shortlist_windows(events, 540, 1), 0)
        self.assertEqual(list(option["half_counts"]), [4, 2])
        self.assertEqual(option["tier"], 1)

    def test_many_sound_effects_cannot_promote_four_actual_dialogues(self):
        events = [cue(at, at + 2) for at in (3, 13, 33, 43)]
        events += [cue(at, at + 1, "[MUSIC]") for at in range(1, 59, 4)]
        self.assertEqual(selection.shortlist_windows(events, 540, 1), [])

    def test_sound_effects_are_total_cues_but_do_not_inflate_dialogue_metrics(self):
        events = balanced() + [cue(at, at + 1, "[gunshots]") for at in (1, 10, 31, 40)]
        option = at_start(selection.shortlist_windows(events, 540, 1), 0)
        self.assertEqual(option["cue_count"], 12)
        self.assertEqual(option["dialogue_count"], 8)
        self.assertAlmostEqual(option["occupied_seconds"], 20)
        self.assertEqual(option["tier"], 0)

    def test_occupied_seconds_merge_overlapping_dialogue_cues(self):
        events = [cue(at, at + 4, f"A different dialogue line {index}.")
                  for index, at in enumerate((3, 3, 13, 13, 33, 33, 43, 43))]
        option = at_start(selection.shortlist_windows(events, 540, 1), 0)
        self.assertAlmostEqual(option["occupied_seconds"], 16)
        self.assertEqual(option["gap_count"], 2)

    def test_balanced_window_precedes_a_denser_backup_even_if_it_is_later(self):
        dense = [cue(1 + index * 2, 2.5 + index * 2) for index in range(24)]
        preferred = balanced(120)
        options = selection.shortlist_windows(dense + preferred, 540, 1)
        self.assertEqual(options[0]["start"], 120)
        self.assertEqual(options[0]["tier"], 0)
        # Dense dialogue may be offered if no preferred candidate exists.
        backups = selection.shortlist_windows(dense, 540, 1)
        self.assertTrue(backups)
        self.assertTrue(all(option["tier"] == 1 for option in backups))

    def test_continuous_occupancy_is_not_preferred_just_because_count_is_good(self):
        events = [cue(index * 7, index * 7 + 7) for index in range(8)]
        options = selection.shortlist_windows(events, 540, 1)
        self.assertTrue(options)
        self.assertEqual(options[0]["tier"], 1)
        self.assertEqual(options[0]["gap_count"], 0)

    def test_shortlist_stays_inside_its_third_and_is_bounded_and_separated(self):
        events = sum((balanced(start) for start in (0, 120, 240, 360, 480, 600, 720, 840, 960)), [])
        for region in (1, 2, 3):
            with self.subTest(region=region):
                options = selection.shortlist_windows(events, 1080, region, limit=2)
                self.assertEqual(len(options), 2)
                starts = [option["start"] for option in options]
                self.assertTrue(all((region - 1) * 360 <= start and start + 60 <= region * 360 for start in starts))
                self.assertGreaterEqual(abs(starts[0] - starts[1]), 120)
                self.assertTrue(all(start % 30 == 0 for start in starts))

    def test_chinese_and_japanese_dialogue_can_form_preferred_windows(self):
        for text in ("我们现在就走。", "「先に行って。」"):
            with self.subTest(text=text):
                option = at_start(selection.shortlist_windows(balanced(text=text), 540, 1), 0)
                self.assertEqual(option["dialogue_count"], 8)
                self.assertEqual(option["tier"], 0)


class ManualAudioWindowQualityTest(unittest.TestCase):
    def test_empty_activity_is_not_usable(self):
        result = selection.audio_window_quality([])
        self.assertEqual(result["spoken_seconds"], 0)
        self.assertFalse(result["usable"])
        self.assertFalse(result["preferred"])

    def test_distributed_paused_activity_is_preferred(self):
        activity = [(2, 5), (9, 12), (16, 19), (34, 37), (41, 44), (48, 51)]
        result = selection.audio_window_quality(activity)
        self.assertEqual(result["spoken_seconds"], 18)
        self.assertEqual(list(result["half_seconds"]), [9, 9])
        self.assertEqual(result["pause_count"], 4)
        self.assertTrue(result["usable"])
        self.assertTrue(result["preferred"])

    def test_continuous_activity_is_not_preferred_over_clear_pauses(self):
        result = selection.audio_window_quality([(0, 60)])
        self.assertEqual(result["spoken_seconds"], 60)
        self.assertEqual(result["pause_count"], 0)
        self.assertTrue(result["usable"])
        self.assertFalse(result["preferred"])

    def test_activity_concentrated_in_one_half_is_unusable(self):
        result = selection.audio_window_quality([(1, 12), (15, 25)])
        self.assertGreater(result["spoken_seconds"], 12)
        self.assertFalse(result["usable"])
        self.assertFalse(result["preferred"])

    def test_short_activity_is_usable_only_as_backup(self):
        result = selection.audio_window_quality([(3, 6), (33, 36)])
        self.assertEqual(result["spoken_seconds"], 6)
        self.assertTrue(result["usable"])
        self.assertFalse(result["preferred"])

    def test_overlapping_activity_does_not_inflate_spoken_seconds(self):
        result = selection.audio_window_quality([(1, 6), (3, 8), (32, 37), (35, 40)])
        self.assertEqual(result["spoken_seconds"], 15)
        self.assertEqual(list(result["half_seconds"]), [7, 8])
        self.assertEqual(result["pause_count"], 0)

    def test_audio_quality_does_not_require_zero_offset_cue_overlap(self):
        # Unlike the subtitle fixture at 3/7/13/... these bursts occur elsewhere.
        # No transcript, cue text, or zero-offset alignment enters this API.
        result = selection.audio_window_quality([(8, 11), (15, 18), (23, 26), (39, 42), (47, 50), (55, 58)])
        self.assertTrue(result["usable"])
        self.assertTrue(result["preferred"])


if __name__ == "__main__":
    unittest.main()
