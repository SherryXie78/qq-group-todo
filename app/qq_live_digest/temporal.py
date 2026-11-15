"""Beijing notice dates, anchored to the message, with numeric false-positive guards."""
from __future__ import annotations
import datetime as dt
import re

BEIJING=dt.timezone(dt.timedelta(hours=8))

def number(text):
    if text=='半':return 30
    if text.isdigit():return int(text)
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if '十' in text:
        left,_,right=text.partition('十');return (digits.get(left,1) if left else 1)*10+digits.get(right,0)
    return digits.get(text,-1)

def clock(text):
    pattern=r'(?<!\d)(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|晚)?\s*(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[:：点时]\s*(\d{1,2}|[零〇一二两三四五六七八九十]{1,3}|半)?(?:分)?'
    candidates=[]
    for match in re.finditer(pattern,text):
        period,hour,minute=match.groups();lead=text[max(0,match.start()-18):match.start()]
        if re.search(r'(?:不是|并非|不要|误写为|误判为|错误地设为)\s*$',lead):continue
        h=number(hour);m=number(minute) if minute else 0
        if period in ('下午','傍晚','晚上','夜里','晚') and h<12:h+=12
        elif period=='中午' and h<11:h+=12
        if 0<=h<24 and 0<=m<60:
            priority=2 if re.search(r'应(?:该)?(?:改)?(?:为|成|是)|正确.*(?:为|是)|改为|改成|更正为|修正为|而是',lead) else 1
            candidates.append((priority,-match.start(),f'{h:02}:{m:02}'))
    return max(candidates)[2] if candidates else ''

def dated_clauses(text,reference,*,include_range_start=False):
    """Return date/clock/proof; never read a classroom or exercise number as a date."""
    reference=reference or dt.datetime.now(BEIJING).replace(tzinfo=None)
    if reference.tzinfo:reference=reference.astimezone(BEIJING).replace(tzinfo=None)
    results=[]
    for clause in re.split(r'[\n。；;！？!?]',text):
        if not clause.strip():continue
        if re.search(r'地点|教室|页码|实验\s*\d|[pP]\s*\d',clause) and not re.search(r'时间|截止|DDL|日期|\d月|\d[日号]',clause,re.I):continue
        if re.search(r'放假|休息|明天发',clause) and not re.search(r'截止|报名|提交|考试|面试|交作业|完成.*前|前.*完成',clause):continue
        occupied=[];found=[]
        def add(start,end,year,month,day):
            occupied.append((start,end))
            try:
                if re.search(r'(?:不是|并非|不要|误写为|误判为)\s*$',clause[max(0,start-12):start]):return
                date=dt.date(int(year),int(month),int(day))
                found.append((start,end,date))
            except (ValueError,TypeError):pass
        for m in re.finditer(r'(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?',clause):add(m.start(),m.end(),*m.groups())
        for m in re.finditer(r'(?<![\d.-])(\d{1,2})(月|/|-|\.)(\d{1,2})(?:日|(?![-\d]))',clause):
            if any(a<=m.start()<b for a,b in occupied):continue
            if m[2]=='.' and not re.search(r'时间|日期|截止|DDL|月|号|日',clause,re.I):continue
            year=reference.year
            if int(m[1])<reference.month-6:year+=1
            add(m.start(),m.end(),year,m[1],m[3])
        for m in re.finditer(r'(?<![\d月])(\d{1,2})[日号]',clause):
            if any(a<=m.start()<b for a,b in occupied):continue
            previous=[v for v in found if v[1]<=m.start()]
            month=previous[-1][2].month if previous else reference.month
            year=previous[-1][2].year if previous else reference.year
            if '下个月' in clause or '下月' in clause:month+=1
            if month==13:year+=1;month=1
            add(m.start(),m.end(),year,month,m[1])
        offsets={'大后天':3,'后天':2,'明天':1,'明日':1,'明晚':1,'今天':0,'今日':0,'今晚':0}
        for m in re.finditer('|'.join(offsets),clause):
            if not re.search(r'报名|提交|完成|截止|考试|面试|上课|活动|领取|开会|会议|交|准备',clause):continue
            date=reference.date()+dt.timedelta(days=offsets[m[0]]);found.append((m.start(),m.end(),date))
        for m in re.finditer(r'(下周|本周|这周|周|星期)([一二三四五六日天])',clause):
            weekday='一二三四五六日'.find(m[2].replace('天','日'));start=reference.date()-dt.timedelta(days=reference.weekday())
            if m[1]=='下周':start+=dt.timedelta(days=7)
            elif m[1] in ('周','星期') and weekday<reference.weekday():start+=dt.timedelta(days=7)
            found.append((m.start(),m.end(),start+dt.timedelta(days=weekday)))
        found.sort()
        for index,(start,end,date) in enumerate(found):
            tail=clause[end:found[index+1][0] if index+1<len(found) else len(clause)]
            if not include_range_start and index+1<len(found) and re.match(r'\s*[-—–～~至到]',tail):continue
            if clause[start:end] in ('今晚','明晚'):tail='晚上'+tail
            value=clock(tail)
            results.append({'date':date.isoformat(),'clock':value,'quote':clause.strip(),
                            'kind':'event' if re.search(r'面试|上课|开会|会议|考试时间|活动时间',clause) and not re.search(r'截止|DDL|之前',clause,re.I) else 'deadline'})
    return results

def extract_when(text,reference):
    values=dated_clauses(text,reference)
    candidates=[dt.datetime.fromisoformat(v['date']+'T'+(v['clock'] or '23:59')) for v in values]
    cutoff=(reference or dt.datetime.now(BEIJING).replace(tzinfo=None)).date()-dt.timedelta(days=1)
    return min((v for v in candidates if v.date()>=cutoff),default=None)

def countdown(deadline,now,kind='deadline'):
    if not deadline:return '截止时间未注明'
    difference=(deadline-now).total_seconds();days=(deadline.date()-now.date()).days
    if difference<0:
        return f'已逾期 {abs(days)} 天' if days<0 else '已超过时间'
    if days==0:
        return f'剩余 {int(difference//3600)} 小时' if difference>=3600 else f'剩余 {max(1,int(difference//60))} 分钟'
    return f'还剩 {days} 天'
