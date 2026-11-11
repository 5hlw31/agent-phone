"""Run eight synthetic file tasks, with scripted or explicitly enabled live models."""
import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import zipfile
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from agent_runtime import Engine
from config import Settings
from main import create_app
from workspace import Workspace
import tools
from evaluations.cases import CASES
from evaluations.grading import file_state,grade

FILE_TOOLS={'read_file','write_file','edit_file','list_files','search_code'}


class Budget:
    def __init__(self,maximum):self.maximum=maximum;self.calls=0;self.returned_models=set()
    def take(self):
        if self.calls>=self.maximum:raise RuntimeError('evaluation_model_call_budget_exhausted')
        self.calls+=1


class ScriptedStream:
    def __init__(self,text=None,call=None):self.text=text;self.call=call
    def __aiter__(self):return self.iterate()
    async def iterate(self):
        delta=NS(content=self.text,tool_calls=[self.call] if self.call else [],reasoning_content=None)
        yield NS(choices=[NS(delta=delta,finish_reason='tool_calls' if self.call else 'stop')])
    async def close(self):pass


class ScriptedClient:
    def __init__(self,case,budget,outside_path):
        self.case,self.budget,self.outside_path=case,budget,outside_path;self.index=0
        self.chat=NS(completions=NS(create=self.create))
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def create(self,**kwargs):
        self.budget.take()
        if self.index<len(self.case['plan']):
            name,args=self.case['plan'][self.index];self.index+=1
            args={key:value.replace('{outside_path}',self.outside_path) if isinstance(value,str) else value for key,value in args.items()}
            call=NS(index=0,id='script-'+str(self.index),function=NS(name=name,arguments=json.dumps(args,ensure_ascii=False)))
            return ScriptedStream(call=call)
        return ScriptedStream(text=self.case['reply'])


class ObservedStream:
    def __init__(self,stream,budget,errors):self.stream,self.budget,self.errors=stream,budget,errors
    def __aiter__(self):return self.iterate()
    async def iterate(self):
        try:
            async for chunk in self.stream:
                if getattr(chunk,'model',None):self.budget.returned_models.add(chunk.model)
                yield chunk
        except Exception as exc:
            self.errors.append({'type':type(exc).__name__,'http_status':getattr(exc,'status_code',None)});raise
    async def close(self):await self.stream.close()


class LiveClient:
    def __init__(self,config,budget,errors):
        from openai import AsyncOpenAI
        self.client=AsyncOpenAI(api_key=config.api_key,base_url=config.base_url,timeout=30,max_retries=0)
        self.budget,self.errors=budget,errors
        self.chat=NS(completions=NS(create=self.create))
    async def __aenter__(self):await self.client.__aenter__();return self
    async def __aexit__(self,*args):await self.client.__aexit__(*args)
    async def create(self,**kwargs):
        self.budget.take()
        try:return ObservedStream(await self.client.chat.completions.create(**kwargs),self.budget,self.errors)
        except Exception as exc:
            self.errors.append({'type':type(exc).__name__,'http_status':getattr(exc,'status_code',None)})
            raise


def word_bytes(text):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


def run_case(case,mode,settings,budget):
    started=time.monotonic();initial_calls=budget.calls;trace=[];errors=[]
    with tempfile.TemporaryDirectory(prefix='agent-file-eval-') as folder:
        base=Path(folder);ws=Workspace(base/'workspace');outside=base/'outside.txt'
        if 'outside_text' in case:outside.write_text(case['outside_text'])
        for path,content in case['fixtures'].items():ws.write_text(path,content)
        config=Settings(workspace=ws.root,database=base/'evaluation.db',api_key=settings.api_key,
                        base_url=settings.base_url,model=settings.model,timeout=90)
        factory=(lambda:ScriptedClient(case,budget,str(outside))) if mode=='scripted' else (lambda:LiveClient(config,budget,errors))
        async def execute(name,args):
            if name not in FILE_TOOLS:result='[BLOCKED] 本评估仅开放文件工具。'
            else:
                with tools.using_workspace(ws):result=await tools.execute_tool(name,args)
            trace.append({'name':name,'arguments':args,'result':result});return result
        options={'temperature':0}
        if mode=='live' and urlsplit(settings.base_url).hostname=='api.deepseek.com':
            options['extra_body']={'thinking':{'type':'disabled'}}
        engine=Engine(factory,execute,[d for d in tools.available_definitions() if d['function']['name'] in FILE_TOOLS],
                      config.system_prompt,config.model,max_rounds=4,max_calls=8,max_output_tokens=768,request_options=options)
        app=create_app(config,engine)
        with TestClient(app) as client:
            if 'docx' in case:
                document=case['docx'];uploaded=client.post('/api/files/upload',params={'path':document['path']},
                    content=word_bytes(document['text']),headers={'Content-Type':'application/octet-stream'})
                uploaded.raise_for_status()
            before=file_state(ws.root)
            run=client.post('/api/runs',json={'message':case['prompt'].replace('{outside_path}',str(outside)),
                                            'request_id':'evaluation-'+case['id']}).json()
            deadline=time.monotonic()+95
            while time.monotonic()<deadline:
                status=client.get('/api/runs/'+run['id']).json()['status']
                if status!='running':break
                time.sleep(.03)
            else:
                client.post('/api/runs/'+run['id']+'/cancel',json={})
                raise TimeoutError('evaluation_deadline_exceeded')
            messages=app.state.store.load(run['conversation_id'])['messages']
            final=[m.get('content') or '' for m in messages if m['role']=='assistant' and not m.get('tool_calls')]
            reply=final[-1] if final else ''
            judged=grade(case,ws.root,before,reply,status,trace,outside if 'outside_text' in case else None)
            receipt=client.get('/api/runs/'+run['id']+'/report').json()
            record={'case_id':case['id'],'group':case['group'],'title':case['title'],'run_status':status,
                    **judged,'duration_seconds':round(time.monotonic()-started,3),'model_calls':budget.calls-initial_calls,
                    'tool_counts':receipt['tool_counts'],'provider_errors':errors,'reply':reply,'tool_trace':trace,
                    'messages':[{k:v for k,v in m.items() if k in ('role','content','tool_calls','tool_call_id')} for m in messages],
                    'before_files':before,'after_files':file_state(ws.root),'file_report':receipt}
            # Make reports portable; model credentials and hidden reasoning are never exported.
            return json.loads(json.dumps(record,ensure_ascii=False).replace(str(base),'<sandbox>'))


