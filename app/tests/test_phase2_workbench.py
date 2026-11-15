import datetime as dt,json,tempfile,threading,time,unittest,uuid
from pathlib import Path
from qq_digest import Message,extract_deadline,refine_items,parse_timestamp
from qq_live_digest.providers import LLMProvider,LLMResult
from qq_live_digest.config import Settings
from qq_live_digest.store import Store
from qq_live_digest.temporal import dated_clauses
from qq_live_digest.temporal import clock
from qq_live_digest.timeutil import to_naive_local
from qq_live_digest.task_review import TaskReview
from qq_live_digest.task_intelligence import TaskMatcher
from qq_live_digest.calendar_view import calendar_payload,save_course
from qq_live_digest.daily_advice import DailyAdvice

class Fake(LLMProvider):
 def __init__(self,data):super().__init__(model='fake');self.data=data
 def complete(self,*args,**kwargs):return LLMResult(text=json.dumps(self.data),provider='fake',model='fake')

class TemporalCases(unittest.TestCase):
 def test_interview_day_only_pm_and_classroom_guard(self):
  sent=dt.datetime(2026,10,16,19,7)
  self.assertEqual(extract_deadline(Message(sent,'','面试时间：24号下午三点\n地点：3-4，102教室')),dt.datetime(2026,10,24,15))
  self.assertIsNone(extract_deadline(Message(sent,'','面试教室1-2-113，先去1-2-111等候')))
 def test_holiday_chapters_are_not_deadlines(self):
  text='明天就是假期，好好休息\n1、完成人工智能实验9.7、11.1、11.2\n2、高数要在30号DDL之前完成'
  self.assertEqual(extract_deadline(Message(dt.datetime(2026,10,23),'',text)),dt.datetime(2026,10,30,23,59))
 def test_range_end_and_beijing_utc_conversion(self):
  self.assertEqual(extract_deadline(Message(dt.datetime(2026,10,20),'','10月20日-23日登录系统练习')),dt.datetime(2026,10,23,23,59))
  self.assertEqual(to_naive_local('2026-10-23T18:00:00Z'),dt.datetime(2026,10,24,2))
  self.assertEqual(parse_timestamp('2026-10-23T18:00:00Z'),dt.datetime(2026,10,24,2))
 def test_negated_old_time_is_not_the_correction(self):
  self.assertEqual(clock('不是凌晨三点，而是下午三点'),'15:00')
  values=dated_clauses('不是2027年12月24日，应该是2026年10月24日',dt.datetime(2026,10,24))
  self.assertEqual({v['date'] for v in values},{'2026-10-24'})
 def test_split_independent_work_preserves_evidence_and_distinct_keys(self):
  text='明天就是假期好好休息\n1、完成人工智能实验9.7、11.1、11.2\n2、30号DDL前完成高数第一章作业'
  base={'message':Message(dt.datetime(2026,10,23),'班长',text),'category':'action','score':5,'msg_id':'onebot:123:1','deadline_dt':dt.datetime(2026,10,24)}
  entries=[{'id':1,'summary':'完成人工智能实验','action':'完成实验','quote':text.splitlines()[1],'deadline':''},
           {'id':1,'summary':'提交高数第一章作业','action':'提交高数作业','quote':text.splitlines()[2],'deadline':'2027-10-30 03:00'}]
  values=refine_items([base],Fake({'items':entries}))
  self.assertEqual(len(values),2);self.assertIsNone(values[0]['deadline_dt']);self.assertEqual(values[1]['deadline_dt'],dt.datetime(2026,10,30,23,59))
  self.assertNotEqual(values[0]['task_key'],values[1]['task_key']);self.assertTrue(all(v['evidence'] in text for v in values))
  reverse=refine_items([base],Fake({'items':list(reversed(entries))}));self.assertEqual({v['task_key'] for v in values},{v['task_key'] for v in reverse})

