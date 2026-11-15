import dataclasses,json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from qq_live_digest.config import Settings
from qq_live_digest.mobile_access import MobileAccess

class MobileTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'data').mkdir();(self.root/'runtime').mkdir()
  (self.root/'consent.json').write_text(json.dumps({'mobile_access_approved':True}))
  self.settings=dataclasses.replace(Settings(),data_dir=self.root/'data',web_token='test-only-token')
  self.node={'BackendState':'Running','Self':{'Online':True,'UserID':1,'DNSName':'test.tail-private.ts.net.'},'Peer':{'own':{'OS':'android','Online':True,'UserID':1},'other':{'OS':'android','Online':True,'UserID':2}}}
  self.config={'Web':{'test.tail-private.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8766'}}}},'AllowFunnel':{}}
 def tearDown(self):self.tmp.cleanup()
 def manager(self):return MobileAccess(self.settings,runner=lambda args:self.node if args[0]=='status' else self.config)
 def test_private_pairing_and_same_account_phone(self):
  m=self.manager();s=m.status();self.assertTrue(s['private_ready']);self.assertEqual(s['android_online'],1)
  self.assertIn('<svg',m.qr_svg('workbench'));m.note_phone_visit();self.assertTrue((self.root/'runtime/mobile-visit.json').exists())
 def test_public_funnel_refuses_pairing(self):
  self.config['AllowFunnel']={'test.tail-private.ts.net:443':True}
  m=self.manager();self.assertFalse(m.status()['private_ready'])
  with self.assertRaises(ValueError):m.qr_svg('workbench')
 def test_no_consent_or_login_never_exposes_pairing(self):
  self.node={'BackendState':'NeedsLogin','Self':None,'Peer':None}
  self.assertFalse(self.manager().status()['private_ready'])
  (self.root/'consent.json').write_text('{}');m=self.manager();self.assertFalse(m.status()['approved'])
  with self.assertRaises(ValueError):m.qr_svg('workbench')
 def test_unrelated_proxy_and_invalid_domain_refused(self):
  self.config['Web']['test.tail-private.ts.net:443']['Handlers']['/']['Proxy']='http://127.0.0.1:5199'
  self.assertFalse(self.manager().status()['private_ready'])
  self.node['Self']['DNSName']='not-your-workbench.example/'
  self.assertFalse(self.manager().status()['private_ready'])
 def test_continue_shows_only_fixed_native_pairing_when_ready(self):
  m=self.manager()
  with patch('qq_live_digest.mobile_access.subprocess.Popen') as spawn:
   self.assertTrue(m.begin_setup('continue')['ok'])
   args=spawn.call_args.args[0]
   self.assertEqual(args[-1],str(self.root/'Show-Mobile-Pairing.py'))
   self.assertNotIn(self.settings.web_token,' '.join(args))
  with self.assertRaises(ValueError):m.begin_setup('unknown')
  (self.root/'consent.json').write_text('{}')
  with self.assertRaises(ValueError):m.begin_setup('continue')
