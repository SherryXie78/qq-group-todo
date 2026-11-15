import dataclasses,json,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer

class ControlsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)
  self.store=Store(p/'digest.sqlite3');self.stopped=threading.Event()
  settings=dataclasses.replace(Settings(),data_dir=p,web_host='127.0.0.1',web_port=0,web_token='isolated-test-token')
  self.a=self.store.upsert_task(task_key='a',summary='测试日程甲',deadline='2099-01-01T12:00:00',status='open')
  self.store.upsert_task(task_key='b',summary='测试日程乙',deadline='2099-01-02T12:00:00',status='open')
  self.store.upsert_task(task_key='past',summary='过去事项',deadline='2000-01-01T12:00:00',status='open')
  self.store.upsert_task(task_key='done',summary='已完成事项',deadline='2099-01-02T12:00:00',status='done')
  self.server=TaskWebServer(settings,self.store,stop_handler=self.stopped.set);self.server.start()
  self.base='http://127.0.0.1:'+str(self.server.server.server_address[1])
 def tearDown(self):self.server.stop();self.tmp.cleanup()
 def call(self,path,method='GET',auth=True,origin=None):
  headers={'X-Token':'isolated-test-token'} if auth else {}
  if origin:headers['Origin']=origin
  return urllib.request.urlopen(urllib.request.Request(self.base+path,method=method,headers=headers),timeout=3)
 def test_calendar_preview_and_selection(self):
  with self.call('/api/calendar/preview') as r:tasks=json.load(r)['tasks']
  self.assertEqual(len(tasks),2)
  with self.call('/api/calendar.ics?ids='+str(self.a)) as r:text=r.read().decode()
  self.assertIn('测试日程甲',text);self.assertNotIn('测试日程乙',text);self.assertNotIn('过去事项',text)
 def test_stop_requires_auth_and_same_origin(self):
  for auth,origin in [(False,None),(True,'https://unrelated.invalid')]:
   with self.assertRaises(urllib.error.HTTPError):self.call('/api/app/stop','POST',auth,origin)
  self.assertFalse(self.stopped.is_set())
  with self.call('/api/app/stop','POST') as r:self.assertTrue(json.load(r)['ok'])
  self.assertTrue(self.stopped.wait(2))
 def test_refresh_session_and_cookie_csrf(self):
  with self.call('/') as r:cookie=r.headers['Set-Cookie']
  self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
  with urllib.request.urlopen(urllib.request.Request(self.base+'/',headers={'Cookie':cookie.split(';')[0]})) as r:self.assertEqual(r.status,200)
  with self.assertRaises(urllib.error.HTTPError):self.call('/',auth=False)
  with self.assertRaises(urllib.error.HTTPError):
   urllib.request.urlopen(urllib.request.Request(self.base+'/api/app/stop',method='POST',headers={'Cookie':cookie.split(';')[0],'Origin':'https://other.invalid'}))
 def test_private_proxy_keeps_auth_and_secure_cookie(self):
  headers={'Host':'test.private.ts.net','Tailscale-User-Login':'test@example.invalid'}
  with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(urllib.request.Request(self.base+'/',headers=headers))
  headers['X-Token']='isolated-test-token'
  with urllib.request.urlopen(urllib.request.Request(self.base+'/',headers=headers)) as r:self.assertIn('; Secure',r.headers['Set-Cookie'])
