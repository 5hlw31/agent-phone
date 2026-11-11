"""Async model/tool loop and cancellable runs, independent of browser connections."""
import asyncio
import copy
import json
import logging
import time
from contextlib import nullcontext
from store import TERMINAL

logger=logging.getLogger('agent-phone.runtime')


class ContextBudgetError(ValueError):
    pass


def repair_protocol(messages):
    """Preserve complete call/result groups; old orphan tool messages aren't sent."""
    result=[];pending={}
    def flush():
        for call_id in pending:
            result.append({'role':'tool','tool_call_id':call_id,'content':'[INTERRUPTED] 上一次未记录到工具结果，请先核对实际文件。'})
        pending.clear()
    for original in messages:
        role=original.get('role')
        if role=='system':
            continue
        if role=='tool':
            call_id=original.get('tool_call_id')
            if call_id in pending:
                result.append({'role':'tool','tool_call_id':call_id,'content':str(original.get('content',''))})
                pending.pop(call_id)
            continue
        flush()
        if role not in ('user','assistant'):
            continue
        item={k:copy.deepcopy(v) for k,v in original.items() if k in ('role','content','tool_calls','reasoning_content')}
        if original.get('_partial'):
            item['content']=(item.get('content') or '')+'\n[这段回复未完成]'
        result.append(item)
        for call in item.get('tool_calls',[]):
            pending[call['id']]=call
    flush()
    return result


def prepare_context(messages, system, max_messages=80, max_chars=48000):
    repaired=repair_protocol(messages)
    turns=[]
    for message in repaired:
        if message['role']=='user' or not turns:
            turns.append([])
        turns[-1].append(message)
    chosen=[];size=len(system);count=1
    for turn in reversed(turns):
        length=len(json.dumps(turn,ensure_ascii=False))
        if size+length>max_chars or count+len(turn)>max_messages:
            if not chosen:
                raise ContextBudgetError('当前任务的上下文超过限制，请新开对话或缩小任务。')
            break
        chosen.insert(0,turn);size+=length;count+=len(turn)
    return [{'role':'system','content':system}]+[m for turn in chosen for m in turn]


def close_pending(messages, reason):
    pending={}
    start=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),0)
    for item in messages[start:]:
        for call in item.get('tool_calls',[]):
            pending[call['id']]=call
        if item.get('role')=='tool':
            pending.pop(item.get('tool_call_id'),None)
    for call_id in pending:
        messages.append({'role':'tool','tool_call_id':call_id,'content':reason})


