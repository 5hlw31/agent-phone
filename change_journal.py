"""Record file writes from tools, independently of the model's final reply."""
from contextlib import contextmanager
import contextvars
from datetime import datetime
import difflib
import hashlib
import json
from store import now

_ACTIVE=contextvars.ContextVar('agent_change_journal',default=None)


def snapshot(workspace,path):
    try:return workspace.read_bytes(path)
    except FileNotFoundError:return None


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def text_diff(path,before,after):
    if max(len(before or b''),len(after or b''))>256_000:
        return '', '文件较大，仅记录大小和内容校验值。'
    try:
        old=(before or b'').decode('utf-8');new=(after or b'').decode('utf-8')
    except UnicodeDecodeError:return '', '非 UTF-8 文本，仅记录大小和内容校验值。'
    # Keep both generation work and saved output bounded.
    if max(len(old.splitlines()),len(new.splitlines()))>3000:
        return '', '文件行数较多，仅记录大小和内容校验值。'
    parts=[];size=0
    for line in difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),
            fromfile=path if before is not None else '/dev/null',tofile=path if after is not None else '/dev/null',n=3):
        if size+len(line)>24000:
            return ''.join(parts)+'\n[差异较长，后续省略]\n','差异展示已截断。'
        parts.append(line if line.endswith('\n') else line+'\n');size+=len(line)
    return ''.join(parts),''


class ChangeJournal:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS file_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL,
                    before_hash TEXT, after_hash TEXT, before_size INTEGER, after_size INTEGER,
                    diff TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS file_changes_run ON file_changes(run_id,id);
            ''')

    @contextmanager
    def track(self,run_id):
        token=_ACTIVE.set((self,run_id))
        try:yield
        finally:_ACTIVE.reset(token)

    def begin(self,run_id,name,path,before):
        stamp=now()
        with self.store.connect() as db:
            cursor=db.execute('''INSERT INTO file_changes
                (run_id,tool_name,path,status,before_hash,before_size,created_at,updated_at)
                VALUES (?,?,?,'pending',?,?,?,?)''',
                (run_id,name,path,digest(before),len(before) if before is not None else None,stamp,stamp))
            return cursor.lastrowid

    def finish(self,change_id,path,before,after):
        changed=before!=after
        diff,note=text_diff(path,before,after) if changed else ('','文件内容未改变。')
        with self.store.connect() as db:
            db.execute('''UPDATE file_changes SET status=?,after_hash=?,after_size=?,diff=?,note=?,updated_at=? WHERE id=?''',
                ('changed' if changed else 'unchanged',digest(after),len(after) if after is not None else None,diff,note,now(),change_id))

    def report(self,run_id,workspace):
        run=self.store.run(run_id)
        if not run:raise KeyError('任务不存在')
        with self.store.connect() as db:
            changes=[dict(row) for row in db.execute('SELECT * FROM file_changes WHERE run_id=? ORDER BY id',(run_id,))]
            events=db.execute("SELECT event,data FROM run_events WHERE run_id=? AND event IN ('tool_call','tool_result') ORDER BY seq",(run_id,)).fetchall()
            marker=db.execute("SELECT data FROM run_events WHERE run_id=? AND event='run' ORDER BY seq LIMIT 1",(run_id,)).fetchone()
        recorded=bool(changes) or bool(marker and json.loads(marker['data']).get('file_changes_recorded'))
        counts={'calls':0,'succeeded':0,'failed':0,'unconfirmed':0};tools=[]
        for row in events:
            data=json.loads(row['data'])
            if row['event']=='tool_call':
                counts['calls']+=1;tools.append({'name':data.get('tool_name',''),'status':'unconfirmed','call_id':data.get('call_id')})
            else:
                result=str(data.get('result',''))
                status='failed' if result.startswith(('[ERROR]','[BLOCKED]','[TIMEOUT]')) else 'unconfirmed' if result.startswith(('[CANCELLED]','[INTERRUPTED]','[LIMIT]')) else 'succeeded'
                target=next((t for t in tools if t['call_id']==data.get('call_id') and not t.get('received')),None)
                if target:target.update(status=status,received=True)
        for tool in tools:counts[tool['status']]+=1
        files={}
        for change in changes:
            if change['status']=='unchanged':continue
            path=change['path']
            item=files.setdefault(path,{'path':path,'change_ids':[],'has_unconfirmed':False})
            item['change_ids'].append(change['id']);item['last_change_id']=change['id']
            item['after_hash']=change['after_hash'];item['after_size']=change['after_size']
            item['has_unconfirmed']|=change['status']=='pending'
        for item in files.values():
            try:
                raw=snapshot(workspace,item['path']);item['exists']=raw is not None
                item['current_hash']=digest(raw)
                item['current_status']='unconfirmed' if item['has_unconfirmed'] else 'missing' if raw is None else 'matches' if digest(raw)==item['after_hash'] else 'changed'
            except (OSError,PermissionError,ValueError):
                item.update(exists=False,current_hash=None,current_status='unavailable')
        end=datetime.now().astimezone() if run['status']=='running' else datetime.fromisoformat(run['updated_at'])
        seconds=max(0,(end-datetime.fromisoformat(run['created_at'])).total_seconds())
        return {'schema_version':1,'run_id':run_id,'conversation_id':run['conversation_id'],'status':run['status'],
            'created_at':run['created_at'],'updated_at':run['updated_at'],'checked_at':now(),'duration_seconds':round(seconds,2),
            'tool_counts':counts,'tools':[{k:v for k,v in t.items() if k not in ('received','call_id')} for t in tools],
            'files':list(files.values()),'changes':changes,'file_tracking_enabled':recorded,
            'scope':'仅记录本版运行期间通过 write_file / edit_file 发起的写入。文件校验只反映检查时的内容，不能证明任务语义正确。'}


@contextmanager
def observe_write(workspace,name,path):
    active=_ACTIVE.get()
    if active is None:
        yield;return
    journal,run_id=active
    try:
        path=workspace.path(path).relative_to(workspace.root).as_posix()
        before=snapshot(workspace,path)
    except (OSError,PermissionError,ValueError):
        # The real file tool returns its existing, precise rejection.
        yield;return
    change_id=journal.begin(run_id,name,path,before)
    try:yield
    finally:
        # If a hard crash or unreadable target prevents this update, pending survives.
        after=snapshot(workspace,path)
        journal.finish(change_id,path,before,after)
