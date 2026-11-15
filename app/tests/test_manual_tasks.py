import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.webapp import TaskWebServer


class ManualTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'manual.sqlite3')
        self.settings = Settings(web_host='127.0.0.1', web_port=0, web_token='test-secret')
        self.web = TaskWebServer(self.settings, self.store)
        self.assertTrue(self.web.start())
        self.addCleanup(self.web.stop)
        self.base = 'http://127.0.0.1:' + str(self.web.server.server_port)
        self.payload = {'client_id':str(uuid.uuid4()), 'summary':'补交活动表',
                        'deadline_date':'2026-10-14', 'deadline_time':'18:30',
                        'group':'示例班长群', 'note':'老师通知：周三交表。\n个人备注：需先核对名单。'}

    def post(self, payload=None, *, token=True, origin=None, raw=None):
        headers = {'Content-Type':'application/json'}
        if token: headers['X-Token'] = 'test-secret'
        if origin: headers['Origin'] = origin
        body = raw if raw is not None else json.dumps(self.payload if payload is None else payload).encode()
        request = urllib.request.Request(self.base+'/api/tasks/manual', data=body, headers=headers)
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try: response = http.open(request, timeout=10)
        except urllib.error.HTTPError as error: response = error
        with response: return response.status, json.load(response)

    def test_saved_task_is_manual_open_and_retains_evidence(self):
        code, result = self.post()
        self.assertEqual(code, 200)
        task = self.store.get_task(result['id'])
        self.assertEqual(task['source'], 'manual')
        self.assertEqual(task['status'], 'open')
        self.assertEqual(task['category'], 'action')
        self.assertEqual(task['deadline'], '2026-10-14T18:30:00')
        self.assertEqual(task['groups'], ['示例班长群'])
        self.assertEqual(task['evidence'], self.payload['note'])
        # Reopening the database preserves it; manual entry produces no chat or AI activity.
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_task(result['id'])['summary'], self.payload['summary'])
        with reopened._connect() as db:
            for table in ('messages', 'deliveries', 'llm_calls'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)

    def test_date_only_defaults_to_end_of_day(self):
        code, result = self.post({**self.payload,'deadline_time':''})
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(result['id'])['deadline'],'2026-10-14T23:59:00')

    def test_no_date_and_old_dates_are_allowed_for_manual_entries(self):
        for date in ('', '2020-01-01'):
            code, result = self.post({**self.payload,'client_id':str(uuid.uuid4()),'deadline_date':date,'deadline_time':''})
            self.assertEqual(code, 200)
            task = self.store.get_task(result['id'])
            self.assertEqual(task['deadline'], date+'T23:59:00' if date else '')
            self.assertEqual(task['status'],'open')

    def test_retries_are_atomic_and_do_not_duplicate_reopen_or_overwrite(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.post(), range(4)))
        self.assertTrue(all(code == 200 for code, _ in replies))
        ids = {result['id'] for _, result in replies}
        self.assertEqual(len(ids), 1)
        task_id = ids.pop()
        self.store.apply_task_action(task_id,'done')
        self.post({**self.payload,'summary':'retry must not overwrite'})
        task = self.store.get_task(task_id)
        self.assertEqual(task['status'],'done')
        self.assertEqual(task['summary'],self.payload['summary'])
        self.assertEqual(len(self.store.list_tasks()),1)

    def test_invalid_fields_never_create_tasks(self):
        invalid = [[], {'summary':''}, {'summary':' '*4}, {'summary':'x'*201},
                   {'client_id':'onebot:123456:1'}, {'deadline_time':'12:30','deadline_date':''},
                   {'deadline_date':'2026-02-30'}, {'deadline_date':'2026-1-3'},
                   {'deadline_time':'25:00'}, {'note':'x'*8001}, {'group':'a\nQQ_DIGEST_GROUPS=all'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                payload = {**self.payload,**changes} if isinstance(changes,dict) else changes
                self.assertEqual(self.post(payload)[0],400)
        self.assertEqual(self.post(raw=b'{')[0],400)
        self.assertEqual(self.post(raw=b'x'*(48*1024+1))[0],400)
        self.assertEqual(self.store.list_tasks(),[])

    def test_unauthorized_and_cross_site_requests_are_blocked(self):
        self.assertEqual(self.post(token=False)[0],401)
        self.assertEqual(self.post(origin='https://untrusted.invalid')[0],403)
        self.assertEqual(self.store.list_tasks(),[])

    def test_storage_failure_reports_failure_and_can_retry(self):
        with mock.patch.object(self.store,'upsert_task',side_effect=OSError('private details')):
            code, result = self.post()
        self.assertEqual(code,500)
        self.assertNotIn('private details',str(result))
        self.assertEqual(self.store.list_tasks(),[])
        self.assertEqual(self.post()[0],200)

    def test_ai_task_is_untouched_and_unknown_payload_cannot_change_source(self):
        task_id = self.store.upsert_task(task_key='onebot:123456:1', summary='existing AI task',source='llm')
        code, result = self.post({**self.payload,'source':'llm','status':'done','task_key':'onebot:123456:1'})
        self.assertEqual(code,200)
        self.assertNotEqual(task_id,result['id'])
        self.assertEqual(self.store.get_task(task_id)['summary'],'existing AI task')
        self.assertEqual(self.store.get_task(result['id'])['source'],'manual')


if __name__ == '__main__': unittest.main()
