"""Agent Phone: conversations, replayable runs and a bounded personal workspace."""
from contextlib import asynccontextmanager
import asyncio
import json
import hashlib
import logging
from pathlib import Path
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

from agent_runtime import Engine, RunManager
from config import BASE_DIR, Settings
from store import Conflict, Store
from change_journal import ChangeJournal
import tools
from workspace import Workspace, MAX_FILE_BYTES
from uploads import DOCUMENTS, decode_text, extract_document, unique_path, upload_kind

logger=logging.getLogger('agent-phone')


def _display_messages(messages):
    visible=[];cards={}
    for message in messages:
        role=message.get('role')
        if role=='system':
            continue
        if role in ('assistant','user'):
            if message.get('content'):
                item={'role':role,'content':message['content']}
                if message.get('_run_id'):
                    item['run_id']=message['_run_id']
                if message.get('_partial'):
                    item['partial']=True
                visible.append(item)
            for call in message.get('tool_calls',[]):
                function=call.get('function',{})
                try:
                    arguments=json.loads(function.get('arguments','{}'))
                except (ValueError,TypeError):
                    arguments={}
                card={'role':'tool','call_id':call['id'],'tool_name':function.get('name','tool'),
                      'arguments':arguments if isinstance(arguments,dict) else {},'result':'','status':'unknown'}
                cards[call['id']]=card;visible.append(card)
        elif role=='tool':
            call_id=message.get('tool_call_id','')
            card=cards.get(call_id)
            if card is None:
                card={'role':'tool','call_id':call_id,'tool_name':'tool','arguments':{}}
                visible.append(card)
            result=str(message.get('content',''))
            card['result']=result
            card['status']='cancelled' if result.startswith(('[CANCELLED]','[INTERRUPTED]','[LIMIT]')) else 'error' if result.startswith(('[ERROR]','[BLOCKED]','[TIMEOUT]')) else 'complete'
    return visible


async def body_object(request):
    try:
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>24*1024*1024:
                raise HTTPException(413,'请求内容过大')
            data.extend(chunk)
        body=json.loads(data)
    except (ValueError,UnicodeDecodeError):
        raise HTTPException(400,'请求内容格式不正确')
    if not isinstance(body,dict):
        raise HTTPException(400,'请求内容必须是对象')
    return body


async def authenticated(request: Request):
    config=request.app.state.config
    if not config.token:
        return
    header=request.headers.get('Authorization','')
    if not header.startswith('Bearer ') or not secrets.compare_digest(header[7:].encode(),config.token.encode()):
        raise HTTPException(401,'请先登录')


