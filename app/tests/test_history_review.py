import dataclasses
import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.history_review import HistoryReview, grounded_item, explicit_dates

NOW=dt.datetime(2030,1,2,12)
TEXT='请于2030年1月5日前提交报名表，截止日期为2030年1月5日。'
def candidate(**changes):
    v={'summary':'提交报名表','kind':'task','evidence_ids':['42'],'quote':TEXT,'deadline_date':'2030-01-05','deadline_quote':TEXT}
    v.update(changes);return v

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)
        self.store=Store(p/'digest.sqlite3')
        self.settings=dataclasses.replace(Settings(),data_dir=p,group_whitelist=('123456',),group_aliases={'123456':'测试群'},llm_enabled=True,dashscope_api_key='local-test-placeholder')
        self.messages={'42':{'id':'42','text':TEXT,'time':'2030-01-01T10:00:00'}}
    def tearDown(self):self.tmp.cleanup()
    def test_proof_required(self):
        good=grounded_item(candidate(),self.messages,NOW.date());self.assertTrue(good['eligible'])
        self.assertFalse(grounded_item(candidate(deadline_date='2030-02-05'),self.messages,NOW.date())['eligible'])
        self.assertIsNone(grounded_item(candidate(quote='编造证据'),self.messages,NOW.date()))
        self.assertIsNone(grounded_item(candidate(evidence_ids=['999']),self.messages,NOW.date()))
    def test_expired_or_missing_deadline_never_added(self):
        self.assertFalse(grounded_item(candidate(),self.messages,dt.date(2031,1,1))['eligible'])
        self.assertFalse(grounded_item(candidate(deadline_quote=''),self.messages,NOW.date())['eligible'])
        self.assertFalse(grounded_item(candidate(kind='notice'),self.messages,NOW.date())['eligible'])
    def test_relative_dates_use_message_date(self):
        self.assertEqual(explicit_dates('明天前',dt.datetime(2029,12,31)),{'2030-01-01'})
        self.assertIn('2030-01-05',explicit_dates('1月5日前',dt.datetime(2029,12,31)))
    def test_future_phase_still_eligible_after_first_stage_is_past(self):
        text='1月2日-5日练习与模拟。并在1月6日-9日晚自习时间线上考试。'
        sources={'42':{'id':'42','text':text,'time':'2030-01-01T10:00:00'}}
        plan={'what':'安全竞赛','quote':text,'milestones':[
            {'label':'练习','start_date':'2030-01-02','end_date':'2030-01-05','quote':text.split('。')[0]},
            {'label':'考试','start_date':'2030-01-06','end_date':'2030-01-09','quote':text.split('。')[1]}]}
        value=grounded_item(candidate(quote=text,deadline_quote='',presentation=plan),sources,dt.date(2030,1,7))
        self.assertTrue(value['eligible']);self.assertEqual(len(value['presentation']['milestones']),2)
    def test_time_requires_verbatim_proof(self):
        text=TEXT+'截止2030年1月5日18:30。'
        sources={'42':{'id':'42','text':text,'time':'2030-01-01T10:00:00'}}
        result=grounded_item(candidate(deadline_quote=text,deadline_time='18:30'),sources,NOW.date())
        self.assertEqual(result['deadline_time'],'18:30')
        result=grounded_item(candidate(deadline_quote=text,deadline_time='20:30'),sources,NOW.date())
        self.assertEqual(result['deadline_time'],'')
    def test_history_json_mode_is_explicit_and_deepseek_only(self):
        from qq_live_digest.providers import OpenAICompatProvider
        p=OpenAICompatProvider(api_key='test',endpoint='https://api.deepseek.com/chat/completions',model='deepseek-flash')
        self.assertNotIn('response_format',p.build_payload([],temperature=.1,max_tokens=10))
        p.json_mode=True
        payload=p.build_payload([],temperature=.1,max_tokens=10)
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertEqual(payload['thinking'],{'type':'disabled'})
    def wait(self,m):
        for _ in range(250):
            if not m.active:return
            time.sleep(.01)
        self.fail('background operation did not finish')
    def test_read_review_retry_preserves_done_and_evidence(self):
        def page(group,cursor):return {'messages':[{'group_id':123456,'message_id':42,'user_id':1,'self_id':2,'time':int(dt.datetime(2030,1,1,10).timestamp()),'raw_message':TEXT}], 'cursor':0}
        m=HistoryReview(self.settings,self.store,fetcher=page,analyzer=lambda messages:[candidate(),candidate(deadline_date='2030-02-05')])
        with patch('qq_live_digest.history_review.now_local',return_value=NOW):
            job=m.start({'group_id':'123456','days':7});self.wait(m)
            self.assertEqual(m.get(job['id'])['count'],1)
            m.review(job['id']);self.wait(m)
            tasks=self.store.list_tasks();self.assertEqual(len(tasks),1)
            self.assertEqual(tasks[0]['deadline'],'2030-01-05T23:59:00')
            self.assertIn(TEXT,tasks[0]['evidence'])
            self.store.apply_task_action(tasks[0]['id'],'done')
            m.review(job['id']);self.wait(m)
            self.assertEqual(self.store.task_stats()['total'],1)
            self.assertEqual(self.store.get_task(tasks[0]['id'])['status'],'done')
        self.assertNotIn('messages',m.get(job['id']))
    def test_unselected_groups_and_invalid_ranges_blocked(self):
        m=HistoryReview(self.settings,self.store)
        with self.assertRaises(ValueError):m.start({'group_id':'999999','days':7})
        with self.assertRaises(ValueError):m.start({'group_id':'123456','days':800})
        with self.assertRaises(ValueError):m.get('../secret')
