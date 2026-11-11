import asyncio
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from config import Settings
from main import create_app
from uploads import extract_document
import tools


def word_bytes(text='中文课程报告 &amp; notes'):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('word/document.xml',f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return output.getvalue()


def pdf_bytes(blank=False):
    writer=PdfWriter();page=writer.add_blank_page(width=300,height=300)
    if not blank:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 30 200 Td (Interview document upload test) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    output=io.BytesIO();writer.write(output);return output.getvalue()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        self.config=Settings(workspace=root/'workspace',database=root/'data.db',token='test-token')
        self.app=create_app(self.config);self.client=TestClient(self.app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'Authorization':'Bearer test-token','Content-Type':'application/octet-stream'}

    def upload(self,name,raw):return self.client.post('/api/files/upload',params={'path':name},content=raw,headers=self.headers)

    def test_text_encoding_and_duplicate_names_preserve_both_files(self):
        first=self.upload('你好.txt','第一份'.encode('gb18030')).json()
        second=self.upload('你好.txt','第二份'.encode()).json()
        self.assertEqual(first['path'],'你好.txt');self.assertEqual(second['path'],'你好 (2).txt');self.assertTrue(second['renamed'])
        self.assertEqual((self.config.workspace/'你好.txt').read_text(),'第一份')

    def test_word_original_and_extracted_reference_roundtrip(self):
        original=word_bytes();response=self.upload('资料/报告.docx',original)
        self.assertEqual(response.status_code,200,response.text);data=response.json()
        self.assertTrue(data['readable']);self.assertIn('中文课程报告 & notes',(self.config.workspace/data['reference_path']).read_text())
        self.assertEqual(self.client.get('/api/files/download',params={'path':data['path']},headers=self.headers).content,original)
        preview=self.client.get('/api/files',params={'path':data['path']},headers=self.headers).json()
        self.assertFalse(preview['editable']);self.assertIn('中文课程报告',preview['content'])
        self.assertEqual(self.client.post('/api/files',json={'path':data['path'],'content':'bad'},headers=self.headers).status_code,400)
        async def read():
            with tools.using_workspace(self.app.state.workspace):
                self.assertTrue((await tools.execute_tool('write_file',{'path':data['path'],'content':'bad','overwrite':True})).startswith('[ERROR]'))
                self.assertTrue((await tools.execute_tool('edit_file',{'path':data['path'],'old_string':'中文','new_string':'bad'})).startswith('[ERROR]'))
                return await tools.execute_tool('read_file',{'path':data['path']})
        self.assertIn('中文课程报告',asyncio.run(read()))

    def test_pdf_text_and_blank_document_are_distinguished(self):
        full=self.upload('paper.pdf',pdf_bytes())
        self.assertEqual(full.status_code,200,full.text)
        self.assertIn('Interview document',(self.config.workspace/full.json()['reference_path']).read_text())
        blank=self.upload('scan.pdf',pdf_bytes(blank=True)).json()
        self.assertFalse(blank['readable']);self.assertIsNone(blank['reference_path']);self.assertIn('OCR',blank['notice'])

    def test_upload_requires_auth_and_rejects_paths_without_writing(self):
        self.assertEqual(self.client.post('/api/files/upload?path=a.txt',content=b'a').status_code,401)
        for name in ('../outside.txt','.env','/etc/passwd'):
            self.assertEqual(self.upload(name,b'no').status_code,403)
        self.assertEqual(list(self.config.workspace.iterdir()),[])

    def test_limits_empty_and_unsupported_formats_are_clear(self):
        self.assertEqual(self.upload('a.txt',b'').status_code,400)
        self.assertEqual(self.upload('a.txt',b'x'*(10*1024*1024+1)).status_code,413)
        for name in ('a.exe','a.zip','photo.png','old.doc'):
            self.assertEqual(self.upload(name,b'test').status_code,400)
        self.assertEqual(self.upload('a.txt',b'binary\x00text').status_code,400)
        self.assertEqual(list(self.config.workspace.iterdir()),[])

    def test_invalid_documents_are_not_saved(self):
        for name in ('bad.pdf','bad.docx'):
            result=self.upload(name,b'not a document');self.assertEqual(result.status_code,400,result.text)
        self.assertEqual(list(self.config.workspace.iterdir()),[])

    def test_word_xml_entities_and_expansion_are_rejected(self):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('word/document.xml','<!DOCTYPE a [<!ENTITY x "abc">]><a>&x;</a>')
        result=self.upload('entity.docx',output.getvalue());self.assertEqual(result.status_code,400);self.assertIn('XML',result.json()['detail'])
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:archive.writestr('word/document.xml','x'*(25*1024*1024))
        result=self.upload('large.docx',output.getvalue());self.assertEqual(result.status_code,400);self.assertIn('过大',result.json()['detail'])

    def test_collision_with_existing_extraction_does_not_overwrite_it(self):
        (self.config.workspace/'a.docx.text.md').write_text('keep')
        data=self.upload('a.docx',word_bytes()).json()
        self.assertEqual(data['path'],'a (2).docx');self.assertEqual((self.config.workspace/'a.docx.text.md').read_text(),'keep')
