"""Group edits must change intake safely, persist, and keep existing records."""
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from qq_live_digest.config import Settings
from qq_live_digest.groups import GroupManager
from qq_live_digest.receiver import OneBotReceiver
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer


class GroupManagementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'groups.json'
        self.settings = Settings(group_whitelist=('123456',), group_aliases={'123456':'原群'},
                                 web_host='127.0.0.1', web_port=0, web_token='test-token',
                                 attachments_enabled=False)
        self.manager = GroupManager(self.path, self.settings)
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3')
        self.web = TaskWebServer(self.settings, self.store, group_manager=self.manager)
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.base = 'http://127.0.0.1:' + str(self.web.server.server_port)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, payload=None, *, token=True, origin=None, raw=None):
        headers = {'Content-Type':'application/json'}
        if token: headers['X-Token'] = 'test-token'
        if origin: headers['Origin'] = origin
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(self.base + '/api/groups', data=body, headers=headers)
        try: response = self.http.open(req)
        except urllib.error.HTTPError as error: response = error
        with response: return response.status, json.load(response)

    def test_add_rename_and_restart(self):
        label = '新群, = <课程> & 讨论'
        status, data = self.request({'action':'upsert','group_id':'7654321','name':label})
        self.assertEqual(status, 200)
        self.assertTrue(self.settings.accepts_group('7654321'))
        self.assertEqual(self.settings.group_name('7654321'), label)
        self.request({'action':'upsert','group_id':'7654321','name':'已改名'})
        self.assertEqual(len(self.manager.snapshot()['groups']), 2)
        restarted = dataclasses.replace(self.settings, group_whitelist=(), group_aliases={})
        GroupManager(self.path, restarted)
        self.assertEqual(restarted.group_name('7654321'), '已改名')
        self.assertEqual(self.request()[0], 200)

    def test_live_intake_changes_without_recreating_receiver(self):
        records = []
        receiver = OneBotReceiver(self.settings, lambda record: records.append(record) or True)
        event = {'post_type':'message','message_type':'group','group_id':7654321,
                 'message_id':1,'user_id':99,'self_id':100,'message':'虚构测试通知'}
        self.assertEqual(receiver.handle_payload(event)[0], 204)
        self.request({'action':'upsert','group_id':'7654321','name':'新群'})
        code, body = receiver.handle_payload(event)
        self.assertEqual(code, 200)
        self.assertTrue(body['inserted'])
        self.assertFalse({'reply', 'actions', 'at_sender'} & set(body))
        self.assertEqual(records[0]['group_name'], '新群')
        task = self.store.upsert_task(task_key='historical', summary='原有待办', groups=['新群'])
        self.request({'action':'remove','group_id':'7654321'})
        self.assertEqual(receiver.handle_payload({**event,'message_id':2})[0], 204)
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(self.store.get_task(task))
        self.request({'action':'remove','group_id':'123456'})
        self.assertFalse(self.settings.accepts_group('123456'))
        restarted = dataclasses.replace(self.settings, group_whitelist=('123456',))
        GroupManager(self.path, restarted)
        self.assertEqual(restarted.group_whitelist, ())

    def test_auth_and_cross_site_block_writes(self):
        payload = {'action':'upsert','group_id':'7654321','name':'新群'}
        self.assertEqual(self.request(token=False)[0], 401)
        self.assertEqual(self.request(payload, token=False)[0], 401)
        self.assertEqual(self.request(payload, origin='https://example.invalid')[0], 403)
        self.assertFalse(self.path.exists())

    def test_bad_input_leaves_allowlist_unchanged(self):
        for payload in [[], {'action':'send'}, {'action':'upsert','group_id':'bad','name':'群'},
                        {'action':'upsert','group_id':'7654321','name':' '},
                        {'action':'upsert','group_id':'7654321','name':'群\nQQ_DIGEST_GROUPS=all'},
                        {'action':'upsert','group_id':'7654321','name':'长'*81}]:
            with self.subTest(payload=payload): self.assertEqual(self.request(payload)[0], 400)
        self.assertEqual(self.request(raw=b'{')[0], 400)
        self.assertEqual(self.request(raw=b'x'*4097)[0], 400)
        self.assertEqual(self.settings.group_whitelist, ('123456',))

    def test_failed_save_does_not_change_intake_or_last_saved_file(self):
        self.request({'action':'upsert','group_id':'7654321','name':'新群'})
        saved = self.path.read_bytes()
        with mock.patch('qq_live_digest.groups.os.replace', side_effect=PermissionError):
            status, data = self.request({'action':'remove','group_id':'7654321'})
        self.assertEqual(status, 500)
        self.assertTrue(self.settings.accepts_group('7654321'))
        self.assertEqual(saved, self.path.read_bytes())
        self.assertFalse(list(self.path.parent.glob('.groups-*.tmp')))

    def test_corrupt_file_does_not_reenable_original_groups(self):
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError): GroupManager(self.path, self.settings)


if __name__ == '__main__': unittest.main()
