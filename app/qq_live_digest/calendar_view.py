"""Private calendar projection and explicitly entered recurring courses."""
from __future__ import annotations
import datetime as dt,re,uuid
from .temporal import clock
from .timeutil import now_local,parse_iso

def courses(store):
    with store._connect() as db:return [dict(r) for r in db.execute('SELECT * FROM courses ORDER BY weekday,start_time,id')]

def save_course(store,payload):
    if payload.get('action')=='delete':
        try:course_id=int(payload.get('id'))
        except (ValueError,TypeError):raise ValueError('课程标识无效。')
        with store._connect() as db:db.execute('DELETE FROM courses WHERE id=?',(course_id,))
        return {'ok':True}
    title=str(payload.get('title') or '').strip();location=str(payload.get('location') or '').strip()
    if not 1<=len(title)<=100 or len(location)>100:raise ValueError('请填写课程名称（最多100字），地点最多100字。')
    try:
        day=int(payload.get('weekday'));first=dt.date.fromisoformat(str(payload.get('first_monday')))
        begin=int(payload.get('week_start'));end=int(payload.get('week_end'))
        start=str(payload.get('start_time'));finish=str(payload.get('end_time'))
        if not re.fullmatch(r'\d{2}:\d{2}',start) or not re.fullmatch(r'\d{2}:\d{2}',finish):raise ValueError()
        dt.time.fromisoformat(start);dt.time.fromisoformat(finish)
        if not 1<=day<=7 or first.weekday()!=0 or not 1<=begin<=end<=60 or finish<=start:raise ValueError()
    except (ValueError,TypeError):raise ValueError('请核对星期、第一周周一日期、周次范围及上下课时间。')
    parity=payload.get('parity','all')
    if parity not in ('all','odd','even'):raise ValueError('单双周选项无效。')
    values=(title,day,start,finish,first.isoformat(),begin,end,parity,location,str(payload.get('note') or '')[:500])
    with store._connect() as db:
        course_id=int(payload.get('id') or 0)
        if course_id:
            if not db.execute('SELECT id FROM courses WHERE id=?',(course_id,)).fetchone():raise ValueError('课程不存在。')
            db.execute('UPDATE courses SET title=?,weekday=?,start_time=?,end_time=?,first_monday=?,week_start=?,week_end=?,parity=?,location=?,note=? WHERE id=?',(*values,course_id))
        else:
            prior=db.execute('SELECT id FROM courses WHERE title=? AND weekday=? AND start_time=? AND end_time=? AND first_monday=? AND week_start=? AND week_end=? AND parity=? AND location=? AND note=?',values).fetchone()
            course_id=prior[0] if prior else db.execute('INSERT INTO courses(title,weekday,start_time,end_time,first_monday,week_start,week_end,parity,location,note) VALUES(?,?,?,?,?,?,?,?,?,?)',values).lastrowid
    return {'ok':True,'id':course_id}

def calendar_payload(store,start,end):
    try:
        start=dt.date.fromisoformat(str(start)[:10]);end=dt.date.fromisoformat(str(end)[:10])
        if not 0<(end-start).days<=100:raise ValueError()
    except (ValueError,TypeError):raise ValueError('日历范围应为1至100天。')
    events=[];undated=[]
    for task in store.list_tasks(limit=1000):
        if task['status'] not in ('open','candidate'):continue
        plan=task.get('presentation') or {};name=plan.get('what') or task['action'] or task['summary']
        phases=plan.get('milestones') or []
        if phases:
            for phase in phases:
                if phase.get('done'):continue
                left=dt.date.fromisoformat(phase.get('start_date') or phase['end_date']);right=dt.date.fromisoformat(phase['end_date'])
                if right<start or left>=end:continue
                events.append({'id':f"task-{task['id']}-{phase['id']}",'title':name+' · '+phase['label'],'start':left.isoformat(),
                    'end':(right+dt.timedelta(days=1)).isoformat(),'allDay':True,'color':'#6750a4','extendedProps':{'task_id':task['id'],'kind':'stage','location':plan.get('location',''),'description':phase.get('time_text','')+'；阶段截止 '+phase['end_date']}})
            continue
        moment=parse_iso(task.get('deadline'))
        if not moment:undated.append({'id':task['id'],'title':name});continue
        if not start<=moment.date()<end:continue
        explicit=bool(clock(plan.get('quote') or task.get('evidence') or '')) or task.get('source')=='manual' and moment.strftime('%H:%M')!='23:59'
        is_event=plan.get('time_kind')=='event';all_day=not explicit
        event={'id':'task-'+str(task['id']),'title':('' if is_event else '截止 · ')+name,'start':moment.isoformat() if not all_day else moment.date().isoformat(),
               'allDay':all_day,'color':'#be5f00' if not is_event else '#2866d9','extendedProps':{'task_id':task['id'],'kind':'activity' if is_event else 'deadline','location':plan.get('location',''),'description':'待办截止点，不代表课程时长' if not is_event else '活动时间；结束时间未注明'}}
        if not all_day:event['end']=(moment+dt.timedelta(minutes=1)).isoformat()
        # Deadlines are points, never fabricated one-hour classes.
        events.append(event)
    rows=courses(store)
    for course in rows:
        monday=dt.date.fromisoformat(course['first_monday'])
        for week in range(course['week_start'],course['week_end']+1):
            if course['parity']=='odd' and week%2==0 or course['parity']=='even' and week%2:continue
            day=monday+dt.timedelta(weeks=week-1,days=course['weekday']-1)
            if start<=day<end:
                events.append({'id':f"course-{course['id']}-{day}",'title':course['title'],'start':day.isoformat()+'T'+course['start_time'],
                    'end':day.isoformat()+'T'+course['end_time'],'color':'#238477','extendedProps':{'course_id':course['id'],'kind':'course','location':course['location'],'description':f"第{week}周 · "+course['note']}})
    return {'events':events,'undated':undated,'courses':rows,'server_time':now_local().isoformat()+'+08:00','timezone':'Asia/Shanghai'}