def create_app(config=None,engine=None):
    config=config or Settings.from_env()
    if config.password and not config.token:
        config.token=secrets.token_urlsafe(32)
    workspace=Workspace(config.workspace)
    store=Store(config.database)
    journal=ChangeJournal(store)
    using_real_engine=engine is None
    if engine is None:
        def factory():
            return AsyncOpenAI(api_key=config.api_key,base_url=config.base_url,timeout=35,max_retries=0)
        async def execute(name,arguments):
            with tools.using_workspace(workspace):
                return await tools.execute_tool(name,arguments)
        options={}
        from urllib.parse import urlsplit
        if urlsplit(config.base_url).hostname=='api.deepseek.com' and config.thinking in ('enabled','disabled'):
            options['extra_body']={'thinking':{'type':config.thinking}}
        engine=Engine(factory,execute,tools.available_definitions(),config.system_prompt,config.model,
                      max_rounds=config.max_rounds,max_calls=config.max_calls,request_options=options)
    manager=RunManager(store,engine,config.timeout,journal)

    @asynccontextmanager
    async def lifespan(app):
        import fcntl
        lock=open(str(config.database)+'.lock','a')
        try:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('此版本应使用一个服务进程；该数据库已有运行中的实例。')
            store.recover()
            yield
        finally:
            await manager.shutdown()
            lock.close()

    app=FastAPI(title='Agent Phone',version='0.3.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.config=config;app.state.workspace=workspace
    app.state.store=store;app.state.manager=manager
    app.state.journal=journal
    attempts={}
    upload_slots=asyncio.Semaphore(2)

    @app.exception_handler(Conflict)
    async def conflict(request,exc):
        return Response(json.dumps({'detail':str(exc)},ensure_ascii=False),status_code=409,media_type='application/json')

    @app.exception_handler(PermissionError)
    async def denied(request,exc):
        return Response(json.dumps({'detail':str(exc)},ensure_ascii=False),status_code=403,media_type='application/json')

    @app.exception_handler(FileNotFoundError)
    async def missing(request,exc):
        return Response('{"detail":"文件不存在"}',status_code=404,media_type='application/json')

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return Response(json.dumps({'detail':str(exc)},ensure_ascii=False),status_code=400,media_type='application/json')

    @app.middleware('http')
    async def response_headers(request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        if request.url.path.startswith('/static/'):
            response.headers['Cache-Control']='no-cache'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.get('/',response_class=HTMLResponse)
    async def index():
        return HTMLResponse((BASE_DIR/'static/index.html').read_text(),headers={'Cache-Control':'no-cache'})

    @app.get('/api/auth/status')
    async def auth_status():
        return {'login_required':bool(config.token),'auth_required':bool(config.token),'password_login':bool(config.password)}

    @app.post('/api/auth/login')
    async def login(request: Request):
        body=await body_object(request)
        password=body.get('password')
        if not isinstance(password,str):
            raise HTTPException(400,'请输入口令')
        host=request.client.host if request.client else 'local'
        stamp=time.monotonic();recent=[t for t in attempts.get(host,[]) if stamp-t<60]
        if len(recent)>=8:
            raise HTTPException(429,'尝试次数较多，请稍后再试')
        recent.append(stamp);attempts[host]=recent
        if len(attempts)>1000:
            attempts.clear();attempts[host]=recent
        if not config.password or not secrets.compare_digest(password.encode(),config.password.encode()):
            raise HTTPException(403,'口令不正确')
        attempts.pop(host,None)
        return {'ok':True,'token':config.token}

    @app.get('/api/auth/check',dependencies=[Depends(authenticated)])
    async def auth_check():
        return {'ok':True,'logged_in':True}

    @app.get('/api/health',dependencies=[Depends(authenticated)])
    async def health():
        return {'status':'ok','version':app.version,'model':config.model,'model_configured':bool(config.api_key),
                'tool_count':len(tools.available_definitions()),'tools':tools.capability_status()}

    @app.get('/api/conversations',dependencies=[Depends(authenticated)])
    async def conversations():
        return {'conversations':store.conversations()}

    @app.post('/api/conversations',dependencies=[Depends(authenticated)])
    async def new_conversation(request: Request):
        body=await body_object(request)
        title=body.get('title','新对话')
        if not isinstance(title,str) or len(title)>100:
            raise HTTPException(400,'对话标题格式不正确')
        conv=store.new_conversation(title)
        return {'id':conv['id'],'title':conv['title']}

    @app.get('/api/conversations/{conv_id}',dependencies=[Depends(authenticated)])
    async def conversation(conv_id: str):
        conv=store.load(conv_id)
        if not conv:
            raise HTTPException(404,'对话不存在')
        messages=_display_messages(conv['messages'])
        run=store.latest_run(conv_id)
        start_index=next((i for i,m in enumerate(messages) if run and m.get('run_id')==run['id']),None)
        return {'id':conv['id'],'title':conv['title'],'messages':messages,'run':run,
                'run_start_index':start_index,'created_at':conv['created_at'],'updated_at':conv['updated_at']}

    @app.delete('/api/conversations/{conv_id}',dependencies=[Depends(authenticated)])
    async def remove_conversation(conv_id: str):
        if not store.load(conv_id):
            raise HTTPException(404,'对话不存在')
        store.delete(conv_id)
        return {'ok':True}

    async def start_run(request):
        body=await body_object(request)
        message=body.get('message');conv_id=body.get('conversation_id');request_id=body.get('request_id')
        if not isinstance(message,str) or not message.strip():
            raise HTTPException(400,'请输入任务内容')
        if len(message)>12000:
            raise HTTPException(413,'任务内容请控制在 12000 字以内')
        if conv_id is not None and (not isinstance(conv_id,str) or not conv_id or len(conv_id)>80):
            raise HTTPException(400,'对话编号不正确')
        if request_id is not None and (not isinstance(request_id,str) or not request_id or len(request_id)>80):
            raise HTTPException(400,'请求编号不正确')
        if not config.api_key and using_real_engine:
            raise HTTPException(503,'模型服务尚未配置')
        try:
            return manager.start(conv_id,message.strip(),request_id)
        except KeyError:
            raise HTTPException(404,'对话不存在')

    @app.post('/api/runs',dependencies=[Depends(authenticated)],status_code=201)
    async def create_run(request: Request):
        return await start_run(request)

    @app.post('/api/chat/send',dependencies=[Depends(authenticated)])
    async def chat_send(request: Request):
        run=await start_run(request)
        return StreamingResponse(manager.events(run['id']),media_type='text/event-stream',
                                 headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/runs/{run_id}',dependencies=[Depends(authenticated)])
    async def get_run(run_id: str):
        run=store.run(run_id)
        if not run:
            raise HTTPException(404,'任务不存在')
        return run

    @app.get('/api/runs/{run_id}/events',dependencies=[Depends(authenticated)])
    async def run_events(run_id: str,request: Request,after: int=0):
        if not store.run(run_id):
            raise HTTPException(404,'任务不存在')
        try:
            after=max(after,int(request.headers.get('Last-Event-ID','0')))
        except ValueError:
            raise HTTPException(400,'事件编号不正确')
        return StreamingResponse(manager.events(run_id,max(0,after)),media_type='text/event-stream',
                                 headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/runs/{run_id}/report',dependencies=[Depends(authenticated)])
    async def run_report(run_id: str):
        try:
            async with tools.write_lock():
                return journal.report(run_id,workspace)
        except KeyError:
            raise HTTPException(404,'任务不存在')

    @app.post('/api/runs/{run_id}/cancel',dependencies=[Depends(authenticated)])
    async def cancel_run(run_id: str):
        try:
            run=manager.cancel(run_id)
        except KeyError:
            raise HTTPException(404,'任务不存在')
        return {'ok':True,'run_id':run_id,'status':run['status']}

    @app.get('/api/files',dependencies=[Depends(authenticated)])
    async def files(path: str=''):
        resolved=workspace.path(path)
        if resolved.is_dir():
            return {'type':'dir','name':resolved.name,'path':resolved.relative_to(workspace.root).as_posix(),
                    'entries':workspace.listing(path)}
        raw=workspace.read_bytes(path);content=raw.decode('utf-8',errors='replace')
        metadata={}
        if resolved.suffix.lower() in DOCUMENTS:
            text_path=path+'.text.md'
            content=workspace.read_text(text_path) if workspace.path(text_path).is_file() else ''
            metadata={'editable':False,'readable':bool(content),'reference_path':text_path if content else None,
                'notice':'提取的正文；排版、表格和图片请核对原文件。' if content else '没有可供助手读取的正文；可重新上传以提取文字。'}
        return {'type':'file','path':resolved.relative_to(workspace.root).as_posix(),'name':resolved.name,
                'content':content,'size':len(raw),'revision':hashlib.sha256(raw).hexdigest(),**metadata}

    @app.post('/api/files/upload',dependencies=[Depends(authenticated)])
    async def upload_file(request: Request,path: str):
        if not path.strip() or len(path)>500 or any(ord(c)<32 for c in path):
            raise HTTPException(400,'文件名格式不正确')
        workspace.relative(path)
        kind=upload_kind(path)
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>MAX_FILE_BYTES:raise HTTPException(413,'文件超过 10 MB 限制')
            raw.extend(chunk)
        if not raw:raise HTTPException(400,'文件为空，请选择有内容的文件')
        async with upload_slots:
            if kind=='text':
                content=decode_text(bytes(raw));notice=''
                stored=content.encode('utf-8')
                if len(stored)>MAX_FILE_BYTES:raise HTTPException(413,'转换为 UTF-8 后超过 10 MB，请精简文件')
            else:
                document=await extract_document(kind,bytes(raw));content=document['content'];notice=document['notice'];stored=bytes(raw)
            if await request.is_disconnected():raise HTTPException(499,'上传连接已取消')
            async with tools.write_lock():
                saved_path=unique_path(workspace,path,document=kind!='text')
                reference_path=saved_path if kind=='text' else saved_path+'.text.md' if content else None
                workspace.write_bytes(saved_path,stored)
                try:
                    if kind!='text' and content:
                        workspace.write_text(reference_path,f'# 文档正文\n\n原文件：{saved_path}\n\n'+(f'提取提示：{notice}\n\n' if notice else '')+content)
                except BaseException:
                    workspace.delete(saved_path);raise
        return {'ok':True,'path':saved_path,'reference_path':reference_path,'size':len(stored),'kind':kind,
                'renamed':saved_path!=path,'notice':notice,'readable':reference_path is not None}

    @app.post('/api/files',dependencies=[Depends(authenticated)])
    async def save_file(request: Request):
        body=await body_object(request);path=body.get('path');content=body.get('content','');is_dir=body.get('is_dir',False)
        if not isinstance(path,str) or not path.strip() or not isinstance(content,str) or not isinstance(is_dir,bool):
            raise HTTPException(400,'文件内容格式不正确')
        if not is_dir and Path(path).suffix.lower() in DOCUMENTS:
            raise HTTPException(400,'PDF 和 Word 请使用上传入口，正文副本可以单独编辑。')
        async with tools.write_lock():
            if path.endswith('/') or is_dir:
                workspace.mkdir(path)
            else:
                if workspace.path(path).exists():
                    revision=hashlib.sha256(workspace.read_bytes(path)).hexdigest()
                    if body.get('expected_revision')!=revision:
                        raise Conflict('文件已经存在或有新的改动，请重新读取后再保存。')
                workspace.write_text(path,content)
        return {'ok':True,'path':workspace.path(path).relative_to(workspace.root).as_posix()}

    @app.delete('/api/files',dependencies=[Depends(authenticated)])
    async def delete_file(path: str,revision: str=''):
        async with tools.write_lock():
            target=workspace.path(path)
            if target.is_file() and hashlib.sha256(workspace.read_bytes(path)).hexdigest()!=revision:
                raise Conflict('文件已有新改动，请重新读取后再删除。')
            try:
                workspace.delete(path)
            except OSError:
                raise HTTPException(400,'只能删除普通文件或空目录')
        return {'ok':True}

    @app.get('/api/files/download',dependencies=[Depends(authenticated)])
    async def download(path: str):
        data=workspace.read_bytes(path)
        from urllib.parse import quote
        return Response(data,media_type='application/octet-stream',headers={
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote(workspace.path(path).name)})

    app.mount('/static',StaticFiles(directory=BASE_DIR/'static'),name='static')
    return app



if __name__=='__main__':
    import uvicorn
    uvicorn.run('main:create_app',factory=True,host='127.0.0.1',port=8000)
