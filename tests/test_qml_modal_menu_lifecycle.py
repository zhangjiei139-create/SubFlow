"""Exercise actual QML signals and nested QWidget dialogs in isolated processes.

The old implementation aborts the child process on a periodic row refresh;
mocking only the bridge callback cannot detect that native Qt failure.
"""
from pathlib import Path
import hashlib,json,os,subprocess,sys,tempfile,unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'qml_frontend')]


def run_child(output,route,result_path):
    from unittest.mock import patch
    from PySide6.QtCore import QMetaObject,QObject,Property,Qt,QTimer,QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtWidgets import QApplication,QDialog
    from bridge import BackendBridge
    from profile_model import BatchPlan
    from translation_review_dialog import TranslationReviewDialog

    class LicenseFixture(QObject):
        @Property(bool,constant=True)
        def valid(self): return True
        @Property(bool,constant=True)
        def busy(self): return False
        @Property(str,constant=True)
        def message(self): return ''
        @Property(str,constant=True)
        def deviceCode(self): return 'test'

    app=QApplication([])
    output=Path(output); source=str(output).replace('.SF.mkv','.mkv')
    state=dict(opened=False,rows=0,refreshes=0,handler_returned=False)
    with patch.object(BackendBridge,'_detect_hardware'),patch('qt_app.ProMaxQt._detect_hardware'), \
         patch('profile_model.profile_file',return_value=output.parent/'profiles.json'):
        backend=BackendBridge()
        controller=backend._ensure_tool_controller()
        controller.batch_paths=[source]
        plan=BatchPlan(path=source,profile_slot=1,output_path=str(output),status='ready',task_state='completed')
        plan.external_subtitle=str(output.parent/'movie.SF_work/translated-de.srt')
        plan.translation_review_pending=2
        controller.batch_plans[source]=plan
        controller._render_batch_table()
        fixture=LicenseFixture()
        engine=QQmlApplicationEngine()
        for name,value in [('backend',backend),('licenseBridge',fixture),('uiFontFamily','Arial'),('appVersion','test')]:
            engine.rootContext().setContextProperty(name,value)
        engine.load(QUrl.fromLocalFile(str(ROOT/'qml_frontend/Main.qml')))
        if not engine.rootObjects(): return 2
        window=engine.rootObjects()[0]

        def refresh(): state['refreshes']+=1
        backend.toolStateChanged.connect(refresh)
        def fixture_dialog(_path):
            dialog=QDialog(controller);dialog.setWindowTitle('modal lifecycle fixture');dialog.exec()
        if route=='manual': controller._open_batch_online_search=fixture_dialog
        if route=='align': controller._force_batch_manual_review=fixture_dialog
        if route=='pending':
            plan.manual_review_required=True
            plan.status='failed'
            plan.task_state='failed'
            controller._open_batch_pending_review=fixture_dialog
            backend.toolStateChanged.emit()

        def trigger():
            batch_list=window.findChild(QQuickItem,'batchList')
            rows=[item for item in batch_list.childItems()[0].childItems() if item.objectName()=='movieRow']
            menu=rows[0].findChild(QObject,'movieContextMenu')
            if route=='manual':
                action=menu.findChild(QObject,'contextChooseOnlineSubtitle')
            else:
                action=menu.findChild(QObject,'contextReviewTranslation' if route=='translation' else 'contextAlignSubtitle')
            QMetaObject.invokeMethod(action,'triggered',Qt.ConnectionType.DirectConnection)
            state['handler_returned']=True

        def close_dialog():
            dialog=app.activeModalWidget()
            if dialog is not None:
                state['opened']=True
                if isinstance(dialog,TranslationReviewDialog): state['rows']=dialog.table.rowCount()
                dialog.reject()

        QTimer.singleShot(100,trigger)
        QTimer.singleShot(1500,close_dialog)
        QTimer.singleShot(1800,app.quit)
        exit_code=app.exec()
        backend._tool_sync_timer.stop()
        window.close()
        engine.deleteLater()
        app.processEvents()
        Path(result_path).write_text(json.dumps(state),encoding='utf8')
        return exit_code


class QmlModalMenuLifecycleTests(unittest.TestCase):
    def exercise(self,route):
        import subtitle_tool_core as core
        import translation_review as review
        import translation_remux as remux
        from subtitle_identity_guard import media_signature
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);output=folder/'movie.SF.mkv';output.write_bytes(b'completed fixture')
            work=remux.work_directory(output);work.mkdir()
            events=[core.SubtitleEvent('00:00:01,000','00:00:03,000','Think differently.'),
                    core.SubtitleEvent('00:00:05,000','00:00:07,000','Come back.')]
            target=work/'translated-de.srt';cache=work/'translation-cache-de.jsonl'
            issues={i:dict(index=i,start=e.start,end=e.end,source=e.text,translation='Anders框.',
                           reason='含中文字符',status='needs_review',repair_attempts=2)
                    for i,e in enumerate(events,1)}
            review.save_progress(events,'de','en',cache,target,issues,complete=True,
                                 translations={i:x['translation'] for i,x in issues.items()})
            report=review.report_path(cache)
            manifest=dict(schema_version=1,output=str(output.resolve()),output_signature=media_signature(str(output)),
                bindings=[dict(report=str(report.resolve()),target_srt=str(target.resolve()),muxed_sha256=remux._hash(target),
                               pending_count=2,track=dict(id=3,uid=123,codec='S_TEXT/UTF8',language='de',name='德语 Auto'))])
            (work/remux.MANIFEST).write_text(json.dumps(manifest),encoding='utf8')
            before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (output,target,report)}
            result_path=folder/'result.json'
            env=os.environ.copy();env.update(QT_QPA_PLATFORM='offscreen',QSG_RHI_BACKEND='software')
            run=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--child',str(output),route,str(result_path)],
                               cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=15)
            self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
            result=json.loads(result_path.read_text(encoding='utf8'))
            self.assertTrue(result['opened'])
            self.assertTrue(result['handler_returned'])
            self.assertGreaterEqual(result['refreshes'],2)
            if route=='translation': self.assertEqual(result['rows'],2)
            self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_real_translation_dialog_survives_refresh_from_qml_menu(self): self.exercise('translation')
    def test_manual_search_modal_survives_refresh_from_qml_menu(self): self.exercise('manual')
    def test_manual_alignment_modal_survives_refresh_from_qml_menu(self): self.exercise('align')
    def test_pending_review_modal_survives_refresh_from_qml_menu(self): self.exercise('pending')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        raise SystemExit(run_child(*sys.argv[2:5]))
    unittest.main()
