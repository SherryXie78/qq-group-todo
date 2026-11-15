import json,os,tempfile,unittest,urllib.error,urllib.request
from pathlib import Path
from unittest.mock import Mock,patch
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.qq_session import QQSession,read_json,run_action,write_job
from qq_live_digest.webapp import TaskWebServer


class Fixture(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  for folder in ('runtime','SnowLuma/config','app/.venv/Scripts','data','normal-client'):(self.root/folder).mkdir(parents=True,exist_ok=True)
  self.write('consent.json',{'qq_main_account_approved':True});self.write('runtime/snowluma-coexistence.json',{})
  self.write('SnowLuma/config/approved-account.json',{'uin':'123456789'})
  (self.root/'QQ-Login-Control.py').write_text('# test placeholder')
  (self.root/'app/.venv/Scripts/pythonw.exe').write_bytes(b'fake executable; not run')
  (self.root/'normal-client/QQ.exe').write_bytes(b'fake client; not run')
  self.write('runtime/qq-client.json',{'executable':str(self.root/'normal-client/QQ.exe')})
  self.settings=Settings(data_dir=self.root/'data',web_host='127.0.0.1',web_port=0,web_token='fixture-only-token')
  self.spawn=Mock();self.now=2000000000.;self.session=QQSession(self.settings,spawn=self.spawn,clock=lambda:self.now)
 def write(self,file,value):
  (self.root/file).write_text(json.dumps(value),encoding='utf-8')
 def health(self,**values):
  self.write('runtime/collector-health.json',dict({'at':self.now*1000,'online':False,'state':'waiting_for_qq','mode':'shared-desktop-qq'},**values))


class StatusTests(Fixture):
 def test_live_health_is_online_and_only_exposes_masked_account(self):
  self.health(online=True,state='online',matched_process_count=1)
  result=self.session.status();self.assertTrue(result['online']);self.assertEqual(result['state'],'online')
  self.assertIn('789',result['account_label']);self.assertNotIn('123456789',json.dumps(result));self.assertTrue(result['last_checked'].endswith('+08:00'))
 def test_stale_or_future_health_cannot_claim_login_has_expired(self):
  for stamp in ((self.now-36)*1000,(self.now+10)*1000,'invalid',float('inf')):
   self.health(online=True,at=stamp)
   result=self.session.status();self.assertFalse(result['online']);self.assertEqual(result['state'],'unresponsive');self.assertIn('不能据此判断',result['message'])
 def test_waiting_account_and_attaching_have_different_instructions(self):
  self.health();self.assertEqual(self.session.status()['state'],'login_required')
  self.health(account_confirmed_process_count=1);self.assertEqual(self.session.status()['state'],'account_unconfirmed')
  self.health(matched_process_count=1);self.assertEqual(self.session.status()['state'],'connecting')
 def test_explicit_stop_and_wrong_profile_do_not_revive_old_login(self):
  self.health(state='stopped',at=0);self.assertEqual(self.session.status()['state'],'stopped')
  self.health(mode='napcat',online=True);self.assertFalse(self.session.status()['online'])
  self.assertEqual(self.session.status()['state'],'not_configured')
 def test_no_original_consent_cannot_launch_anything(self):
  self.write('consent.json',{'qq_main_account_approved':False})
  with self.assertRaises(ValueError):self.session.begin('open_login')
  self.spawn.assert_not_called()
 def test_old_action_failure_does_not_remain_as_current_login_error(self):
  self.health(online=True,state='online')
  write_job(self.root,{'id':'old','status':'error','requested_at':self.now-600,'message':'old failure'})
  result=self.session.status();self.assertTrue(result['online']);self.assertFalse(result['action']['visible'])
 def test_repeated_clicks_share_one_action_and_do_not_pass_provider_key(self):
  self.health()
  with patch.dict(os.environ,{'QQ_ASSISTANT_DEEPSEEK_KEY':'synthetic-key'}):
   first=self.session.begin('open_login');second=self.session.begin('reconnect')
  self.assertEqual(first['action']['id'],second['action']['id']);self.assertTrue(second['already_running']);self.assertEqual(self.spawn.call_count,1)
  args=self.spawn.call_args.args[0];self.assertEqual(args[-2],'open_login');self.assertNotIn('NapCat',' '.join(args));self.assertNotIn('QQ_ASSISTANT_DEEPSEEK_KEY',self.spawn.call_args.kwargs['env'])
 def test_failed_spawn_is_visible_and_old_running_job_is_not_permanent(self):
  self.spawn.side_effect=OSError('secret-looking exception must not be displayed')
  result=self.session.begin('reconnect');self.assertFalse(result['ok']);self.assertNotIn('secret-looking',json.dumps(result))
  job=read_json(self.root/'runtime/qq-login-action.json');job.update(status='running',requested_at=self.now-101);write_job(self.root,job)
  self.assertEqual(self.session.status()['action']['status'],'interrupted')
 def test_worker_opens_only_normal_qq_and_starts_shared_collector(self):
  result=self.session.begin('open_login');run=Mock();spawn=Mock()
  run_action(self.root,'open_login',result['action']['id'],run=run,spawn=spawn)
  self.assertEqual(spawn.call_args.args[0],[str(self.root/'normal-client/QQ.exe')])
  self.assertEqual(Path(run.call_args.args[0][-2]).name,'Start-SnowLuma.ps1')
  self.assertEqual(read_json(self.root/'runtime/qq-login-action.json')['status'],'done')
 def test_recovery_restarts_only_collector_never_qq(self):
  result=self.session.begin('reconnect');run=Mock();spawn=Mock()
  run_action(self.root,'reconnect',result['action']['id'],run=run,spawn=spawn)
  scripts=[next(v for v in call.args[0] if v.endswith('.ps1')) for call in run.call_args_list]
  self.assertEqual([Path(v).name for v in scripts],['Stop-SnowLuma.ps1','Start-SnowLuma.ps1']);spawn.assert_not_called()
 def test_existing_qq_window_is_restored_without_launching_second_client(self):
  result=self.session.begin('open_login');run=Mock();spawn=Mock();focus=Mock(return_value=True)
  run_action(self.root,'open_login',result['action']['id'],run=run,spawn=spawn,focus=focus)
  focus.assert_called_once_with(self.root/'normal-client/QQ.exe');spawn.assert_not_called()
  self.assertEqual(read_json(self.root/'runtime/qq-login-action.json')['status'],'done')
 def test_old_protocol_executable_is_rejected_without_starting_it(self):
  folder=self.root/'NapCat';folder.mkdir();(folder/'QQ.exe').write_bytes(b'not allowed')
  self.write('runtime/qq-client.json',{'executable':str(folder/'QQ.exe')});result=self.session.begin('open_login');spawn=Mock();run=Mock()
  run_action(self.root,'open_login',result['action']['id'],spawn=spawn,run=run)
  spawn.assert_not_called();run.assert_not_called();self.assertEqual(read_json(self.root/'runtime/qq-login-action.json')['status'],'error')


class ApiTests(Fixture):
 def setUp(self):
  super().setUp();self.health(online=True,state='online')
  self.server=TaskWebServer(self.settings,Store(self.root/'data/digest.sqlite3'),qq_session=self.session);self.server.start();self.addCleanup(self.server.stop)
  self.base='http://127.0.0.1:'+str(self.server.server.server_address[1]);self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def call(self,method='GET',body=None,auth=True,origin=None):
  headers={'X-Token':self.settings.web_token} if auth else {}
  if origin:headers['Origin']=origin
  value=None if body is None else json.dumps(body).encode()
  return self.http.open(urllib.request.Request(self.base+'/api/qq-session',data=value,method=method,headers=headers),timeout=5)
 def test_status_is_authenticated_and_tasks_use_same_status(self):
  with self.assertRaises(urllib.error.HTTPError) as caught:self.call(auth=False)
  self.assertEqual(caught.exception.code,401)
  with self.call() as response:self.assertTrue(json.load(response)['online'])
  request=urllib.request.Request(self.base+'/api/tasks',headers={'X-Token':self.settings.web_token})
  with self.http.open(request,timeout=5) as response:self.assertEqual(json.load(response)['collector']['state'],'online')
 def test_actions_require_auth_and_same_origin(self):
  for auth,origin,code in ((False,None,401),(True,'https://unrelated.invalid',403)):
   with self.assertRaises(urllib.error.HTTPError) as caught:self.call('POST',{'action':'open_login'},auth,origin)
   self.assertEqual(caught.exception.code,code)
  self.spawn.assert_not_called()
 def test_no_arbitrary_actions_bodies_or_executable_parameters(self):
  for body in ({'action':'send_group_msg'},{'action':'logout'},[],{'action':'x'*300}):
   with self.assertRaises(urllib.error.HTTPError) as caught:self.call('POST',body)
   self.assertEqual(caught.exception.code,400)
  with self.call('POST',{'action':'open_login','executable':'C:/Windows/cmd.exe'}) as response:self.assertTrue(json.load(response)['ok'])
  self.assertEqual(self.spawn.call_count,1);self.assertTrue(self.spawn.call_args.args[0][1].endswith('QQ-Login-Control.py'))


if __name__=='__main__':unittest.main()