class DataCases(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'digest.sqlite3');self.settings=Settings(llm_enabled=False)
  self.source='面试时间：24号下午三点\n地点：3-4，102教室'
  self.store.insert_message(msg_id='notice',group_id='123',content=self.source,ts='2026-10-16T19:07:22')
  self.task=self.store.upsert_task(task_key='notice',summary='参加书法面试',action='参加面试',category='action',deadline='2027-10-24T03:00:00',evidence=self.source)
 def tearDown(self):self.temp.cleanup()
 def wait(self,review):
  for _ in range(200):
   job=review.latest().get(self.task)
   if job and job['status'] not in ('queued','running'):return job
   time.sleep(.01)
  self.fail('Correction did not complete')
 def corrected(self,**extra):
  return dict(summary='参加书法面试',action='参加面试',category='action',deadline='2026-10-24T15:00:00',quote=self.source,presentation={'what':'参加书法面试','quote':self.source,'location':'3-4，102教室'},**extra)
 def test_free_text_retains_task_protects_reanalysis_and_can_undo(self):
  review=TaskReview(self.settings,self.store,analyzer=lambda _:self.corrected())
  payload={'reason':'时间是24号下午三点，不是明年凌晨，请保留','keep':True,'client_id':str(uuid.uuid4())}
  job=review.submit(self.task,payload);self.assertEqual(review.submit(self.task,payload)['id'],job['id'])
  self.assertEqual(self.wait(review)['status'],'done')
  task=self.store.get_task(self.task);self.assertEqual(task['deadline'],'2026-10-24T15:00:00');self.assertEqual(task['status'],'open')
  self.store.upsert_task(task_key='notice',summary='old incorrect',deadline='2027-10-24T03:00:00')
  self.assertEqual(self.store.get_task(self.task)['summary'],'参加书法面试')
  self.store.undo_task_edit(self.task);self.assertEqual(self.store.get_task(self.task)['deadline'],'2027-10-24T03:00:00')
  self.assertEqual(review.latest()[self.task]['status'],'undone')
 def test_ai_cannot_overwrite_completion_while_review_running(self):
  entered=threading.Event();release=threading.Event()
  def analyze(_):entered.set();release.wait(2);return self.corrected()
  review=TaskReview(self.settings,self.store,analyzer=analyze);review.submit(self.task,{'reason':'24号下午三点','client_id':str(uuid.uuid4())})
  self.assertTrue(entered.wait(1));self.store.apply_task_action(self.task,'done');release.set()
  self.assertEqual(self.wait(review)['status'],'failed');self.assertEqual(self.store.get_task(self.task)['status'],'done')
 def test_unverifiable_ai_date_keeps_original(self):
  result=self.corrected();result['deadline']='2030-11-29T18:00:00'
  review=TaskReview(self.settings,self.store,analyzer=lambda _:result);review.submit(self.task,{'reason':'请修正时间，保留任务','client_id':str(uuid.uuid4())})
  self.assertEqual(self.wait(review)['status'],'failed');self.assertEqual(self.store.get_task(self.task)['deadline'],'2027-10-24T03:00:00')
 def test_explicit_discard_is_reversible(self):
  review=TaskReview(self.settings,self.store,analyzer=lambda _:self.corrected());review.submit(self.task,{'reason':'24号下午三点，但这件事我不参加','keep':False,'client_id':str(uuid.uuid4())})
  self.assertEqual(self.wait(review)['status'],'done');self.assertEqual(self.store.get_task(self.task)['status'],'dismissed')
  self.store.undo_task_edit(self.task);self.assertEqual(self.store.get_task(self.task)['status'],'open')
 def test_courses_recur_on_odd_weeks_and_retry_does_not_duplicate(self):
  data={'title':'高等数学','weekday':5,'start_time':'08:00','end_time':'09:40','first_monday':'2026-10-19','week_start':1,'week_end':3,'parity':'odd','location':'教学楼'}
  first=save_course(self.store,data);self.assertEqual(save_course(self.store,data)['id'],first['id'])
  events=calendar_payload(self.store,'2026-10-19','2026-11-09')['events'];days=[e['start'][:10] for e in events if e['extendedProps']['kind']=='course']
  self.assertEqual(days,['2026-10-23','2026-11-06'])
  with self.assertRaises(ValueError):save_course(self.store,{**data,'first_monday':'2026-10-20'})
 def test_match_rejects_forged_ids_and_mismatched_dates(self):
  item={'message':Message(dt.datetime(2026,10,24),'','参加书法面试'),'summary':'参加书法面试','action':'参加面试','deadline_dt':dt.datetime(2026,10,24,15),'category':'action','msg_id':'new'}
  matcher=TaskMatcher(self.settings,self.store,analyzer=lambda _: {'matches':[{'index':0,'task_id':self.task,'relation':'repeat','confidence':.99}]})
  self.assertEqual(matcher.match([item]),{})
  self.store.edit_task(self.task,{'deadline':'2026-10-24T15:00:00'},reason='source repair',manual=False)
  self.assertEqual(matcher.match([item])[0]['task']['id'],self.task)
  matcher.analyzer=lambda _:{'matches':[{'index':0,'task_id':99999,'relation':'repeat','confidence':1}]};self.assertEqual(matcher.match([item]),{})
 def test_daily_advice_refresh_is_cached_not_rebilled(self):
  calls=[];advice=DailyAdvice(self.settings,self.store,analyzer=lambda p:(calls.append(p) or {'advice':'先做最近到期的一件事，慢慢来。'}))
  now=dt.datetime(2026,10,24,10);tasks=self.store.list_tasks();advice.get(tasks,now)
  for _ in range(200):
   if not advice.running:break
   time.sleep(.01)
  for _ in range(20):self.assertEqual(advice.get(tasks,now)['source'],'deepseek')
  self.assertEqual(len(calls),1)
 def children(self):
  return [{'task_key':'notice:part:a','summary':'完成数学作业','action':'完成数学作业','evidence':'数学作业','origin_msg_id':'notice'},
          {'task_key':'notice:part:b','summary':'完成英语作业','action':'完成英语作业','evidence':'英语作业','origin_msg_id':'notice'}]
 def test_atomic_split_and_undo_preserve_source_and_other_tasks(self):
  extra=self.store.upsert_task(task_key='unrelated',summary='另一件事',status='done')
  parent=self.store.get_task(self.task);ids=self.store.split_task(self.task,self.children(),reason='应分成两项',expected_revision=self.store.task_revision(parent))
  self.assertEqual(len(ids),2);self.assertEqual(self.store.get_task(self.task)['status'],'dismissed')
  self.assertEqual(self.store.get_task(ids[0])['source_text'],self.source)
  self.store.undo_task_edit(self.task);self.assertEqual(self.store.get_task(self.task)['status'],'open')
  self.assertEqual(self.store.get_task(self.task)['source'],parent['source'])
  self.assertTrue(all(self.store.get_task(i)['status']=='dismissed' for i in ids));self.assertEqual(self.store.get_task(extra)['status'],'done')
 def test_invalid_split_rolls_back_every_child(self):
  children=self.children();children[1]['existing_task_id']=99999
  with self.assertRaises(ValueError):self.store.split_task(self.task,children,reason='拆开')
  self.assertIsNone(self.store.task_by_key('notice:part:a'));self.assertEqual(self.store.get_task(self.task)['status'],'open')
 def test_split_undo_cannot_overwrite_child_completion(self):
  ids=self.store.split_task(self.task,self.children(),reason='拆开');self.store.apply_task_action(ids[0],'done')
  with self.assertRaises(ValueError):self.store.undo_task_edit(self.task)
  self.assertEqual(self.store.get_task(ids[0])['status'],'done')
 def test_split_reuses_existing_done_item_without_reopening(self):
  existing=self.store.upsert_task(task_key='oldmath',summary='完成数学作业',status='done');children=self.children();children[0]['existing_task_id']=existing
  ids=self.store.split_task(self.task,children,reason='拆开并合并重复项')
  self.assertIn(existing,ids);self.assertEqual(self.store.get_task(existing)['status'],'done')
  self.store.undo_task_edit(self.task);self.assertEqual(self.store.get_task(existing)['status'],'done')
 def test_event_source_heading_retrieves_generic_old_title_and_supports_match(self):
  source='转发学工群通知\n《关于大学生示例活动14的通知》\n10月20日-23日练习，10月24日-27日线上考试'
  self.store.insert_message(msg_id='competition',group_id='123',content=source,ts='2026-10-20T10:00:00')
  competition=self.store.upsert_task(task_key='competition',summary='大一新生练习模拟与线上考试',action='练习模拟并考试',evidence='10月20日-23日练习，10月24日-27日线上考试')
  broad='明天假期好好休息\n但仍有作业\n1、完成高数\n2、准备大学生示例活动14'
  self.store.insert_message(msg_id='siblings',group_id='123',content=broad)
  for n in range(6):self.store.upsert_task(task_key='sibling-'+str(n),summary='完成高数第'+str(n)+'题',evidence='完成高数',origin_msg_id='siblings')
  item={'message':Message(dt.datetime.now(),'','认真准备大学生示例活动14'),'summary':'准备大学生示例活动14','action':'准备大学生示例活动14','task_key':'newcompetition','deadline_dt':None}
  def analyze(payload):
   self.assertIn(competition,payload['new'][0]['candidate_ids'])
   return {'matches':[{'index':0,'task_id':competition,'relation':'repeat','confidence':.9}]}
  result=TaskMatcher(self.settings,self.store,analyzer=analyze).match([item]);self.assertEqual(result[0]['task']['id'],competition)
 def test_lower_confidence_generic_coursework_is_not_auto_merged(self):
  existing=self.store.upsert_task(task_key='lab',summary='完成化学实验报告',evidence='完成化学实验报告')
  item={'message':Message(dt.datetime.now(),'','完成化学实验报告'),'summary':'完成化学实验报告','action':'完成化学实验报告','task_key':'another-lab','deadline_dt':None}
  matcher=TaskMatcher(self.settings,self.store,analyzer=lambda _:{'matches':[{'index':0,'task_id':existing,'relation':'repeat','confidence':.9}]})
  self.assertEqual(matcher.match([item]),{})
 def test_merge_alias_resolves_canonical_and_preserves_completion(self):
  canonical=self.store.upsert_task(task_key='canonical',summary='已存在的竞赛',status='done')
  self.store.merge_duplicate_task(self.task,canonical,reason='已核实重复',expected_revision=self.store.task_revision(self.store.get_task(self.task)))
  self.assertEqual(self.store.task_by_key('notice')['id'],canonical);self.assertEqual(self.store.get_task(canonical)['status'],'done')
  self.assertEqual(self.store.get_task(self.task)['status'],'dismissed')

