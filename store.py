"""SQLite conversations, task states and replayable events."""
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import uuid
from contextlib import contextmanager

TERMINAL = {'completed', 'cancelled', 'failed', 'interrupted', 'limited'}


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
                    messages TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, request_id TEXT UNIQUE, request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_run_per_conversation
                    ON runs(conversation_id) WHERE status='running';
                CREATE TABLE IF NOT EXISTS run_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    event TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_events_lookup ON run_events(run_id,seq);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_conversation(self, title='新对话', system=''):
        conv = {'id': uuid.uuid4().hex, 'title': title[:100] or '新对话',
                'messages': [{'role':'system','content':system}] if system else [],
                'created_at': now(), 'updated_at': now()}
        with self.connect() as db:
            db.execute('INSERT INTO conversations VALUES (?,?,?,?,?)',
                       (conv['id'], conv['title'], json.dumps(conv['messages'],ensure_ascii=False),conv['created_at'],conv['updated_at']))
        return conv

    def load(self, conv_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM conversations WHERE id=?',(conv_id,)).fetchone()
        if not row:
            return None
        result=dict(row);result['messages']=json.loads(result['messages'])
        return result

    def save(self, conv):
        conv['updated_at']=now()
        with self.connect() as db:
            db.execute('UPDATE conversations SET messages=?,updated_at=? WHERE id=?',
                       (json.dumps(conv['messages'],ensure_ascii=False),conv['updated_at'],conv['id']))

    def conversations(self):
        with self.connect() as db:
            rows=db.execute('''SELECT c.id,c.title,c.created_at,c.updated_at,
                (SELECT id FROM runs WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) AS last_run_id,
                (SELECT status FROM runs WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) AS last_run_status
                FROM conversations c ORDER BY updated_at DESC''').fetchall()
        return [dict(r) for r in rows]

    def delete(self, conv_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND status='running'",(conv_id,)).fetchone():
                raise Conflict('请先停止这个对话中的任务')
            db.execute('DELETE FROM conversations WHERE id=?',(conv_id,))

    def start(self, conv_id, message, request_id=None):
        fingerprint=hashlib.sha256(json.dumps([conv_id,message],ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                previous=db.execute('SELECT * FROM runs WHERE request_id=?',(request_id,)).fetchone()
                if previous:
                    if previous['request_hash'] != fingerprint:
                        raise Conflict('这个请求编号已用于另一项任务')
                    return dict(previous),False
            if conv_id:
                row=db.execute('SELECT * FROM conversations WHERE id=?',(conv_id,)).fetchone()
                if not row:
                    raise KeyError('对话不存在')
                if db.execute("SELECT 1 FROM runs WHERE conversation_id=? AND status='running'",(conv_id,)).fetchone():
                    raise Conflict('这段对话还有任务在执行，请等待或先停止')
                conv=dict(row);conv['messages']=json.loads(conv['messages'])
            else:
                conv={'id':uuid.uuid4().hex,'title':message[:60].replace('\n',' '),
                      'messages':[],'created_at':now(),'updated_at':now()}
                db.execute('INSERT INTO conversations VALUES (?,?,?,?,?)',
                           (conv['id'],conv['title'],'[]',conv['created_at'],conv['updated_at']))
            run_id=uuid.uuid4().hex
            conv['messages'].append({'role':'user','content':message,'_run_id':run_id})
            db.execute('UPDATE conversations SET messages=?,updated_at=? WHERE id=?',
                       (json.dumps(conv['messages'],ensure_ascii=False),now(),conv['id']))
            run={'id':run_id,'conversation_id':conv['id'],'status':'running',
                 'request_id':request_id,'request_hash':fingerprint,'created_at':now(),'updated_at':now()}
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',tuple(run.values()))
            return run,True

    def run(self, run_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self, conv_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1',(conv_id,)).fetchone()
        return dict(row) if row else None

    def emit(self, run_id, event, data):
        with self.connect() as db:
            cursor=db.execute('INSERT INTO run_events(run_id,event,data) VALUES (?,?,?)',
                              (run_id,event,json.dumps(data,ensure_ascii=False)))
            return cursor.lastrowid

    def events(self, run_id, after=0):
        with self.connect() as db:
            rows=db.execute('SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200',
                            (run_id,after)).fetchall()
        return [{'seq':r['seq'],'event':r['event'],'data':json.loads(r['data'])} for r in rows]

    def finish(self, run_id, status, conversation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE runs SET status=?,updated_at=? WHERE id=?',(status,now(),run_id))
            data={'run_id':run_id,'conversation_id':conversation_id,'status':status}
            db.execute('INSERT INTO run_events(run_id,event,data) VALUES (?,?,?)',(run_id,'done',json.dumps(data)))

    def recover(self):
        with self.connect() as db:
            rows=db.execute("SELECT id,conversation_id FROM runs WHERE status='running'").fetchall()
        for row in rows:
            conv=self.load(row['conversation_id'])
            if conv:
                messages=conv['messages']
                start=next((i for i,m in enumerate(messages) if m.get('_run_id')==row['id']),len(messages))
                with self.connect() as db:
                    events=db.execute('SELECT event,data FROM run_events WHERE run_id=? ORDER BY seq',(row['id'],)).fetchall()
                round_no=0;partial=[]
                for event in events:
                    data=json.loads(event['data'])
                    if event['event']=='phase' and data.get('phase')=='model':
                        round_no=data.get('round',0);partial=[]
                    elif event['event']=='delta':
                        partial.append(data.get('content',''))
                assistants=sum(m.get('role')=='assistant' for m in messages[start:])
                if partial and assistants<round_no:
                    messages.append({'role':'assistant','content':''.join(partial),'_partial':True})
                pending={}
                for message in messages[start:]:
                    for call in message.get('tool_calls',[]):
                        pending[call['id']]=True
                    if message.get('role')=='tool':
                        pending.pop(message.get('tool_call_id'),None)
                for call_id in pending:
                    messages.append({'role':'tool','tool_call_id':call_id,
                        'content':'[INTERRUPTED] 服务中断前没有记录到结果，请先核对实际文件。'})
                self.save(conv)
            self.emit(row['id'],'error',{'message':'服务重启，这次任务已中断。请核对工作区结果后再继续。'})
            self.finish(row['id'],'interrupted',row['conversation_id'])
