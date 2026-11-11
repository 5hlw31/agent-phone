import asyncio
import os
from pathlib import Path
import sys
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workspace import Workspace
import tools


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.ws=Workspace(self.base/'workspace')

    def test_atomic_save_and_nested_read(self):
        self.ws.write_text('notes/a.md','旧内容')
        self.ws.write_text('notes/a.md','新内容')
        self.assertEqual(self.ws.read_text('notes/a.md'),'新内容')
        self.assertEqual(self.ws.listing('notes')[0]['name'],'a.md')

    def test_failed_save_preserves_original(self):
        self.ws.write_text('a.txt','keep')
        with patch('workspace.os.replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):self.ws.write_text('a.txt','lost')
        self.assertEqual(self.ws.read_text('a.txt'),'keep')
        self.assertEqual([p.name for p in self.ws.root.iterdir()],['a.txt'])

    def test_traversal_and_protected_paths(self):
        for path in ['../outside','/etc/passwd','.env','a/.env.local','.git/config','.ssh/id_rsa','private.key']:
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):self.ws.write_text(path,'bad')
        self.assertEqual(self.ws.listing(),[])

    def test_symlink_cannot_read_or_write(self):
        outside=self.base/'outside';outside.mkdir();(outside/'data').write_text('keep')
        (self.ws.root/'link').symlink_to(outside,target_is_directory=True)
        (self.ws.root/'file').symlink_to(outside/'data')
        for path in ['link/data','file']:
            with self.assertRaises(PermissionError):self.ws.read_text(path)
            with self.assertRaises(PermissionError):self.ws.write_text(path,'bad')
        self.assertEqual((outside/'data').read_text(),'keep')
        self.assertEqual(self.ws.listing(),[])

    def test_special_files_and_hardlinks_not_read(self):
        outside=self.base/'secret';outside.write_text('keep')
        os.link(outside,self.ws.root/'linked')
        with self.assertRaises(PermissionError):self.ws.read_text('linked')
        os.mkfifo(self.ws.root/'pipe')
        with self.assertRaises(PermissionError):self.ws.read_text('pipe')

    def test_delete_root_and_nonempty_directory(self):
        self.ws.write_text('dir/file','keep')
        with self.assertRaises(PermissionError):self.ws.delete('')
        with self.assertRaises(OSError):self.ws.delete('dir')
        self.ws.delete('dir/file');self.ws.delete('dir')


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ws=Workspace(Path(self.temp.name)/'workspace')
        self.binding=tools.using_workspace(self.ws);self.binding.__enter__();self.addCleanup(self.binding.__exit__,None,None,None)

    async def test_real_file_tool_roundtrip_and_edit(self):
        self.assertTrue((await tools.execute_tool('write_file',{'path':'a.txt','content':'hello world'})).startswith('Written'))
        self.assertTrue((await tools.execute_tool('read_file',{'path':'a.txt'})).endswith('hello world'))
        result=await tools.execute_tool('edit_file',{'path':'a.txt','old_string':'world','new_string':'吉林大学'})
        self.assertIn('Edited',result);self.assertEqual(self.ws.read_text('a.txt'),'hello 吉林大学')
        self.assertTrue((await tools.execute_tool('edit_file',{'path':'a.txt','old_string':'','new_string':'bad'})).startswith('[ERROR]'))

    async def test_new_file_tool_does_not_silently_overwrite(self):
        self.ws.write_text('a.txt','keep')
        result=await tools.execute_tool('write_file',{'path':'a.txt','content':'replace'})
        self.assertTrue(result.startswith('[ERROR]'));self.assertEqual(self.ws.read_text('a.txt'),'keep')
        await tools.execute_tool('write_file',{'path':'a.txt','content':'replace','overwrite':True})
        self.assertEqual(self.ws.read_text('a.txt'),'replace')

    async def test_read_offset_and_recursive_glob(self):
        self.ws.write_text('a.py','one\ntwo\nthree\nfour')
        result=await tools.execute_tool('read_file',{'path':'a.py','start_line':3,'max_lines':1})
        self.assertIn('three',result);self.assertNotIn('two',result);self.assertIn('第 4 行',result)
        self.assertIn('a.py',await tools._list_files(glob='**/*.py'))
        self.assertIn('a.py:2',await tools._search_code('two',path='a.py'))

    async def test_file_content_cannot_spoof_tool_failure(self):
        self.ws.write_text('error.log','[ERROR] this is a log entry')
        result=await tools.execute_tool('read_file',{'path':'error.log'})
        self.assertFalse(result.startswith('[ERROR]'));self.assertIn('[ERROR] this is',result)
        result=await tools.execute_tool('run_command',{'command':'cat error.log'})
        self.assertFalse(result.startswith('[ERROR]'));self.assertIn('[ERROR] this is',result)

    async def test_validation_before_execution(self):
        for args in [None,[],{'max_results':True},{'max_results':'3'},{'max_results':-1},{'extra':1}]:
            result=await tools.execute_tool('list_files',args)
            self.assertTrue(result.startswith('[ERROR]'),result)
        result=await tools.execute_tool('create_pptx',{'filename':'x','title':'y','slides':['bad']})
        self.assertTrue(result.startswith('[ERROR]'))

    async def test_workspace_commands_and_unsupported_shell(self):
        self.ws.write_text('a.txt','one\ntwo\nthree')
        self.assertEqual(await tools._run_command('head -n 2 a.txt'),'输出：\none\ntwo')
        for command in ['touch a && echo b','echo x>y','echo x;echo y','cat /etc/passwd','python3 -c print(1)']:
            result=await tools._run_command(command)
            self.assertTrue(result.startswith(('[ERROR]','[BLOCKED]')),result)
        self.assertEqual(self.ws.read_text('a.txt'),'one\ntwo\nthree')

    async def test_search_and_listing_skip_links_and_secrets(self):
        self.ws.write_text('a.py','literal ( value')
        (self.ws.root/'.env').write_text('literal SECRET')
        outside=Path(self.temp.name)/'outside';outside.write_text('literal OUTSIDE')
        (self.ws.root/'linked.py').symlink_to(outside)
        result=await tools._search_code('literal')
        self.assertIn('a.py:1',result);self.assertNotIn('SECRET',result);self.assertNotIn('OUTSIDE',result)
        self.assertIn('a.py',await tools._list_files())
        self.assertNotIn('.env',await tools._list_files())
        self.assertIn('a.py',await tools._search_code('('))

    async def test_code_executor_not_exposed_as_sandbox(self):
        self.assertNotIn('code_exec',[d['function']['name'] for d in tools.available_definitions()])
        self.assertTrue((await tools.execute_tool('code_exec',{'code':'print(1)'})).startswith('[BLOCKED]'))

    async def test_process_timeout_and_cancel_reap_owned_child(self):
        for cancelled in [False,True]:
            proc=await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(30)',
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
            task=asyncio.create_task(tools._communicate_or_stop(proc,30 if cancelled else .03))
            if cancelled:
                await asyncio.sleep(.03);task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancelled else asyncio.TimeoutError):await task
            self.assertIsNotNone(proc.returncode)

    async def test_process_output_is_bounded_and_reaped(self):
        proc=await asyncio.create_subprocess_exec(sys.executable,'-c','import sys,time; print("x"*200000,flush=True); time.sleep(30)',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
        with self.assertRaises(ValueError):await tools._communicate_or_stop(proc,3,max_output_bytes=1000)
        self.assertIsNotNone(proc.returncode)

    @unittest.skipUnless(shutil.which('git'),'Git is not installed')
    async def test_git_read_tools_work_and_do_not_inherit_credentials(self):
        env={'PATH':os.defpath,'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}
        def git(*args):
            subprocess.run(['git','-c','commit.gpgsign=false','-C',str(self.ws.root),*args],check=True,env=env,capture_output=True)
        git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        self.ws.write_text('a.txt','one');git('add','a.txt');git('commit','-m','fixture')
        self.ws.write_text('a.txt','two')
        original=tools.asyncio.create_subprocess_exec;environments=[]
        async def spawn(*args,**kwargs):
            environments.append(kwargs.get('env',{}));return await original(*args,**kwargs)
        with patch.object(tools.asyncio,'create_subprocess_exec',spawn):
            self.assertIn('a.txt',await tools.execute_tool('git_status',{}))
            self.assertIn('+two',await tools.execute_tool('git_diff',{}))
            self.assertIn('fixture',await tools.execute_tool('git_log',{}))
        self.assertTrue(environments)
        self.assertTrue(all('DEEPSEEK_API_KEY' not in env and 'AUTH_TOKEN' not in env for env in environments))

    @unittest.skipUnless(shutil.which('git'),'Git is not installed')
    async def test_git_rejects_external_filters_and_includes(self):
        subprocess.run(['git','init',str(self.ws.root)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.ws.root),'config','filter.demo.clean','echo filtered'],check=True)
        self.assertTrue((await tools.execute_tool('git_status',{})).startswith('[BLOCKED]'))
        subprocess.run(['git','-C',str(self.ws.root),'config','--remove-section','filter.demo'],check=True)
        subprocess.run(['git','-C',str(self.ws.root),'config','includeIf.gitdir:/never/.path','/nonexistent'],check=True)
        self.assertTrue((await tools.execute_tool('git_status',{})).startswith('[BLOCKED]'))
