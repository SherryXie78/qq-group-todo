import datetime as dt
import tempfile,unittest
from pathlib import Path
from qq_live_digest.task_plan import normalize_plan,dates_in_text,card_brief
from qq_live_digest.store import Store

SOURCE='请大一新生在10月20日-23日登录系统进行练习与模拟。并在10月24日-27日期间利用晚自习时间进行线上考试，成绩≥80分。'
def raw_plan():return {'what':'示例活动14','location':'线上','quote':SOURCE,'milestones':[
 {'label':'练习与模拟','start_date':'2026-10-20','end_date':'2026-10-23','quote':SOURCE.split('。')[0]},
 {'label':'线上考试','start_date':'2026-10-24','end_date':'2026-10-27','time_text':'晚自习时间','quote':SOURCE.split('。')[1]}]}

class PlanTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'data.sqlite3');self.plan=normalize_plan(raw_plan(),SOURCE,dt.datetime(2026,10,20))
 def tearDown(self):self.temp.cleanup()
 def create(self):return self.store.upsert_task(task_key='competition',summary='比赛通知',action='参加竞赛',status='open',presentation=self.plan)
 def test_month_inheritance_and_grounded_stages(self):
  self.assertIn('2026-10-23',dates_in_text('10月20日-23日',dt.datetime(2026,10,20)))
  self.assertEqual(self.plan['start_date'],'2026-10-20');self.assertEqual(self.plan['end_date'],'2026-10-27')
  self.assertEqual(len(self.plan['milestones']),2);self.assertEqual(self.plan['milestones'][0]['deadline_time'],'')
  self.assertEqual(self.plan['milestones'][1]['time_text'],'晚自习时间')
 def test_fabricated_location_and_phase_date_rejected(self):
  raw=raw_plan();raw['location']='九号楼514';raw['milestones'][0]['end_date']='2026-10-29'
  plan=normalize_plan(raw,SOURCE,dt.datetime(2026,10,20))
  self.assertNotIn('location',plan);self.assertEqual(len(plan['milestones']),1)
 def test_first_stage_complete_keeps_second_and_survives_updates(self):
  task=self.create();first,second=self.plan['milestones']
  self.assertEqual(self.store.get_task(task)['deadline'],'2026-10-23T23:59:00')
  self.store.apply_task_action(task,'stage_done',detail={'milestone_id':first['id']})
  self.create();value=self.store.get_task(task)
  self.assertEqual(value['deadline'],'2026-10-27T23:59:00');self.assertTrue(value['presentation']['milestones'][0]['done']);self.assertEqual(value['status'],'open')
  self.assertEqual(Store(self.store.path).get_task(task)['presentation'],value['presentation'])
  self.store.apply_task_action(task,'stage_done',detail={'milestone_id':second['id']});self.assertEqual(self.store.get_task(task)['status'],'done')
  self.create();self.assertEqual(self.store.get_task(task)['status'],'done')
 def test_parent_completion_and_explicit_stage_reopen(self):
  task=self.create();self.store.apply_task_action(task,'done')
  second=self.plan['milestones'][1]
  self.store.apply_task_action(task,'stage_reopen',detail={'milestone_id':second['id']})
  value=self.store.get_task(task)
  self.assertTrue(value['presentation']['milestones'][0]['done']);self.assertFalse(value['presentation']['milestones'][1]['done'])
  self.assertEqual(value['status'],'open');self.assertEqual(value['deadline'],'2026-10-27T23:59:00')
  with self.assertRaises(ValueError):self.store.apply_task_action(task,'stage_done',detail={'milestone_id':'forged'})
 def test_three_fact_card_and_unknown_venue(self):
  task=self.store.get_task(self.create());brief=card_brief(task)
  self.assertEqual(brief['what'],'示例活动14');self.assertIn('10-20—10-27',brief['when']);self.assertEqual(brief['where'],'线上')
  self.assertEqual(card_brief({'summary':'交表'})['where'],'地点待核对 · 见原文')
 def test_broad_quote_cannot_mix_different_stage_dates_or_times(self):
  raw=raw_plan();raw['milestones'][0].update(quote=SOURCE,end_date='2026-10-27')
  plan=normalize_plan(raw,SOURCE,dt.datetime(2026,10,20))
  self.assertEqual(len(plan['milestones']),1)
  raw=raw_plan();raw['milestones'][0].update(quote=SOURCE,time_text='晚自习时间')
  plan=normalize_plan(raw,SOURCE,dt.datetime(2026,10,20))
  self.assertEqual(plan['milestones'][0]['time_text'],'')
  self.assertNotIn('线上考试',plan['milestones'][0]['quote'])
 def test_single_activity_is_not_an_artificial_one_stage_plan(self):
  source='面试时间：24号下午三点'
  plan=normalize_plan({'what':'参加面试','quote':source,'milestones':[{'label':'参加面试','start_date':'2026-10-24','end_date':'2026-10-24','deadline_time':'15:00','quote':source}]},source,dt.datetime(2026,10,16))
  self.assertNotIn('milestones',plan);self.assertEqual(plan['time_kind'],'event');self.assertEqual(plan['deadline_time'],'15:00')
