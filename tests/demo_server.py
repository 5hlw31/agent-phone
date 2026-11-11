import asyncio
import json
from pathlib import Path
import sys
import os
import tempfile
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime import Engine
from main import create_app
from config import Settings
from workspace import Workspace
import tools
ROOT=Path(os.getenv('AGENT_DEMO_DIR',str(Path(tempfile.gettempdir())/'agent-phone-demo'))).resolve()
ws=Workspace(ROOT/'workspace')
if not ws.path('README.md').exists():ws.write_text('README.md','# Agent Phone 本地演示\n\n这个目录用于检查对话、文件工具和执行记录。\n模型回复是固定的测试数据，不代表真实模型效果。\n')
if not ws.path('notes.md').exists():ws.write_text('notes.md','# 面试准备\n\n- 画出工具调用循环\n- 解释 API 配置\n- 练习两分钟介绍\n')
class Stream:
    def __init__(self,chunks):self.chunks=chunks
    def __aiter__(self):return self.iterate()
    async def iterate(self):
        for chunk in self.chunks:
            await asyncio.sleep(.09)
            yield chunk
    async def close(self):pass
class Model:
    def __init__(self):self.chat=NS(completions=NS(create=self.create))
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def create(self,**kwargs):
        messages=kwargs['messages'];question=next((m['content'] for m in reversed(messages) if m['role']=='user'),'')
        def chunk(text=None,calls=None,reason=None):return NS(choices=[NS(delta=NS(content=text,tool_calls=calls,reasoning_content=None),finish_reason=reason)])
        if messages[-1]['role']=='tool':
            failed=messages[-1]['content'].startswith(('[ERROR]','[BLOCKED]'))
            text='这次没有读取成功，工具返回了文件不存在。可以先查看工作区，再选择实际存在的文件。' if failed else '已经处理完成。\n\n- 执行过程保存在这段对话里。\n- 文件可以在右侧工作区打开核对。\n\n这是一条本地模拟回复，用于检查界面和流程。'
            return Stream([chunk(text[:24]),chunk(text[24:],reason='stop')])
        if '慢任务' in question:
            await asyncio.sleep(4)
        if '错误' in question:
            name='read_file';args={'path':'not-found.md'}
        elif '整理' in question or '新建' in question:
            name='write_file';args={'path':'notes/next.md','overwrite':True,'content':'# 下一步\n\n- 理解工具调用循环\n- 检查失败与停止\n- 核对实际文件\n'}
        elif '读取' in question or '代码' in question:
            name='read_file';args={'path':'README.md'}
        else:
            name='list_files';args={}
        call=NS(index=0,id='demo-call',function=NS(name=name,arguments=json.dumps(args,ensure_ascii=False)))
        return Stream([chunk('我先查看相关文件。'),chunk(calls=[call],reason='tool_calls')])
async def execute(name,args):
    await asyncio.sleep(.3)
    with tools.using_workspace(ws):return await tools.execute_tool(name,args)
config=Settings(workspace=ws.root,database=ROOT/'agent.db',api_key='local-demo-only',model='本地演示 · 模拟模型')
engine=Engine(Model,execute,tools.available_definitions(),config.system_prompt,config.model)
app=create_app(config,engine)
