import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from agent_runtime import Engine,RunManager
from change_journal import ChangeJournal,observe_write,text_diff
from config import Settings
from main import create_app
from store import Store
from workspace import Workspace
import tools
from test_runtime import Client,Stream,chunk,call


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.ws=Workspace(base/'workspace');self.store=Store(base/'data.db');self.journal=ChangeJournal(self.store)

    async def run_tools(self,streams):
        client=Client(streams)
        async def execute(name,args):
            with tools.using_workspace(self.ws):return await tools.execute_tool(name,args)
        engine=Engine(lambda:client,execute,[],'system','fake')
        manager=RunManager(self.store,engine,journal=self.journal)
        run=manager.start(None,'test')
        task=manager.active[run['id']]['task'];await task
        return self.journal.report(run['id'],self.ws)

    async def test_actual_writes_and_multiple_steps_are_independent_of_reply(self):
        report=await self.run_tools([
            Stream([chunk(calls=[call(json.dumps({'path':'a.md','content':'first\n'}),'write_file','c1')],reason='tool_calls')]),
            Stream([chunk(calls=[call(json.dumps({'path':'a.md','old_string':'first','new_string':'second'}),'edit_file','c1')],reason='tool_calls')]),
            Stream([chunk('没有改文件。',reason='stop')])])
        self.assertEqual(self.ws.read_text('a.md'),'second\n');self.assertEqual(len(report['files']),1)
        self.assertEqual(len(report['files'][0]['change_ids']),2);self.assertEqual(report['files'][0]['current_status'],'matches')
        self.assertEqual(report['tool_counts'],{'calls':2,'succeeded':2,'failed':0,'unconfirmed':0})
        self.assertIn('-first',report['changes'][1]['diff']);self.assertIn('+second',report['changes'][1]['diff'])
        self.assertNotIn('没有改文件',str(report))

    async def test_failed_overwrite_and_noop_do_not_claim_changed_file(self):
        self.ws.write_text('a.md','keep')
        report=await self.run_tools([Stream([chunk(calls=[call('{"path":"a.md","content":"bad"}','write_file')],reason='tool_calls')]),Stream([chunk('done',reason='stop')])])
        self.assertEqual(report['files'],[]);self.assertEqual(report['changes'][0]['status'],'unchanged');self.assertEqual(report['tool_counts']['failed'],1)
        self.assertTrue(report['file_tracking_enabled'])
        self.assertEqual(self.ws.read_text('a.md'),'keep')

    async def test_current_check_detects_later_changes_missing_and_symlink(self):
        report=await self.run_tools([Stream([chunk(calls=[call('{"path":"a.md","content":"first"}','write_file')],reason='tool_calls')]),Stream([chunk('done',reason='stop')])])
        run_id=report['run_id'];self.ws.write_text('a.md','later')
        self.assertEqual(self.journal.report(run_id,self.ws)['files'][0]['current_status'],'changed')
        self.ws.delete('a.md');self.assertEqual(self.journal.report(run_id,self.ws)['files'][0]['current_status'],'missing')
        outside=Path(self.temp.name)/'outside';outside.write_text('private');(self.ws.root/'a.md').symlink_to(outside)
        report=self.journal.report(run_id,self.ws)
        self.assertEqual(report['files'][0]['current_status'],'unavailable');self.assertNotIn('private',str(report))

    async def test_cancel_after_write_keeps_evidence_and_stops_task(self):
        client=Client([Stream([chunk(calls=[call('{"path":"a.md","content":"saved"}','write_file')],reason='tool_calls')]),Stream([chunk('later',reason='stop')],delay=2)])
        async def execute(name,args):
            with tools.using_workspace(self.ws):return await tools.execute_tool(name,args)
        manager=RunManager(self.store,Engine(lambda:client,execute,[],'system','fake'),journal=self.journal)
        run=manager.start(None,'cancel after write');task=manager.active[run['id']]['task']
        for _ in range(100):
            if self.ws.path('a.md').exists():break
            await asyncio.sleep(.01)
        manager.cancel(run['id']);await task
        report=self.journal.report(run['id'],self.ws)
        self.assertEqual(report['status'],'cancelled');self.assertEqual(report['files'][0]['current_status'],'matches')
        self.assertEqual(self.ws.read_text('a.md'),'saved')

    async def test_pending_survives_recovery_without_inventing_after_hash(self):
        run,_=self.store.start(None,'crash')
        self.journal.begin(run['id'],'write_file','a.md',None);self.ws.write_text('a.md','written before crash')
        self.store.recover();report=self.journal.report(run['id'],self.ws)
        self.assertEqual(report['status'],'interrupted');self.assertEqual(report['files'][0]['current_status'],'unconfirmed')
        self.assertIsNone(report['changes'][0]['after_hash']);self.assertEqual(report['changes'][0]['status'],'pending')

    async def test_context_does_not_leak_into_manual_edits_or_other_runs(self):
        run,_=self.store.start(None,'one')
        with self.journal.track(run['id']):
            with tools.using_workspace(self.ws):await tools.execute_tool('write_file',{'path':'one.txt','content':'one'})
        with tools.using_workspace(self.ws):await tools.execute_tool('write_file',{'path':'manual.txt','content':'manual'})
        self.assertEqual([f['path'] for f in self.journal.report(run['id'],self.ws)['files']],['one.txt'])
        self.store.finish(run['id'],'completed',run['conversation_id']);self.store.delete(run['conversation_id'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM file_changes').fetchone()[0],0)

    async def test_failed_tool_that_changed_file_still_leaves_change_record(self):
        run,_=self.store.start(None,'partial failure')
        with self.journal.track(run['id']):
            with self.assertRaises(RuntimeError):
                with observe_write(self.ws,'write_file','a.txt'):
                    self.ws.write_text('a.txt','actual');raise RuntimeError('after write')
        report=self.journal.report(run['id'],self.ws)
        self.assertEqual(report['files'][0]['current_status'],'matches')


class ReportAPITests(unittest.TestCase):
    def test_report_requires_auth_returns_404_and_has_no_reasoning(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);config=Settings(workspace=base/'workspace',database=base/'data.db',token='token')
            app=create_app(config)
            with TestClient(app) as client:
                run,_=app.state.store.start(None,'task')
                app.state.store.emit(run['id'],'delta',{'content':'private reasoning is not part of a report'})
                url='/api/runs/'+run['id']+'/report'
                self.assertEqual(client.get(url).status_code,401)
                result=client.get(url,headers={'Authorization':'Bearer token'})
                self.assertEqual(result.status_code,200);self.assertNotIn('private reasoning',result.text)
                self.assertFalse(result.json()['file_tracking_enabled'])
                self.assertEqual(client.get('/api/runs/missing/report',headers={'Authorization':'Bearer token'}).status_code,404)

    def test_large_and_binary_diff_is_bounded(self):
        for before,after in ((b'old',b'x'*260000),(b'old',b'\xff\xfe')):
            diff,note=text_diff('a',before,after);self.assertEqual(diff,'');self.assertTrue(note)
