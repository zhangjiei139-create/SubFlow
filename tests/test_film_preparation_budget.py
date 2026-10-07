from pathlib import Path
import sys,unittest,tempfile,time,threading
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import continuous_vad as v,pro_core as p,subtitle_tool_core as c,smart_subtitles as smart

class FilmPreparationBudgetTests(unittest.TestCase):
    def test_duration_policy_is_bounded(self):
        for duration,expected in [(600,180),(6727.68,180),(7200,180),(10800,180),(10801,300),(86400,300),(0,180),(-1,180),(float('nan'),180),(float('inf'),180)]:
            with self.subTest(duration=duration):self.assertEqual(v.preparation_budget_seconds(duration),expected)

    def run_preparation(self,budget=None):
        audio=c.Track(2,'audio','DTS-HD Master Audio','en','',False,False,False,False,False)
        deadlines=[]
        def build(movie,audio_id,cache,log,cancel):
            deadlines.append(cancel.deadline-time.monotonic());return Path('ref.npz')
        with tempfile.TemporaryDirectory() as td,patch.object(c,'inspect_tracks',return_value=[audio]),patch.object(c,'inspect_media',return_value={}),patch.object(c,'video_track_duration_ns',return_value=6727.68e9),patch.object(p,'_audio_stream_index',return_value=1),patch.object(p,'_build_continuous_alignment_audio',side_effect=build):
            v.shared_audio(str(Path(td)/'movie.mkv'),2,lambda _:None,budget=budget)
        return deadlines[0]

    def test_default_uses_duration_and_explicit_budget_is_not_enlarged(self):
        self.assertAlmostEqual(self.run_preparation(),180,delta=.5)
        self.assertAlmostEqual(self.run_preparation(60),60,delta=.5)
        self.assertAlmostEqual(self.run_preparation(.5),.5,delta=.2)

    def test_explicit_invalid_limits_are_rejected(self):
        for value in (0,-1,float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):self.run_preparation(value)

    def test_active_wrapper_and_search_opt_in_to_duration_policy(self):
        with patch.object(v,'shared_audio',return_value=object()) as shared:
            p.prepare_shared_subtitle_content_audio('movie',2,'work',lambda _:None)
        self.assertIsNone(shared.call_args.args[4])
        self.assertIsNone(smart.SMART_AUDIO_PREPARATION_SECONDS)
        self.assertEqual(smart.SMART_TIME_BUDGET_SECONDS,20)

    def test_waiting_for_same_film_still_reports_timeout_and_does_not_release_other_owner(self):
        audio=c.Track(2,'audio','AAC','en','',False,False,False,False,False)
        lock=MagicMock();lock.acquire.return_value=False
        with tempfile.TemporaryDirectory() as td:
            movie=str(Path(td)/'movie.mkv');key=(str(Path(movie).resolve()).casefold(),2)
            with patch.dict(v._locks,{key:lock}),patch.object(c,'inspect_tracks',return_value=[audio]),patch.object(c,'inspect_media',return_value={}),patch.object(p,'_build_continuous_alignment_audio') as build:
                with self.assertRaisesRegex(p.SubtitlePreflightTimeoutError,'0.01秒上限'):
                    v.shared_audio(movie,2,lambda _:None,budget=.01)
            lock.release.assert_not_called();build.assert_not_called()

    def test_user_cancel_remains_user_cancel(self):
        audio=c.Track(2,'audio','AAC','en','',False,False,False,False,False);stop=threading.Event();stop.set()
        with tempfile.TemporaryDirectory() as td,patch.object(c,'inspect_tracks',return_value=[audio]),patch.object(c,'inspect_media',return_value={}):
            with self.assertRaises(c.CancelledError):v.shared_audio(str(Path(td)/'movie'),2,lambda _:None,stop)

    def test_shared_image_preparation_defers_packet_scan(self):
        tracks=[c.Track(i,'subtitles','HDMV PGS','zh','',False,False,False,True,False) for i in (4,5,6,7)]
        with tempfile.TemporaryDirectory() as td,patch.object(c,'inspect_tracks',return_value=tracks),patch.object(c,'inspect_media',return_value={}),patch.object(c,'video_track_duration_ns',return_value=6727.68e9),patch.object(p,'_embedded_image_intervals',return_value=[(1,2)]) as scan:
            p.prepare_shared_image_timing('movie',[4,5,6,7],td,lambda _:None)
        scan.assert_not_called()

    def test_shared_image_preparation_preserves_user_cancel(self):
        tracks=[c.Track(4,'subtitles','HDMV PGS','zh','',False,False,False,True,False)]
        stop=threading.Event();stop.set()
        with tempfile.TemporaryDirectory() as td,patch.object(c,'inspect_tracks',return_value=tracks),patch.object(p,'_embedded_image_intervals') as scan:
            with self.assertRaises(c.CancelledError):
                p.prepare_shared_image_timing('movie',[4],td,lambda _:None,stop)
            scan.assert_not_called()

    def test_image_preparation_once_precedes_all_candidate_clocks(self):
        candidates=[SimpleNamespace(release=f'Movie item {i}',file_name=f'c{i}.srt',language='en',file_id=str(i)) for i in (1,2)]
        order=[]
        class Service:
            @staticmethod
            def load_settings():return {'key':'test'}
            @staticmethod
            def search(*args):return None,candidates,None
            @staticmethod
            def download(key,candidate,dest):
                path=Path(dest)/candidate.file_name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('unique '+candidate.file_id);return path
        real_deadline=smart._DeadlineCancel
        def deadline(*args):order.append('candidate_clock');return real_deadline(*args)
        def prepare(cancel):order.append('prepare_images');c.check_cancel(cancel)
        def verify(movie,sub,*args,**kwargs):order.append('verify');return Path(sub),'pass'
        def accept(path,cancel):
            order.append('accept')
            if path.name=='c1.srt':raise p.SubtitleContentMismatchError('PGS conflict')
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'Movie.mkv';movie.write_bytes(b'v')
            with patch.object(smart,'PROVIDERS',(('Test',Service,'key'),)),patch.object(p,'prepare_shared_subtitle_content_audio',return_value=object()),patch.object(p,'preflight_online_subtitle',side_effect=verify),patch.object(smart,'_DeadlineCancel',side_effect=deadline):
                result=smart.find_verified_english(str(movie),2,lambda _:None,candidate_preparation=prepare,candidate_acceptance=accept)
            self.assertEqual(result.candidate.file_id,'2')
            self.assertEqual(order,['prepare_images','candidate_clock','verify','accept','candidate_clock','verify','accept'])

if __name__=='__main__':unittest.main()
