"""Private task rewards and a small companion; no LLM or messaging calls."""
from __future__ import annotations

import hashlib
import io
import re
import warnings
from contextlib import contextmanager

from .timeutil import iso, now_local

REWARD = 10
ACTIONS = {
    'feed': {'name': '喂食', 'cost': 5, 'message': '饼干收到！今天也有好好努力呀。'},
    'bonk': {'name': '殴打', 'cost': 10, 'message': '咚！烦恼飞走，今天辛苦啦。'},
    'woodfish': {'name': '敲木鱼', 'cost': 2, 'message': '咚——烦恼 −1，平静 +1。'},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pet_profile (
    id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL DEFAULT '小搭子',
    avatar BLOB, avatar_hash TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO pet_profile(id) VALUES(1);
CREATE TABLE IF NOT EXISTS pet_rewards (
    task_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pet_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, amount INTEGER NOT NULL,
    kind TEXT NOT NULL, task_id INTEGER, action TEXT NOT NULL DEFAULT '',
    request_id TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pet_ledger_task ON pet_ledger(task_id,id);
-- Only seed once: pre-existing completed tasks never receive retroactive rewards.
INSERT OR IGNORE INTO pet_rewards(task_id,eligible)
    SELECT id,0 FROM tasks WHERE status='done'
    AND NOT EXISTS(SELECT 1 FROM meta WHERE key='pet_rewards_started');
INSERT OR IGNORE INTO meta(key,value) VALUES('pet_rewards_started',strftime('%Y-%m-%dT%H:%M:%S','now'));
-- Task status and reward ledger are one SQLite transaction, including milestones.
CREATE TRIGGER IF NOT EXISTS pet_task_completed AFTER UPDATE OF status ON tasks
WHEN OLD.status <> 'done' AND NEW.status = 'done'
BEGIN
    INSERT OR IGNORE INTO pet_rewards(task_id) VALUES(NEW.id);
    INSERT INTO pet_ledger(amount,kind,task_id,created_at)
        SELECT 10,'reward',NEW.id,NEW.updated_at FROM pet_rewards
        WHERE task_id=NEW.id AND eligible=1 AND active=0;
    UPDATE pet_rewards SET active=1 WHERE task_id=NEW.id AND eligible=1;
END;
CREATE TRIGGER IF NOT EXISTS pet_task_reopened AFTER UPDATE OF status ON tasks
WHEN OLD.status = 'done' AND NEW.status <> 'done'
BEGIN
    INSERT INTO pet_ledger(amount,kind,task_id,created_at)
        SELECT -10,'reversal',NEW.id,NEW.updated_at FROM pet_rewards
        WHERE task_id=NEW.id AND eligible=1 AND active=1;
    UPDATE pet_rewards SET active=0 WHERE task_id=NEW.id AND eligible=1;
END;
"""


def init(connection):
    connection.executescript(SCHEMA)


@contextmanager
def track_task_reward(db,task_id,receipt=None):
    db.execute('BEGIN IMMEDIATE')
    before=db.execute('SELECT COALESCE(SUM(amount),0) FROM pet_ledger WHERE task_id=?',(task_id,)).fetchone()[0]
    yield
    if receipt is not None:
        after=db.execute('SELECT COALESCE(SUM(amount),0) FROM pet_ledger WHERE task_id=?',(task_id,)).fetchone()[0]
        balance=db.execute('SELECT COALESCE(SUM(amount),0) FROM pet_ledger').fetchone()[0]
        receipt.update(delta=after-before,balance=balance)


def snapshot_connection(db):
    profile = db.execute('SELECT name,avatar_hash,updated_at FROM pet_profile WHERE id=1').fetchone()
    balance = db.execute('SELECT COALESCE(SUM(amount),0) FROM pet_ledger').fetchone()[0]
    history = []
    for row in db.execute('SELECT p.*,t.summary FROM pet_ledger p LEFT JOIN tasks t ON t.id=p.task_id ORDER BY p.id DESC LIMIT 20'):
        history.append({'id':row['id'],'amount':row['amount'],'kind':row['kind'],
                        'label':row['summary'] or ACTIONS.get(row['action'],{}).get('name','任务奖励'),
                        'at':row['created_at']})
    totals = {row['action']:row['n'] for row in db.execute("SELECT action,COUNT(*) n FROM pet_ledger WHERE kind='spend' GROUP BY action")}
    return {'name':profile['name'],'avatar_hash':profile['avatar_hash'],
            'avatar_url':('/api/pet/avatar?v='+profile['avatar_hash']) if profile['avatar_hash'] else '',
            'balance':balance,'reward':REWARD,'history':history,'totals':totals,
            'actions':[dict(value,id=key) for key,value in ACTIONS.items()],
            'rules':'每项待办完成 +10；多阶段任务全部完成后统一奖励。撤销完成回退奖励，重新完成补回。同一任务不能反复刷分。旧的已完成任务不追溯。',
            'updated_at':profile['updated_at']}


def snapshot(store):
    with store._connect() as db:
        return snapshot_connection(db)


def interact(store, payload):
    action = payload.get('action')
    request_id = str(payload.get('request_id') or '')
    if not isinstance(action,str) or action not in ACTIONS:
        raise ValueError('请选择喂食、殴打或敲木鱼。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id):
        raise ValueError('互动编号无效，请重新点击。')
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT action FROM pet_ledger WHERE request_id=?',(request_id,)).fetchone()
        if existing and existing['action'] != action:
            raise ValueError('同一次互动的内容发生变化，请重新操作。')
        if not existing:
            balance = db.execute('SELECT COALESCE(SUM(amount),0) FROM pet_ledger').fetchone()[0]
            if balance < ACTIONS[action]['cost']:
                raise ValueError('积分不足，先完成一项待办，再来找我玩吧。')
            db.execute("INSERT INTO pet_ledger(amount,kind,action,request_id,created_at) VALUES(?,'spend',?,?,?)",
                       (-ACTIONS[action]['cost'],action,request_id,iso(now_local())))
        return {'ok':True,'replayed':bool(existing),'action':action,
                'message':ACTIONS[action]['message'],'pet':snapshot_connection(db)}


def save_name(store, value):
    if not isinstance(value,str): raise ValueError('请填写角色昵称。')
    name = value.strip()
    if not 1 <= len(name) <= 24 or any(ord(c)<32 for c in name):
        raise ValueError('昵称请填写 1～24 个字。')
    with store._connect() as db:
        db.execute('UPDATE pet_profile SET name=?,updated_at=? WHERE id=1',(name,iso(now_local())))
    return snapshot(store)


def save_avatar(store, raw):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not raw or len(raw) > 2*1024*1024: raise ValueError('图片请控制在 2 MB 以内。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>20_000_000:
                    raise ValueError('请上传普通 JPG、PNG 或 WebP 图片，分辨率不超过 2000 万像素。')
                image.load()
                result=ImageOps.fit(ImageOps.exif_transpose(image).convert('RGBA'),(512,512))
                output=io.BytesIO();result.save(output,format='PNG');data=output.getvalue()
    except (UnidentifiedImageError,OSError,Image.DecompressionBombWarning,Image.DecompressionBombError) as error:
        raise ValueError('图片无法读取，请换一张 JPG、PNG 或 WebP 图片。') from error
    with store._connect() as db:
        db.execute('UPDATE pet_profile SET avatar=?,avatar_hash=?,updated_at=? WHERE id=1',
                   (data,hashlib.sha256(data).hexdigest()[:20],iso(now_local())))
    return snapshot(store)


def avatar(store):
    with store._connect() as db:
        row=db.execute('SELECT avatar FROM pet_profile WHERE id=1').fetchone()
        return row['avatar'] if row else None


def remove_avatar(store):
    with store._connect() as db:
        db.execute("UPDATE pet_profile SET avatar=NULL,avatar_hash='',updated_at=? WHERE id=1",(iso(now_local()),))
    return snapshot(store)
