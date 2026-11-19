import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核", parent TEXT, replaced_by TEXT)')
    columns={r['name'] for r in db.execute('PRAGMA table_info(runs)').fetchall()}
    # 兼容快照前已存在的库：补上谱系列，旧批次视为当前版本。
    for column in ('parent','replaced_by'):
        if column not in columns:db.execute('ALTER TABLE runs ADD COLUMN %s TEXT'%column)
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    return db

def save(run_id, body, parent=None):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body,parent) VALUES (?,?,?)',(run_id,json.dumps(body,ensure_ascii=False),parent))
        # 已替换的旧批次不再指向“最新”，重新导入同一来账时不会把旧版顶回工作台。
        current=db.execute('SELECT replaced_by FROM runs WHERE id=?',(run_id,)).fetchone()
        if current and not current['replaced_by']:
            db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body'])
    body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'],
        parent=r['parent'],replaced_by=r['replaced_by'],
        status='已替换' if r['replaced_by'] else '当前')
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision,parent,replaced_by FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r,status='已替换' if r['replaced_by'] else '当前') for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        row=db.execute('SELECT replaced_by FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        if row['replaced_by']:raise DomainError('STALE','该批次已被替换，请复核接替批次',409)
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def replace(parent_id, body, version):
    """以“新批次接替旧批次”的方式替换来账，全部在一个事务内完成。

    - 版本不符、父批次已替换、或新内容重算失败：整笔不写，旧版原样保留；
    - 成功：旧批次正文不变，只标记 replaced_by，仍可完整下载解释原来账；
    - 新批次号仍由 输入+参数+规则版本 计算，因此之后再次导入同一来账
      会落到同一批次，不会多出一份已分配金额。
    """
    new_id=body['run_id']
    encoded=json.dumps(body,ensure_ascii=False)
    with connection() as db:
        row=db.execute('SELECT * FROM runs WHERE id=?',(parent_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','批次不存在',404)
        if row['version']!=version:raise DomainError('STALE','批次版本已更新，保留旧版不替换',409)
        if row['replaced_by']:raise DomainError('STALE','该批次已被替换，旧版仍可下载',409)
        if new_id==parent_id:
            # 与原批次完全相同的来账：幂等，不新建也不重复分配。
            return get(parent_id)
        db.execute('INSERT OR IGNORE INTO runs(id,body,parent) VALUES (?,?,?)',(new_id,encoded,parent_id))
        cur=db.execute('UPDATE runs SET replaced_by=?,version=version+1 WHERE id=? AND version=?',(new_id,parent_id,version))
        if cur.rowcount!=1:
            # 触发上下文管理器回滚，新插入的批次与旧批次的标记一起撤销。
            raise DomainError('STALE','批次版本已更新，保留旧版不替换',409)
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(new_id,))
    return get(new_id)
