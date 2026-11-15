"""Evidence-grounded task cards and multi-day milestones; never infer a venue."""
from __future__ import annotations
import datetime as dt
import hashlib
import re


def dates_in_text(text: str, sent: dt.datetime) -> set[str]:
    from .temporal import dated_clauses
    return {v['date'] for v in dated_clauses(text,sent,include_range_start=True)}


def grounded_time(value: str, quote: str) -> str:
    from .temporal import clock
    if not re.fullmatch(r'\d{2}:\d{2}',value):return ''
    for clause in re.split(r'[。；;\n]',quote):
        if clock(clause)==value:return value
    return ''


def phase_proof(proof: str, start: str, end: str, sent: dt.datetime) -> str:
    """Keep dates and time hints within the same phase, even for broad AI quotes."""
    clauses=re.split(r'[。；;\n]|(?=并在|然后在|随后在)',proof)
    range_pattern=r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日?\s*(?:-|—|－|~|～|至|到)\s*(?:(\d{1,2})月)?(\d{1,2})日?'
    iso_range=r'(20\d{2}-\d{2}-\d{2})\s*(?:至|到|—|~|～|\s-\s)\s*(20\d{2}-\d{2}-\d{2})'
    for clause in clauses:
        if end not in dates_in_text(clause,sent):continue
        pairs=[]
        for match in re.finditer(range_pattern,clause):
            year,month,first,last_month,last=match.groups()
            left=dates_in_text(f'{year+"年" if year else ""}{month}月{first}日',sent)
            right=dates_in_text(f'{year+"年" if year else ""}{last_month or month}月{last}日',sent)
            if left and right:pairs.append((next(iter(left)),next(iter(right))))
        pairs.extend(re.findall(iso_range,clause))
        if start and start!=end:
            if pairs and (start,end) not in pairs:continue
            if not pairs and dates_in_text(clause,sent)!={start,end}:continue
        elif pairs and not any(last==end for _,last in pairs):continue
        # A clause with two different ranges is too broad to attach a clock safely.
        if len(set(pairs))>1:continue
        return clause.strip()
    return ''


def normalize_plan(raw, source: str, sent: dt.datetime) -> dict:
    if not isinstance(raw,dict) or not source:return {}
    plan={}
    quote=str(raw.get('quote') or '').strip()
    if quote and quote in source:
        what=str(raw.get('what') or '').strip()
        if what and len(what)<=100:plan['what']=what
        location=str(raw.get('location') or '').strip()
        if location and len(location)<=100 and location in quote:plan['location']=location
        for name in ('start_date','end_date'):
            value=str(raw.get(name) or '')
            if value in dates_in_text(quote,sent):plan[name]=value
        when=str(raw.get('time_text') or '').strip()
        if when and len(when)<=80 and when in quote:plan['time_text']=when
        plan['quote']=quote[:6000]
    phases=[]
    raw_phases=raw.get('milestones')
    for phase in (raw_phases[:12] if isinstance(raw_phases,list) else []):
        if not isinstance(phase,dict):continue
        proof=str(phase.get('quote') or '').strip();label=str(phase.get('label') or '').strip()
        if not label or len(label)>80 or not proof or proof not in source:continue
        dates=dates_in_text(proof,sent)
        start=str(phase.get('start_date') or '');end=str(phase.get('end_date') or '')
        if end not in dates or (start and (start not in dates or start>end)):continue
        proof=phase_proof(proof,start,end,sent)
        if not proof:continue
        clock=grounded_time(str(phase.get('deadline_time') or ''),proof)
        hint=str(phase.get('time_text') or '').strip()
        if not hint or len(hint)>80 or hint not in proof:hint=''
        phase_id=hashlib.sha256((start+'|'+end+'|'+clock+'|'+label).encode()).hexdigest()[:16]
        if any(p['id']==phase_id for p in phases):continue
        phases.append({'id':phase_id,'label':label,'start_date':start,'end_date':end,'deadline_time':clock,'time_text':hint,'quote':proof[:6000],'done':False})
    if phases:
        phases.sort(key=lambda p:(p['end_date'],p['deadline_time'] or '23:59'))
        plan['start_date']=min(p['start_date'] or p['end_date'] for p in phases)
        plan['end_date']=max(p['end_date'] for p in phases)
        if len(phases)==1 and len(raw_phases)==1:
            single=phases[0]
            if single['deadline_time']:plan['deadline_time']=single['deadline_time']
            if single['time_text']:plan['time_text']=single['time_text']
            from .temporal import dated_clauses
            plan['time_kind']='event' if any(v['kind']=='event' for v in dated_clauses(single['quote'],sent)) else 'deadline'
        else:plan['milestones']=phases
    return plan


