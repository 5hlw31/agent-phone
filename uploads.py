"""Upload validation and bounded document extraction, separate from model tools."""
import asyncio
import json
import os
from pathlib import Path
import sys

DOCUMENTS={'.pdf','.docx'}
TEXT_EXTENSIONS=set('txt md markdown csv tsv json jsonl yaml yml toml ini conf cfg log py js jsx ts tsx vue svelte html htm css scss c h cpp hpp java go rs rb php sh sql xml svg tex rst r gitignore dockerfile makefile'.split())


def upload_kind(path):
    name=Path(path).name
    suffix=Path(name).suffix.lower()
    if suffix in DOCUMENTS:return suffix[1:]
    if suffix.lstrip('.') in TEXT_EXTENSIONS or name.lower() in {'readme','license','dockerfile','makefile','.gitignore'}:
        return 'text'
    raise ValueError('暂时支持 PDF、DOCX、文本和代码文件；图片、旧版 DOC 和压缩包请先转换或解压。')


async def extract_document(kind,raw):
    process=await asyncio.create_subprocess_exec(sys.executable,'-I',str(Path(__file__).with_name('extract_document.py')),kind,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
        env={'PATH':os.defpath,'LANG':'C.UTF-8'})
    try:
        try:output,_=await asyncio.wait_for(process.communicate(raw),15)
        except asyncio.TimeoutError:raise ValueError('文档解析超时，请精简文档后重试。')
        if process.returncode or len(output)>2*1024*1024:raise ValueError('文档解析超出限制，请精简文档后重试。')
        result=json.loads(output)
        if result.get('error'):raise ValueError(result['error'])
        return result
    finally:
        if process.returncode is None:
            process.kill();await process.communicate()


def decode_text(raw):
    for encoding in ('utf-8-sig','utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8','gb18030'):
        try:
            text=raw.decode(encoding)
            if '\x00' in text:raise ValueError('该文件不是可读取的文本。')
            return text
        except UnicodeError:pass
    raise ValueError('无法识别文字编码，请转为 UTF-8 后上传。')


def unique_path(workspace,path,document=False):
    file=Path(path)
    for index in range(1,1001):
        candidate=str(file if index==1 else file.with_name(f'{file.stem} ({index}){file.suffix}'))
        if not workspace.path(candidate).exists() and (not document or not workspace.path(candidate+'.text.md').exists()):return candidate
    raise ValueError('同名文件太多，请换一个文件名。')
