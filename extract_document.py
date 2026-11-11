"""Bounded text extraction worker. Receives bytes on stdin, emits JSON only."""
import io
import json
import logging
import resource
import sys
import zipfile
import xml.etree.ElementTree as ET

MAX_CHARS=200_000


def extract(kind,raw):
    notice=''
    if kind=='pdf':
        from pypdf import PdfReader
        logging.getLogger('pypdf').setLevel(logging.CRITICAL)
        reader=PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF 有密码保护，请先在本机解除密码。')
        parts=[];length=0;has_text=False
        for index,page in enumerate(reader.pages):
            if index>=200 or length>=MAX_CHARS:
                notice='正文较长，只提取前 200 页或 20 万字符，请核对后分段处理。';break
            text=page.extract_text() or ''
            has_text=has_text or bool(text.strip())
            parts.append(f'\n\n## 第 {index+1} 页\n\n'+text[:MAX_CHARS-length]);length+=len(text)
        content=''.join(parts).strip() if has_text else ''
        if length>MAX_CHARS:notice='正文较长，只提取前 20 万字符，请核对后分段处理。'
    elif kind=='docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist()
            if len(entries)>1000 or sum(e.file_size for e in entries)>24*1024*1024:
                raise ValueError('Word 解压后内容过大，请精简后上传。')
            xml=archive.read('word/document.xml')
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('文档包含不支持的 XML 定义。')
        root=ET.fromstring(xml);ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        paragraphs=[]
        for paragraph in root.iter(ns+'p'):
            text=''.join((e.text or '') if e.tag==ns+'t' else '\t' if e.tag==ns+'tab' else '\n' if e.tag==ns+'br' else '' for e in paragraph.iter())
            if text.strip():paragraphs.append(text)
        content='\n\n'.join(paragraphs)
        if len(content)>MAX_CHARS:notice='正文较长，只提取前 20 万字符，请核对后分段处理。'
        content=content[:MAX_CHARS]
    else:
        raise ValueError('不支持此文档格式。')
    if not content.strip():notice='没有提取到正文。扫描件和图片暂不支持 OCR，可先转换成文字。'
    return {'content':content,'notice':notice}


if __name__=='__main__':
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(10,10))
    resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
    try:
        data=sys.stdin.buffer.read(10*1024*1024+1)
        if len(data)>10*1024*1024:raise ValueError('文件超过 10 MB 限制。')
        result=extract(sys.argv[1],data)
    except ValueError as exc:result={'error':str(exc)[:200]}
    except Exception:result={'error':'文档无法解析，请确认文件完整，且格式确实是 PDF 或 DOCX。'}
    print(json.dumps(result,ensure_ascii=False))