class Engine:
    def __init__(self, factory, executor, definitions, system, model,
                 max_rounds=8, max_calls=16, max_messages=80, max_chars=48000,
                 request_options=None,max_output_tokens=4096):
        self.factory,self.executor,self.definitions=factory,executor,definitions
        self.system,self.model=system,model
        self.max_rounds,self.max_calls=max_rounds,max_calls
        self.max_messages,self.max_chars=max_messages,max_chars
        self.request_options=request_options or {}
        self.max_output_tokens=max_output_tokens

    async def run(self, messages, emit, checkpoint):
        calls_used=0
        async with self.factory() as client:
            for round_no in range(1,self.max_rounds+1):
                await emit('phase',{'phase':'model','round':round_no})
                context=prepare_context(messages,self.system,self.max_messages,self.max_chars)
                stream=await client.chat.completions.create(model=self.model,messages=context,
                    tools=self.definitions,stream=True,max_tokens=self.max_output_tokens,**self.request_options)
                content=[];reasoning=[];calls={};finish=None;committed=False
                buffer='';last_flush=time.monotonic()
                try:
                    async for chunk in stream:
                        if not chunk.choices:
                            continue
                        choice=chunk.choices[0];delta=choice.delta
                        if choice.finish_reason is not None:
                            finish=choice.finish_reason
                        if not delta:
                            continue
                        if getattr(delta,'reasoning_content',None):
                            reasoning.append(delta.reasoning_content)
                        if delta.content:
                            content.append(delta.content);buffer+=delta.content
                            if len(buffer)>=48 or time.monotonic()-last_flush>=.08:
                                await emit('delta',{'content':buffer});buffer='';last_flush=time.monotonic()
                        for fragment in delta.tool_calls or []:
                            item=calls.setdefault(fragment.index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                            if fragment.id:
                                item['id']=fragment.id
                            if fragment.function:
                                item['function']['name']+=fragment.function.name or ''
                                item['function']['arguments']+=fragment.function.arguments or ''
                    if buffer:
                        await emit('delta',{'content':buffer});buffer=''
                    if finish not in ('stop','tool_calls') or (calls and finish!='tool_calls'):
                        raise RuntimeError('模型回复没有正常结束')
                    assistant={'role':'assistant','content':''.join(content) or None}
                    if reasoning:
                        assistant['reasoning_content']=''.join(reasoning)
                    if calls:
                        ordered=[calls[i] for i in sorted(calls)]
                        ids=[call['id'] for call in ordered]
                        if any(not call['id'] or not call['function']['name'] for call in ordered) or len(set(ids))!=len(ids):
                            raise RuntimeError('工具调用信息不完整')
                        assistant['tool_calls']=ordered
                    elif not assistant['content']:
                        raise RuntimeError('模型返回了空回复')
                    messages.append(assistant);committed=True
                    await checkpoint()
                finally:
                    await stream.close()
                    if not committed and content:
                        messages.append({'role':'assistant','content':''.join(content),'_partial':True})
                        await checkpoint()
                if not calls:
                    return 'completed'
                for call in ordered:
                    if calls_used>=self.max_calls:
                        close_pending(messages,'[LIMIT] 已达到工具调用次数限制，未执行。')
                        await checkpoint()
                        return 'limited'
                    calls_used+=1
                    name=call['function']['name'];raw=call['function']['arguments']
                    try:
                        arguments=json.loads(raw)
                    except (ValueError,TypeError):
                        arguments=None
                    await emit('tool_call',{'call_id':call['id'],'tool_name':name,'arguments':arguments,'round':round_no})
                    await emit('phase',{'phase':'tool','tool_name':name,'round':round_no})
                    if not isinstance(arguments,dict):
                        result='[ERROR] 工具参数必须是合法的 JSON 对象。'
                    else:
                        result=await self.executor(name,arguments)
                    result=str(result)
                    if len(result)>12000:
                        result=result[:12000]+'\n[结果已截断，可使用文件工具分段读取]'
                    messages.append({'role':'tool','tool_call_id':call['id'],'content':result})
                    await checkpoint()
                    await emit('tool_result',{'call_id':call['id'],'tool_name':name,'result':result})
            messages.append({'role':'assistant','content':'已达到本次调用轮次限制，请先查看执行结果。'})
            await checkpoint()
            return 'limited'


class RunManager:
    def __init__(self,store,engine,timeout=180,journal=None):
        self.store,self.engine,self.timeout=store,engine,timeout
        self.journal=journal
        self.active={}

    def start(self,conv_id,message,request_id=None):
        run,created=self.store.start(conv_id,message,request_id)
        if created:
            record={'started':False,'cancel':False,'task':None}
            self.active[run['id']]=record
            self.store.emit(run['id'],'run',{'run_id':run['id'],'conversation_id':run['conversation_id'],'file_changes_recorded':self.journal is not None})
            record['task']=asyncio.create_task(self._drive(run,record))
        return run

    async def _drive(self,run,record):
        record['started']=True
        conv=self.store.load(run['conversation_id']);messages=conv['messages']
        async def emit(event,data):
            self.store.emit(run['id'],event,data)
            await asyncio.sleep(0)
        async def checkpoint():
            self.store.save(conv)
        status='failed'
        try:
            if record['cancel']:
                raise asyncio.CancelledError
            async with asyncio.timeout(self.timeout):
                with self.journal.track(run['id']) if self.journal else nullcontext():
                    status=await self.engine.run(messages,emit,checkpoint)
        except asyncio.CancelledError:
            status='cancelled'
            close_pending(messages,'[CANCELLED] 本次执行已取消；已完成的文件改动保留。')
        except TimeoutError:
            close_pending(messages,'[TIMEOUT] 本次任务超时；请核对实际文件。')
            await emit('error',{'message':'这次任务超时了，已停止继续调用工具。请核对已完成的结果。'})
        except ContextBudgetError as exc:
            close_pending(messages,'[LIMIT] 上下文超出本次限制，未继续执行。')
            await emit('error',{'message':str(exc)})
        except Exception:
            logger.exception('Run failed: %s',run['id'])
            close_pending(messages,'[ERROR] 本次任务中断；请核对实际文件。')
            await emit('error',{'message':'这次任务没有完成。已保留执行记录，请核对结果后再继续。'})
        finally:
            self.store.save(conv)
            self.store.finish(run['id'],status,run['conversation_id'])
            self.active.pop(run['id'],None)

    def cancel(self,run_id):
        run=self.store.run(run_id)
        if not run:
            raise KeyError('任务不存在')
        record=self.active.get(run_id)
        if record and not record['cancel']:
            record['cancel']=True
            if record['started']:
                record['task'].cancel()
        return run

    async def shutdown(self):
        tasks=[]
        for run_id,record in list(self.active.items()):
            self.cancel(run_id);tasks.append(record['task'])
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)

    async def events(self,run_id,after=0):
        idle=0
        while True:
            batch=self.store.events(run_id,after)
            for event in batch:
                after=event['seq'];idle=0
                yield f"id: {after}\nevent: {event['event']}\ndata: {json.dumps(event['data'],ensure_ascii=False)}\n\n"
                if event['event']=='done':
                    return
            run=self.store.run(run_id)
            if not batch and (not run or run['status'] in TERMINAL):
                return
            idle+=1
            if idle>=60:
                yield ': keepalive\n\n';idle=0
            await asyncio.sleep(.1)