class HttpCases(unittest.TestCase):
 def setUp(self):
  from qq_live_digest.webapp import TaskWebServer
  self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'db.sqlite3');self.task=self.store.upsert_task(task_key='test',summary='交表',evidence='10月30日前交表',category='action')
  self.web=TaskWebServer(Settings(web_host='127.0.0.1',web_port=0,web_token='fixture-secret',llm_enabled=False),self.store)
  self.web.task_review.analyzer=lambda _:{'summary':'提交报名表','action':'交表','deadline':'2026-10-30T23:59:00','quote':'10月30日前交表'}
  self.web.start();self.base='http://127.0.0.1:'+str(self.web.server.server_port)
 def tearDown(self):self.web.stop();self.temp.cleanup()
 def request(self,path,body=None,*,auth=True,origin=None):
  import urllib.request,urllib.error
  headers={'Content-Type':'application/json'}
  if auth:headers['X-Token']='fixture-secret'
  if origin:headers['Origin']=origin
  req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  try:response=opener.open(req,timeout=5)
  except urllib.error.HTTPError as e:response=e
  with response:return response.status,json.load(response)
 def test_private_calendar_and_courses_require_auth_and_csrf(self):
  for path in ('/api/calendar/events?start=2026-10-01&end=2026-11-01','/api/courses','/api/task-reviews'):
   self.assertEqual(self.request(path,auth=False)[0],401)
  self.assertEqual(self.request('/api/courses',{},origin='https://untrusted.example')[0],403)
  self.assertEqual(self.request('/api/calendar/events?start=2026-01-01&end=2030-01-01')[0],400)
 def test_text_review_submit_retries_are_idempotent(self):
  payload={'action':'review','reason':'请改为10月30日前提交报名表，保留','client_id':str(uuid.uuid4()),'keep':True}
  first=self.request('/api/tasks/'+str(self.task),payload);second=self.request('/api/tasks/'+str(self.task),payload)
  self.assertEqual(first[0],202);self.assertEqual(first[1]['id'],second[1]['id'])
  for _ in range(100):
   jobs=self.request('/api/task-reviews')[1]['items']
   if jobs[0]['status'] not in ('queued','running'):break
   time.sleep(.01)
  self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['status'],'done')
  self.assertEqual(self.store.get_task(self.task)['status'],'open')
 def test_malformed_task_body_and_invalid_keep_do_not_write(self):
  self.assertEqual(self.request('/api/tasks/'+str(self.task),[])[0],400)
  self.assertEqual(self.request('/api/tasks/'+str(self.task),{'action':'review','reason':'改时间','keep':'false','client_id':str(uuid.uuid4())})[0],400)
  self.assertEqual(self.request('/api/task-reviews')[1]['items'],[])

if __name__=='__main__':unittest.main()
