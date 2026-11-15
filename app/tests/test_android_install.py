import hashlib,json,tempfile,unittest,urllib.request,urllib.error
from unittest.mock import patch
from pathlib import Path
from qq_live_digest.android_install import APK_NAME,release_info,DOWNLOAD_TTL,download_info,valid_download_ticket
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer


class AndroidInstallTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);(self.root/'data').mkdir();(self.root/'android-release').mkdir()
  self.body=b'APK fixture only; contains no credentials.'
  (self.root/'android-release'/APK_NAME).write_bytes(self.body)
  self.info={'filename':APK_NAME,'version':'2026.10.24.1','bytes':len(self.body),'sha256':hashlib.sha256(self.body).hexdigest()}
  (self.root/'android-release/release.json').write_text(json.dumps(self.info))
  self.settings=Settings(data_dir=self.root/'data',web_host='127.0.0.1',web_port=0,web_token='local-fixture-token')
  self.server=TaskWebServer(self.settings,Store(self.root/'data/digest.sqlite3'));self.assertTrue(self.server.start());self.addCleanup(self.server.stop)
  self.base='http://127.0.0.1:'+str(self.server.server.server_address[1])
  self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def get(self,path,headers=None,auth=True):
  supplied={'X-Token':self.settings.web_token} if auth else {};supplied.update(headers or {})
  return self.http.open(urllib.request.Request(self.base+path,headers=supplied),timeout=5)
 def test_all_installer_routes_require_auth(self):
  for path in ('/android','/api/mobile/android','/api/mobile/android.apk'):
   with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as caught:self.get(path,auth=False)
   self.assertEqual(caught.exception.code,401)
 def test_install_page_establishes_private_cookie_and_removes_query(self):
  with self.get('/android?token='+self.settings.web_token,auth=False) as response:
   body=response.read().decode();self.assertIn('HttpOnly',response.headers['Set-Cookie']);self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
   self.assertIn('history.replaceState',body);self.assertIn('先保留旧 Tailscale',body)
   self.assertNotIn('__TOKEN_JSON__',body)
 def test_apk_download_is_fixed_and_never_cacheable(self):
  with self.get('/api/mobile/android.apk?file=../private/deepseek.dpapi') as response:
   self.assertEqual(response.read(),self.body);self.assertEqual(response.headers['Content-Type'],'application/vnd.android.package-archive');self.assertIn('no-store',response.headers['Cache-Control'])
 def test_partial_download_can_resume(self):
  with self.get('/api/mobile/android.apk',{'Range':'bytes=4-10'}) as response:
   self.assertEqual(response.status,206);self.assertEqual(response.read(),self.body[4:11]);self.assertEqual(response.headers['Content-Range'],f'bytes 4-10/{len(self.body)}')
  with self.get('/api/mobile/android.apk',{'Range':'bytes=10-'}) as response:self.assertEqual(response.read(),self.body[10:])
 def test_invalid_ranges_are_rejected_without_file_content(self):
  for value in ('bytes=500-','bytes=-20','bytes=3-1','bytes=0-3,8-9','invalid'):
   with self.subTest(value=value),self.assertRaises(urllib.error.HTTPError) as caught:self.get('/api/mobile/android.apk',{'Range':value})
   self.assertEqual(caught.exception.code,416);self.assertEqual(caught.exception.read(),b'')
 def test_release_metadata_and_missing_or_mismatched_file(self):
  with self.get('/api/mobile/android') as response:self.assertTrue(json.load(response)['ready'])
  (self.root/'android-release'/APK_NAME).write_bytes(b'incomplete')
  self.assertFalse(release_info(self.root)['ready'])
  with self.assertRaises(urllib.error.HTTPError) as caught:self.get('/api/mobile/android.apk')
  self.assertEqual(caught.exception.code,404)

 def test_system_downloader_ticket_needs_no_browser_cookie(self):
  with self.get('/api/mobile/android') as response:info=json.load(response)
  self.assertNotIn(self.settings.web_token,info['download_url'])
  with self.get(info['download_url'],auth=False) as response:self.assertEqual(response.read(),self.body)
  with self.get(info['download_url'],{'Range':'bytes=5-'},auth=False) as response:self.assertEqual(response.status,206);self.assertEqual(response.read(),self.body[5:])

 def test_ticket_is_expiring_release_bound_and_tamper_proof(self):
  with patch('qq_live_digest.android_install.time.time',return_value=1800000000):
   info=download_info(self.root,self.settings.web_token)
   ticket=info['download_url'].split('download_ticket=')[1]
   self.assertTrue(valid_download_ticket(self.root,self.settings.web_token,ticket))
   self.assertFalse(valid_download_ticket(self.root,'different-secret',ticket))
   self.assertFalse(valid_download_ticket(self.root,self.settings.web_token,ticket[:-1]+('a' if ticket[-1]!='a' else 'b')))
   changed=dict(self.info,sha256='0'*64);(self.root/'android-release/release.json').write_text(json.dumps(changed))
   self.assertFalse(valid_download_ticket(self.root,self.settings.web_token,ticket))
   (self.root/'android-release/release.json').write_text(json.dumps(self.info))
  with patch('qq_live_digest.android_install.time.time',return_value=1800000000+DOWNLOAD_TTL+1):self.assertFalse(valid_download_ticket(self.root,self.settings.web_token,ticket))
  with patch('qq_live_digest.android_install.time.time',return_value=1799999999):self.assertFalse(valid_download_ticket(self.root,self.settings.web_token,ticket))

 def test_ticket_cannot_access_workbench_tasks_or_post(self):
  with self.get('/api/mobile/android') as response:url=json.load(response)['download_url']
  query=url.split('?')[1]
  for path in ('/api/tasks?'+query,'/api/pet?'+query,'/android?'+query):
   with self.assertRaises(urllib.error.HTTPError) as error:self.get(path,auth=False)
   self.assertEqual(error.exception.code,401)
  with self.assertRaises(urllib.error.HTTPError) as error:self.http.open(urllib.request.Request(self.base+url,method='POST',data=b'{}'),timeout=5)
  self.assertEqual(error.exception.code,401)

 def test_head_probe_and_anonymous_download_diagnostics(self):
  with self.get('/api/mobile/android') as response:url=json.load(response)['download_url']
  with self.http.open(urllib.request.Request(self.base+url,method='HEAD'),timeout=5) as response:
   self.assertEqual(int(response.headers['Content-Length']),len(self.body));self.assertEqual(response.read(),b'')
  with self.assertRaises(urllib.error.HTTPError) as error:self.http.open(urllib.request.Request(self.base+'/api/mobile/android.apk',method='HEAD'),timeout=5)
  self.assertEqual(error.exception.code,401)
  text=(self.root/'runtime/android-download-events.json').read_text()
  self.assertNotIn(self.settings.web_token,text);self.assertNotIn('download_ticket',text);self.assertNotIn(url,text)
  self.assertEqual(json.loads(text)[-1]['status'],401)

 def test_install_page_offers_progress_and_verification(self):
  with self.get('/android') as response:page=response.read().decode()
  for marker in ('browser-download','save-apk',"crypto.subtle.digest('SHA-256'",'Content-Range','继续网页内下载'):self.assertIn(marker,page)


if __name__=='__main__':unittest.main()