def source_hashes():
    names=['main.py','agent_runtime.py','tools.py','workspace.py','store.py','change_journal.py','config.py','uploads.py','extract_document.py',
           'evaluations/cases.py','evaluations/grading.py','evaluations/run_suite.py','requirements-linux311.txt','requirements-tested.txt']
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['scripted','live'],default='scripted')
    parser.add_argument('--allow-api',action='store_true',help='Explicitly permit paid/network model requests in live mode')
    parser.add_argument('--env-file',type=Path,help='Read model credentials only from this explicitly selected file')
    parser.add_argument('--model');parser.add_argument('--max-model-calls',type=int,default=32)
    parser.add_argument('--output',type=Path,default=ROOT/'evaluations/results/latest.json')
    args=parser.parse_args(argv)
    if args.max_model_calls<1 or args.max_model_calls>32:parser.error('--max-model-calls must be between 1 and 32')
    values={}
    if args.mode=='live':
        if not args.allow_api:parser.error('live mode requires --allow-api')
        if args.env_file:
            from dotenv import dotenv_values
            values=dict(dotenv_values(args.env_file))
        key=os.getenv('DEEPSEEK_API_KEY') or values.get('DEEPSEEK_API_KEY','')
        if not key:parser.error('DEEPSEEK_API_KEY is required for live mode')
    else:key='scripted-only'
    settings=Settings(api_key=key,model=args.model or values.get('DEEPSEEK_MODEL') or os.getenv('DEEPSEEK_MODEL') or 'deepseek-flash',
                      base_url=values.get('DEEPSEEK_BASE_URL') or os.getenv('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com')
    if args.mode=='scripted':settings.model='scripted'
    budget=Budget(args.max_model_calls);results=[]
    for case in CASES:
        if budget.calls>=budget.maximum:
            results.append({'case_id':case['id'],'group':case['group'],'title':case['title'],'not_run':'model_call_budget_exhausted'});continue
        try:record=run_case(case,args.mode,settings,budget)
        except Exception as exc:record={'case_id':case['id'],'group':case['group'],'title':case['title'],'infrastructure_error':type(exc).__name__}
        results.append(record)
        print(json.dumps({'case':case['id'],'passed':record.get('passed'),'infrastructure_error':record.get('infrastructure_error')},ensure_ascii=False),flush=True)
    complete=all('passed' in r for r in results)
    summary={}
    for group in ('normal','boundary'):
        rows=[r for r in results if r['group']==group]
        summary[group]={'planned':len(rows),'recorded':sum('passed' in r for r in rows),
            'passed':sum(r.get('passed',False) for r in rows),'goals_satisfied':sum(r.get('goal_satisfied',False) for r in rows),
            'infrastructure_errors':sum(bool(r.get('provider_errors')) or 'infrastructure_error' in r for r in rows),
            'constraint_violations':sum(r.get('constraints_respected') is False for r in rows)}
    report={'suite':'synthetic-file-tasks-v1','schema_version':1,'mode':args.mode,
            'interpretation':'Scripted mode checks the harness, not model capability.' if args.mode=='scripted' else 'One trial per fixed case; not a general success rate or security benchmark.',
            'created_at':datetime.now(timezone.utc).isoformat(),'requested_model':settings.model,'returned_models':sorted(budget.returned_models),
            'python':platform.python_version(),'platform':platform.system(),'enabled_tools':sorted(FILE_TOOLS),
            'provider_host':urlsplit(settings.base_url).hostname if args.mode=='live' else None,
            'dependencies':{name:version(name) for name in ('fastapi','openai','httpx','pypdf')},
            'limits':{'max_rounds':4,'max_tool_calls_per_task':8,'max_output_tokens':768,'temperature':0,'max_model_requests':budget.maximum,
                      'task_timeout_seconds':90,'model_request_timeout_seconds':30,'thinking':'disabled' if args.mode=='live' and urlsplit(settings.base_url).hostname=='api.deepseek.com' else 'not_overridden'},
            'model_requests_used':budget.calls,'complete':complete,'source_sha256':source_hashes(),'summary':summary,'cases':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix(args.output.suffix+'.tmp');temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2));temporary.replace(args.output)
    print(json.dumps({'mode':args.mode,'complete':complete,'summary':summary,'model_requests':budget.calls},ensure_ascii=False),flush=True)
    return 0 if complete and all(r['passed'] for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