def next_deadline(plan: dict) -> str:
    pending=[p for p in plan.get('milestones',[]) if not p.get('done')]
    if not pending:return ''
    phase=min(pending,key=lambda p:(p['end_date'],p.get('deadline_time') or '23:59'))
    return phase['end_date']+'T'+(phase.get('deadline_time') or '23:59')+':00'


def merge_plan(previous: dict, incoming: dict) -> dict:
    merged={**previous,**incoming}
    old={p.get('id'):p for p in previous.get('milestones',[])}
    if 'milestones' in incoming:
        merged['milestones']=[{**p,'done':bool(old.get(p['id'],{}).get('done'))} for p in incoming['milestones']]
    return merged


def phase_when(phase: dict) -> str:
    start=phase.get('start_date','');end=phase.get('end_date','')
    value=(start[5:]+'—'+end[5:]) if start and start!=end else end[5:]
    if phase.get('time_text'):value+=' · '+phase['time_text']
    return value+' · 截止 '+end[5:]+(' '+phase['deadline_time'] if phase.get('deadline_time') else '（时刻未注明）')


def card_brief(task: dict) -> dict:
    plan=task.get('presentation') or {}
    what=str(plan.get('what') or task.get('action') or task.get('summary') or '').strip()
    what=re.sub(r'^(?:通知[：:]\s*|请注意[：:]?\s*)','',what)
    phases=plan.get('milestones',[])
    if phases:
        start=plan.get('start_date','');end=plan.get('end_date','')
        when=(start[5:]+'—'+end[5:]+' · 分 '+str(len(phases))+' 个阶段')
    else:
        deadline=str(task.get('deadline') or '')
        start=plan.get('start_date','');end=plan.get('end_date','')
        when=start[5:]+'—'+end[5:] if start and end and start!=end else str(task.get('deadline_text') or '')
        if end and plan.get('quote') and (not start or start==end):
            clock=grounded_time(deadline[11:16],plan['quote'])
            when=end[5:]+(' '+clock if clock else '（时刻未注明）')
        if not when:when=deadline[:16].replace('T',' ')+' 截止' if deadline else '原文未明确时间'
        if plan.get('time_text'):when+=' · '+plan['time_text']
    return {'what':what,'when':when,'where':str(plan.get('location') or '地点待核对 · 见原文')}


PLAN_PROMPT='''在 items 数组的每一个事项对象内部增加 presentation 字段，禁止把 presentation 放在根对象。结构为 {"items":[{"id":1,"presentation":{...},...}]}，其余字段按前述要求保留。presentation 对象格式：{"what":"简短任务名称与动作，不写客套话","location":"原文逐字地点或线上平台；未写留空","quote":"依据的逐字原文片段","start_date":"YYYY-MM-DD或空","end_date":"YYYY-MM-DD或空","time_text":"原文时间描述，如晚自习，或空","milestones":[{"label":"阶段动作，如练习与模拟或线上考试","start_date":"YYYY-MM-DD或空","end_date":"该阶段截止YYYY-MM-DD","deadline_time":"原文明确的HH:mm或空","time_text":"该阶段原文时间描述或空","quote":"同时包含该阶段动作和日期范围的逐字原文"}]}。引文须逐字复制原文，不能改写标点或增添年份。同一活动多天、多阶段分别保留，不能合并成最早一天；每阶段日期只据该阶段引文，不把其他阶段的地点或时间套用过来。未写时刻不得推算时刻。最多12个阶段。主标题尽量不超过35字；对象、条件、要求留在详细说明。'''
