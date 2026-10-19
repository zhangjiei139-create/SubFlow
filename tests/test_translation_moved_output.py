"""Moved-movie review bindings preserve immutable small-artifact snapshots."""
from pathlib import Path
import json,shutil,tempfile,unittest
from unittest.mock import patch
import subtitle_tool_core as core
import translation_remux as remux
import translation_review as review


class MovedTranslationOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.old=self.root/'original/movie.SF.mkv';self.old.parent.mkdir();self.old.write_bytes(b'original movie')
        self.new=self.root/'nas/movie.SF.mkv';self.new.parent.mkdir()
        self.work=remux.work_directory(self.old);self.work.mkdir()
        event=core.SubtitleEvent('00:00:01,000','00:00:03,000','Think differently.')
        self.cache=self.work/'translation-cache-de.jsonl';self.target=self.work/'translated-de.srt'
        issue=dict(index=1,start=event.start,end=event.end,source=event.text,translation='Anders框.',
                   reason='含中文字符',status='needs_review',repair_attempts=2)
        review.save_progress([event],'de','en',self.cache,self.target,{1:issue},complete=True,translations={1:issue['translation']})
        self.props=dict(uid=99,number=1,codec_id='S_TEXT/UTF8',language='ger',language_ietf='de',
                        track_name=core.LANGUAGES['de'][2]+' Auto')
        self.media=dict(tracks=[dict(id=3,type='subtitles',properties=self.props)])
        with patch.object(core,'inspect_media',return_value=self.media):
            remux.register_output(self.old,self.work,['de'])
        shutil.copy2(self.old,self.new);self.old.unlink()
        self.store=self.root/'records'
        self.store_patch=patch.object(remux,'REVIEW_STORE',self.store);self.store_patch.start();self.addCleanup(self.store_patch.stop)
        self.index_patch=patch.object(remux,'LOCATION_FILE',self.store/'locations.json');self.index_patch.start();self.addCleanup(self.index_patch.stop)

    def register(self):
        with patch.object(core,'inspect_media',return_value=self.media):
            return remux.register_moved_output(self.old,self.new)

    def test_relocation_copies_only_records_and_opens_from_both_old_and_new_paths(self):
        movie=self.new.read_bytes();original=self.target.read_bytes()
        store=self.register()
        self.assertTrue(remux.available(self.old));self.assertTrue(remux.available(self.new))
        for path in (self.old,self.new):
            manifest,reports=remux.load_bound_reports(path)
            self.assertEqual(manifest['output'],str(self.new.resolve()))
            self.assertEqual(reports[0][1]['issues'][0]['translation'],'Anders框.')
            self.assertEqual(reports[0][0].parent,store.resolve())
        self.assertEqual(self.new.read_bytes(),movie)
        self.assertEqual(self.target.read_bytes(),original)
        self.assertFalse(list(store.glob('*.mkv')))

    def test_different_subtitle_uid_is_rejected_before_any_snapshot(self):
        media=dict(tracks=[dict(self.media['tracks'][0],properties=dict(self.props,uid=100))])
        with patch.object(core,'inspect_media',return_value=media):
            with self.assertRaisesRegex(ValueError,'UID'):remux.register_moved_output(self.old,self.new)
        self.assertFalse(self.store.exists())

    def test_different_movie_size_is_rejected_before_probing(self):
        self.new.write_bytes(b'other movie with another size')
        with patch.object(core,'inspect_media') as probe:
            with self.assertRaisesRegex(ValueError,'大小'):remux.register_moved_output(self.old,self.new)
        probe.assert_not_called()

    def test_changed_movie_cannot_reuse_registered_verdict(self):
        self.register();self.new.write_bytes(b'changed file')
        with self.assertRaisesRegex(ValueError,'成品已变化'):remux.load_bound_reports(self.new)

    def test_new_processing_at_original_path_does_not_overwrite_moved_review(self):
        self.register()
        self.old.write_bytes(b'new output generation')
        self.target.write_text('later processing replaced this subtitle',encoding='utf8')
        self.assertEqual(remux.current_output(self.old),self.old.resolve())
        _manifest,reports=remux.load_bound_reports(self.new)
        self.assertEqual(reports[0][1]['issues'][0]['translation'],'Anders框.')

    def test_second_move_keeps_original_logical_link(self):
        self.register()
        again=self.new.with_name('renamed.SF.mkv');self.new.rename(again)
        with patch.object(core,'inspect_media',return_value=self.media):remux.register_moved_output(self.new,again)
        self.assertEqual(remux.current_output(self.old),again.resolve())
        self.assertEqual(remux.current_output(self.new),again.resolve())
        self.assertEqual(len(remux.load_bound_reports(again)[1]),1)

    def test_registry_write_failure_preserves_movie_and_original_records(self):
        original=remux._hash(self.target);movie=self.new.read_bytes();writer=remux._write
        def write(path,value):
            if path==remux.LOCATION_FILE:raise OSError('disk full')
            return writer(path,value)
        with patch.object(remux,'_write',side_effect=write):
            with self.assertRaisesRegex(OSError,'disk full'):self.register()
        self.assertEqual(remux._hash(self.target),original)
        self.assertEqual(self.new.read_bytes(),movie)
        self.assertFalse(list(self.store.iterdir()))

    def test_movie_replaced_during_identity_probe_cannot_create_binding(self):
        def replace_movie(*args,**kwargs):
            self.new.write_bytes(b'externally replaced movie')
            return self.media
        with patch.object(core,'inspect_media',side_effect=replace_movie):
            with self.assertRaisesRegex(ValueError,'关联期间'):remux.register_moved_output(self.old,self.new)
        self.assertFalse(remux.LOCATION_FILE.exists())


if __name__=='__main__':unittest.main()
