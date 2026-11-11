import asyncio
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime import Engine,RunManager,prepare_context
from store import Store,Conflict


def chunk(text=None,calls=None,reason=None,thinking=None):
    return NS(choices=[NS(delta=NS(content=text,tool_calls=calls,reasoning_content=thinking),finish_reason=reason)])


class Stream:
    def __init__(self,chunks,fail=False,delay=0):self.chunks=chunks;self.closed=False;self.fail=fail;self.delay=delay
    def __aiter__(self):return self.iterate()
    async def iterate(self):
        for item in self.chunks:
            if self.delay:await asyncio.sleep(self.delay)
            yield item
        if self.fail:raise RuntimeError('private diagnostic')
    async def close(self):self.closed=True


class Client:
    def __init__(self,streams):
        self.streams=list(streams);self.requests=[];self.closed=False
        self.chat=NS(completions=NS(create=self.create))
    async def create(self,**kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return self.streams.pop(0)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True


def call(arguments='{}',name='list_files',call_id='c1'):
    return NS(index=0,id=call_id,function=NS(name=name,arguments=arguments))


class ContextTests(unittest.TestCase):
    def test_trim_whole_turns_and_never_change_original(self):
        history=[]
        for i in range(3):
            history.extend([{'role':'user','content':str(i)},
                {'role':'assistant','content':None,'tool_calls':[{'id':str(i),'type':'function','function':{'name':'x','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':str(i),'content':'ok'}, {'role':'assistant','content':'done'}])
        original=copy.deepcopy(history)
        context=prepare_context(history,'system',max_messages=6)
        self.assertEqual(history,original);self.assertEqual(len(context),5)
        self.assertEqual(context[1]['content'],'2')
        self.assertEqual(context[3]['tool_call_id'],'2')

    def test_old_orphans_are_removed_and_missing_results_repaired(self):
        history=[{'role':'tool','tool_call_id':'orphan','content':'bad'},
            {'role':'user','content':'task'}, {'role':'assistant','content':None,'tool_calls':[{'id':'pending','function':{'name':'x','arguments':'{}'}}]},
            {'role':'user','content':'next'}]
        context=prepare_context(history,'system')
        self.assertNotIn('orphan',str(context));self.assertIn('[INTERRUPTED]',str(context))
        self.assertEqual(context[-1]['content'],'next')

    def test_current_turn_too_big_is_explicit_error(self):
        with self.assertRaises(ValueError):prepare_context([{'role':'user','content':'x'*200}],'system',max_chars=100)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def run_engine(self,streams,executor=None,**kwargs):
        client=Client(streams);self.calls=[];self.events=[]
        async def execute(name,args):
            self.calls.append((name,args));return 'file.txt'
        async def emit(event,data):self.events.append((event,data))
        async def checkpoint():pass
        engine=Engine(lambda:client,executor or execute,[],'system','fake',**kwargs)
        messages=[{'role':'user','content':'test'}]
        status=await engine.run(messages,emit,checkpoint)
        return client,messages,status

    async def test_fragmented_call_usage_frame_reasoning_and_result(self):
        first=call('{','list_');last=call('}','files',None)
        stream=Stream([chunk(thinking='internal'),chunk(calls=[first]),chunk(calls=[last],reason='tool_calls'),NS(choices=[])])
        final=Stream([chunk('done',reason='stop')])
        client,messages,status=await self.run_engine([stream,final])
        self.assertEqual(self.calls,[('list_files',{})]);self.assertEqual(status,'completed')
        self.assertEqual(client.requests[1]['messages'][2]['reasoning_content'],'internal')
        self.assertTrue(stream.closed and final.closed and client.closed)
        self.assertNotIn('internal',str(self.events))

    async def test_invalid_json_is_feedback_and_not_execution(self):
        for args in ['{bad','[]','null','"text"']:
            _,messages,_=await self.run_engine([Stream([chunk(calls=[call(args)],reason='tool_calls')]),Stream([chunk('fixed',reason='stop')])])
            self.assertEqual(self.calls,[])
            self.assertTrue(next(m['content'] for m in messages if m['role']=='tool').startswith('[ERROR]'))

    async def test_stream_failure_preserves_partial_and_closes(self):
        stream=Stream([chunk('partial')],fail=True);client=Client([stream]);messages=[]
        async def noop(*a):pass
        engine=Engine(lambda:client,noop,[],'system','fake')
        with self.assertRaises(RuntimeError):await engine.run(messages,noop,noop)
        self.assertTrue(stream.closed and client.closed);self.assertEqual(messages[-1]['content'],'partial')

    async def test_incomplete_or_duplicate_calls_never_execute(self):
        stream=Stream([chunk(calls=[call()])])
        with self.assertRaises(RuntimeError):await self.run_engine([stream])
        self.assertEqual(self.calls,[]);self.assertTrue(stream.closed)
        other=NS(index=1,id='c1',function=NS(name='list_files',arguments='{}'))
        stream=Stream([chunk(calls=[call(),other],reason='tool_calls')])
        with self.assertRaises(RuntimeError):await self.run_engine([stream])
        self.assertEqual(self.calls,[]);self.assertTrue(stream.closed)

    async def test_tool_budget_prevents_extra_execution(self):
        first=call(call_id='c1');second=NS(index=1,id='c2',function=NS(name='list_files',arguments='{}'))
        _,messages,status=await self.run_engine([Stream([chunk(calls=[first,second],reason='tool_calls')])],max_calls=1)
        self.assertEqual(len(self.calls),1);self.assertEqual(status,'limited');self.assertIn('[LIMIT]',str(messages))


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.db')

    async def test_idempotency_conflict_and_run_continues_without_listener(self):
        class Slow:
            count=0
            async def run(inner,messages,emit,checkpoint):
                inner.count+=1;await asyncio.sleep(.06)
                messages.append({'role':'assistant','content':'done'});return 'completed'
        engine=Slow();manager=RunManager(self.store,engine)
        run=manager.start(None,'task','once')
        replay=manager.start(None,'task','once');self.assertEqual(run['id'],replay['id'])
        with self.assertRaises(Conflict):manager.start(run['conversation_id'],'overlap')
        events=manager.events(run['id']);await anext(events);await events.aclose()
        await asyncio.sleep(.1)
        self.assertEqual(self.store.run(run['id'])['status'],'completed');self.assertEqual(engine.count,1)
        with self.assertRaises(Conflict):manager.start(None,'changed','once')

    async def test_cancel_stops_before_followup_and_saves_tool_protocol(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        async def execute(*a):
            started.set()
            try:await asyncio.sleep(30)
            finally:cancelled.set()
        client=Client([Stream([chunk(calls=[call()],reason='tool_calls')])])
        engine=Engine(lambda:client,execute,[],'system','fake')
        manager=RunManager(self.store,engine);run=manager.start(None,'task')
        await started.wait();manager.cancel(run['id'])
        await asyncio.sleep(.02)
        self.assertTrue(cancelled.is_set());self.assertEqual(self.store.run(run['id'])['status'],'cancelled')
        self.assertIn('[CANCELLED]',str(self.store.load(run['conversation_id'])['messages']))
        self.assertTrue(client.closed);self.assertEqual(len(client.requests),1)

    async def test_cancel_before_worker_starts(self):
        class Never:
            async def run(self,*args):raise AssertionError('must not execute')
        manager=RunManager(self.store,Never());run=manager.start(None,'task')
        manager.cancel(run['id']);await asyncio.sleep(.01)
        self.assertEqual(self.store.run(run['id'])['status'],'cancelled')

    async def test_restart_marks_old_tasks_interrupted(self):
        run,_=self.store.start(None,'task');self.store.recover()
        self.assertEqual(self.store.run(run['id'])['status'],'interrupted')
        self.assertEqual(self.store.events(run['id'])[-1]['event'],'done')

    async def test_restart_recovers_partial_reply_without_repeating_it(self):
        run,_=self.store.start(None,'task')
        self.store.emit(run['id'],'phase',{'phase':'model','round':1})
        self.store.emit(run['id'],'delta',{'content':'已生成但尚未完成'})
        self.store.recover();self.store.recover()
        messages=self.store.load(run['conversation_id'])['messages']
        self.assertEqual(sum(m.get('content')=='已生成但尚未完成' for m in messages),1)
        self.assertTrue(messages[-1]['_partial'])
