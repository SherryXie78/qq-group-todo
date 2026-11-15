import concurrent.futures
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from qq_live_digest import pet
from qq_live_digest.config import Settings
from qq_live_digest.store import Store,SCHEMA
from qq_live_digest.webapp import TaskWebServer


class PetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'digest.sqlite3';self.store=Store(self.path)
        self.task=self.store.upsert_task(task_key='a',summary='完成实验报告',category='action')

    def state(self):return pet.snapshot(self.store)
    def complete(self):
        receipt={};self.store.apply_task_action(self.task,'done',reward_receipt=receipt);return receipt

    def test_reward_duplicate_click_reopen_and_debt(self):
        self.assertEqual(self.complete(),{'delta':10,'balance':10})
        self.assertEqual(self.complete(),{'delta':0,'balance':10})
        pet.interact(self.store,{'action':'bonk','request_id':'spend-original-12345'})
        receipt={};self.store.apply_task_action(self.task,'reopen',reward_receipt=receipt)
        self.assertEqual(receipt,{'delta':-10,'balance':-10})
        with self.assertRaises(ValueError):pet.interact(self.store,{'action':'woodfish','request_id':'insufficient-12345'})
        self.complete();self.assertEqual(self.state()['balance'],0)
        self.assertEqual(self.state()['totals'],{'bonk':1})

    def test_concurrent_completion_only_earns_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.store.apply_task_action(self.task,'done'),range(8)))
        self.assertEqual(sum(results),1);self.assertEqual(self.state()['balance'],10)
        self.assertEqual(len(self.state()['history']),1)

    def test_concurrent_spend_cannot_overdraw(self):
        self.complete()
        def buy(i):
            try:pet.interact(self.store,{'action':'feed','request_id':f'concurrent-feed-{i:06}'});return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(buy,range(8)))
        self.assertEqual(sum(results),2);self.assertEqual(self.state()['balance'],0)

    def test_uncertain_retry_does_not_charge_twice(self):
        self.complete();payload={'action':'feed','request_id':'same-request-12345'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(lambda _:pet.interact(self.store,payload),range(3)))
        self.assertEqual(sum(not r['replayed'] for r in results),1)
        self.assertEqual(self.state()['balance'],5)
        with self.assertRaises(ValueError):pet.interact(self.store,dict(payload,action='bonk'))

    def test_legacy_completions_never_get_retroactive_points(self):
        path=Path(self.tmp.name)/'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO tasks(task_key,status,created_at,updated_at) VALUES('old','done','','')")
        db.close()
        store=Store(path);old=store.list_tasks()[0]['id']
        self.assertEqual(pet.snapshot(store)['balance'],0)
        store.apply_task_action(old,'reopen');store.apply_task_action(old,'done')
        self.assertEqual(pet.snapshot(store)['balance'],0)

    def test_milestones_only_reward_whole_task_and_reverse_on_stage_reopen(self):
        plan={'milestones':[{'id':'first','label':'练习','end_date':'2099-01-02','done':False},{'id':'last','label':'实战','end_date':'2099-01-03','done':False}]}
        with self.store._connect() as db:db.execute('UPDATE tasks SET presentation=? WHERE id=?',(json.dumps(plan),self.task))
        self.store.apply_task_action(self.task,'stage_done',detail={'milestone_id':'first'})
        self.assertEqual(self.state()['balance'],0)
        self.store.apply_task_action(self.task,'stage_done',detail={'milestone_id':'last'})
        self.assertEqual(self.state()['balance'],10)
        self.store.apply_task_action(self.task,'stage_done',detail={'milestone_id':'last'})
        self.assertEqual(self.state()['balance'],10)
        self.store.apply_task_action(self.task,'stage_reopen',detail={'milestone_id':'first'})
        self.assertEqual(self.state()['balance'],0)

    def test_server_controls_prices_and_persists_after_restart(self):
        self.complete()
        pet.interact(self.store,{'action':'feed','request_id':'forged-price-12345','cost':-10000,'balance':99999})
        self.assertEqual(pet.snapshot(Store(self.path))['balance'],5)
        for payload in ({'action':'mint','request_id':'invalid-action-12345'},{'action':{},'request_id':'invalid-action-12345'},{'action':'feed','request_id':'short'}):
            with self.assertRaises(ValueError):pet.interact(self.store,payload)

    def test_failed_transaction_rolls_back_task_and_reward(self):
        with self.store._connect() as db:
            db.executescript("CREATE TRIGGER fail_after_reward AFTER INSERT ON pet_ledger BEGIN SELECT RAISE(ABORT,'fixture failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):self.complete()
        self.assertEqual(self.store.get_task(self.task)['status'],'open');self.assertEqual(self.state()['balance'],0)

    def test_avatar_decode_resize_strip_metadata_and_validation(self):
        image=Image.new('RGB',(700,400),'#ae8bd3');raw=io.BytesIO();image.save(raw,'JPEG',comment=b'private photo metadata')
        info=pet.save_avatar(self.store,raw.getvalue());self.assertTrue(info['avatar_hash'])
        stored=pet.avatar(self.store)
        with Image.open(io.BytesIO(stored)) as current:self.assertEqual(current.size,(512,512));self.assertFalse(current.getexif())
        self.assertNotIn(b'private photo metadata',stored)
        for bad in (b'<svg onload="alert(1)"/>',b'not a photo',b'x'*(2*1024*1024+1)):
            with self.assertRaises(ValueError):pet.save_avatar(self.store,bad)
        self.assertFalse(pet.remove_avatar(self.store)['avatar_hash']);self.assertIsNone(pet.avatar(self.store))

    def test_name_validation(self):
        self.assertEqual(pet.save_name(self.store,'  导员小搭子  ')['name'],'导员小搭子')
        for bad in ('','a'*25,'bad\nname',None):
            with self.assertRaises(ValueError):pet.save_name(self.store,bad)


class PetApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'digest.sqlite3');self.task=self.store.upsert_task(task_key='one',summary='仅测试任务')
        self.server=TaskWebServer(Settings(data_dir=self.root,web_host='127.0.0.1',web_port=0,web_token='pet-test-only'),self.store)
        self.server.start();self.base='http://127.0.0.1:'+str(self.server.server.server_address[1])
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):self.server.stop();self.tmp.cleanup()
    def request(self,path,body=None,auth=True,origin=None,raw=None):
        headers={'X-Token':'pet-test-only'} if auth else {}
        if origin:headers['Origin']=origin
        if body is not None:raw=json.dumps(body).encode();headers['Content-Type']='application/json'
        return self.http.open(urllib.request.Request(self.base+path,data=raw,headers=headers),timeout=4)

    def test_auth_csrf_and_body_limits(self):
        for route in ('/api/pet','/api/pet/avatar'):
            for body in (None,{'action':'feed','request_id':'not-authorized-12345'}):
                with self.assertRaises(urllib.error.HTTPError) as error:self.request(route,body,auth=False)
                self.assertEqual(error.exception.code,401)
            with self.assertRaises(urllib.error.HTTPError) as error:self.request(route,{'action':'rename','name':'x'},origin='https://other.invalid')
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/pet',raw=b'x'*1025)
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/pet',body=[])

    def test_completion_receipt_and_shared_wallet(self):
        with self.request('/api/tasks/'+str(self.task),{'action':'done'}) as response:receipt=json.load(response)['reward']
        self.assertEqual(receipt,{'delta':10,'balance':10})
        with self.request('/api/pet',{'action':'woodfish','request_id':'api-woodfish-12345'}) as response:self.assertEqual(json.load(response)['pet']['balance'],8)
        with self.request('/api/tasks') as response:self.assertEqual(json.load(response)['pet']['balance'],8)

    def test_avatar_route_returns_only_normalized_png_and_no_public_access(self):
        raw=io.BytesIO();Image.new('RGB',(80,120),'blue').save(raw,'PNG')
        with self.request('/api/pet/avatar',raw=raw.getvalue()) as response:self.assertTrue(json.load(response)['avatar_hash'])
        with self.request('/api/pet/avatar') as response:
            self.assertEqual(response.headers['Content-Type'],'image/png');self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff');self.assertEqual(response.headers['Cache-Control'],'private, no-store');self.assertTrue(response.read().startswith(b'\x89PNG'))

if __name__=='__main__':unittest.main()
