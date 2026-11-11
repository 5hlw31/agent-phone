import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from main import create_app
from config import Settings


class SlowEngine:
    def __init__(self):self.calls=0
    async def run(self,messages,emit,checkpoint):
        self.calls+=1
        await emit('delta',{'content':'working'})
        await asyncio.sleep(.25)
        messages.append({'role':'assistant','content':'done'})
        await checkpoint();return 'completed'


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.config=Settings(workspace=base/'workspace',database=base/'data.db',password='测试口令',token='token')
        self.engine=SlowEngine();self.app=create_app(self.config,self.engine)
        self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'Authorization':'Bearer token'}

    def post(self,url,json):return self.client.post(url,json=json,headers=self.headers)

    def test_auth_and_unicode_password(self):
        self.assertEqual(self.client.get('/api/conversations').status_code,401)
        self.assertEqual(self.client.get('/api/conversations',headers={b'Authorization':'Bearer 中文'.encode()}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'password':'测试口令'}).json()['token'],'token')

    def test_invalid_requests_and_missing_conversation_never_write(self):
        for body in [[],None,{'message':False},{'message':' '},{'message':'x','conversation_id':False}]:
            self.assertEqual(self.post('/api/runs',body).status_code,400)
        self.assertEqual(self.post('/api/runs',{'message':'x','conversation_id':'missing'}).status_code,404)
        self.assertEqual(self.client.get('/api/conversations',headers=self.headers).json()['conversations'],[])

    def test_async_run_conflict_cancel_and_event_replay(self):
        run=self.post('/api/runs',{'message':'task','request_id':'once'}).json()
        start=time.monotonic()
        self.assertEqual(self.client.get('/api/health',headers=self.headers).status_code,200)
        self.assertLess(time.monotonic()-start,.2)
        self.assertEqual(self.post('/api/runs',{'message':'task','request_id':'once'}).json()['id'],run['id'])
        self.assertEqual(self.post('/api/runs',{'message':'overlap','conversation_id':run['conversation_id']}).status_code,409)
        self.assertEqual(self.post('/api/runs/'+run['id']+'/cancel',{}).status_code,200)
        for _ in range(100):
            state=self.client.get('/api/runs/'+run['id'],headers=self.headers).json()
            if state['status']!='running':break
            time.sleep(.01)
        self.assertEqual(state['status'],'cancelled')
        response=self.client.get('/api/runs/'+run['id']+'/events',headers=self.headers)
        self.assertIn('event: done',response.text)
        rows=self.app.state.store.events(run['id']);last=rows[-1]['seq']
        response=self.client.get('/api/runs/'+run['id']+'/events',headers={**self.headers,'Last-Event-ID':str(last)})
        self.assertEqual(response.text,'')

    def test_file_api_and_tools_share_workspace_boundaries(self):
        self.assertEqual(self.post('/api/files',{'path':'notes/你好.md','content':'内容'}).status_code,200)
        body=self.client.get('/api/files',params={'path':'notes/你好.md'},headers=self.headers).json()
        self.assertEqual(body['content'],'内容')
        for path in ['../outside','.env','/etc/passwd']:
            self.assertEqual(self.post('/api/files',{'path':path,'content':'bad'}).status_code,403)
        outside=Path(self.temp.name)/'outside';outside.write_text('keep')
        (self.config.workspace/'linked').symlink_to(outside)
        self.assertEqual(self.client.get('/api/files',params={'path':'linked'},headers=self.headers).status_code,403)
        self.assertEqual(self.client.get('/api/files/download',params={'path':'linked'},headers=self.headers).status_code,403)
        self.assertEqual(self.client.get('/api/files/download',params={'path':'notes/你好.md'},headers=self.headers).content,'内容'.encode())

    def test_running_conversation_cannot_be_deleted(self):
        run=self.post('/api/runs',{'message':'task'}).json()
        response=self.client.delete('/api/conversations/'+run['conversation_id'],headers=self.headers)
        self.assertEqual(response.status_code,409)

    def test_edit_revision_prevents_overwriting_newer_file(self):
        self.post('/api/files',{'path':'a.txt','content':'old'})
        old=self.client.get('/api/files',params={'path':'a.txt'},headers=self.headers).json()
        self.app.state.workspace.write_text('a.txt','newer')
        response=self.post('/api/files',{'path':'a.txt','content':'stale edit','expected_revision':old['revision']})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.app.state.workspace.read_text('a.txt'),'newer')
        self.assertEqual(self.client.delete('/api/files',params={'path':'a.txt','revision':old['revision']},headers=self.headers).status_code,409)

    def test_single_process_lock_rejects_second_instance(self):
        with self.assertRaises(RuntimeError):
            with TestClient(create_app(self.config,self.engine)):pass

    def test_unconfigured_real_model_does_not_create_records(self):
        config=Settings(workspace=Path(self.temp.name)/'other',database=Path(self.temp.name)/'other.db')
        with TestClient(create_app(config)) as client:
            self.assertEqual(client.post('/api/runs',json={'message':'task'}).status_code,503)
            self.assertEqual(client.get('/api/conversations').json()['conversations'],[])
