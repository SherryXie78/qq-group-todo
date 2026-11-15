function openCorrection(task) {
  var dialog=el('dialog','manual-dialog'),form=el('form'),busy=false,requestId=crypto.randomUUID();
  dialog.appendChild(form);form.appendChild(el('h2','','纠正这项待办'));
  form.appendChild(el('p','manual-hint',task.brief?task.brief.what:task.summary));
  var label=el('label','','哪里有误，应如何修改？'),reason=el('textarea');reason.required=true;reason.minLength=2;reason.maxLength=2000;
  reason.setAttribute('aria-label','纠错原因');reason.placeholder='例如：面试是24号下午三点，不是明年凌晨三点；请保留这件事。也可以说明应拆成哪些事项。';label.appendChild(reason);form.appendChild(label);
  var keepLabel=el('label','keep-option'),keep=el('input');keep.type='checkbox';keep.checked=true;keepLabel.appendChild(keep);keepLabel.appendChild(el('span','','修正后保留这项待办'));form.appendChild(keepLabel);
  form.appendChild(el('p','manual-hint','默认保留：修改内容和时间，继续显示在待办与日历。取消勾选则修正后移到已忽略。原文与本次说明将交给 DeepSeek 审查。'));
  var error=el('p','manual-error');error.setAttribute('role','alert');form.appendChild(error);
  var evidence=el('details');evidence.appendChild(el('summary','','核对原文'));evidence.appendChild(el('p','source-copy',task.source_text||task.evidence||'无原文'));form.appendChild(evidence);
  if(task.correction_note)form.appendChild(el('p','manual-hint','上次更正：'+task.correction_note));
  var actions=el('div','actions'),cancel=el('button','btn','取消'),save=el('button','btn primary','提交并后台修正');cancel.type='button';save.type='submit';cancel.onclick=function(){dialog.close();};actions.appendChild(cancel);actions.appendChild(save);form.appendChild(actions);
  if(task.correction_note){var undo=el('button','btn','撤回最近一次 AI 修正');undo.type='button';undo.onclick=function(){if(isOffline()){error.textContent='请先恢复连接。';return;}api('/api/tasks/'+task.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'undo_correction'})}).then(function(){dialog.close();loadTasks();}).catch(function(e){error.textContent=e.message;});};form.appendChild(undo);}
  form.onsubmit=function(event){event.preventDefault();if(busy)return;if(isOffline()){error.textContent='当前离线，说明会保留在窗口中，恢复连接后再提交。';return;}busy=true;save.disabled=true;
    api('/api/tasks/'+task.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'review',reason:reason.value,keep:keep.checked,client_id:requestId})})
      .then(function(){dialog.close();var note=document.getElementById('manual-feedback');note.hidden=false;note.textContent='已提交后台审查，原待办暂时保留；修正状态会自动更新。';loadTasks();})
      .catch(function(e){error.textContent=e.message;}).finally(function(){busy=false;save.disabled=false;});};
  dialog.addEventListener('cancel',function(event){if(busy)event.preventDefault();});dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();reason.focus();
}

function openCorrectionsLog(){
 var dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','','纠错记录与撤回'));var list=el('div');dialog.appendChild(list);list.textContent='正在读取记录…';
 api('/api/task-reviews').then(function(data){list.textContent='';if(!data.items.length)list.appendChild(el('p','manual-hint','还没有文字纠错记录。'));
  var seen={};data.items.forEach(function(item){var latest=!seen[item.task_id];seen[item.task_id]=true;var card=el('div','notice');card.appendChild(el('h3','',item.summary));card.appendChild(el('p','','你的说明：'+item.reason));card.appendChild(el('p','',item.status==='undone'?'这次修正已撤回':item.result.explanation||item.error||'正在审查'));
   if(latest && item.status==='done'){var undo=el('button','btn','撤回这次修正');undo.onclick=function(){if(isOffline())return;undo.disabled=true;api('/api/tasks/'+item.task_id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'undo_correction',review_id:item.id})}).then(function(){undo.textContent='已撤回';loadTasks();}).catch(function(e){undo.disabled=false;undo.textContent=e.message;});};card.appendChild(undo);}list.appendChild(card);});
 }).catch(function(e){list.textContent=e.message;});var close=el('button','btn','关闭');close.onclick=function(){dialog.close();};dialog.appendChild(close);dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}

var personalCalendar=null,calendarLoading=false;
function showCalendarEvent(event){
 var info=event.extendedProps,dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','',event.title));
 dialog.appendChild(el('p','manual-hint',info.location?'地点：'+info.location:'地点待核对'));
 dialog.appendChild(el('p','',info.description||''));
 if(info.task_id){var open=el('button','btn primary','查看待办');open.onclick=function(){dialog.close();document.querySelector('[data-tab="tasks"]').click();setTimeout(function(){var target=document.getElementById('task-'+info.task_id);if(target)target.scrollIntoView({behavior:'smooth',block:'center'});},50);};dialog.appendChild(open);}
 if(info.course_id){var edit=el('button','btn','编辑课程');edit.onclick=function(){api('/api/courses').then(function(data){var course=data.courses.find(function(c){return c.id===info.course_id;});if(course){dialog.close();openCourse(course);}});};dialog.appendChild(edit);}
 var close=el('button','btn','关闭');close.onclick=function(){dialog.close();};dialog.appendChild(close);dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}
function calendarFetch(info,success,failure){
 api('/api/calendar/events?start='+encodeURIComponent(info.startStr.slice(0,10))+'&end='+encodeURIComponent(info.endStr.slice(0,10))).then(function(data){
  document.getElementById('calendar-error').textContent=isOffline()?'当前为已缓存的日历，恢复连接后可更新。':'';
  var undated=document.getElementById('calendar-undated');undated.textContent='';
  if(data.undated.length){undated.appendChild(el('h3','','截止时间未注明 · '+data.undated.length+' 项'));data.undated.slice(0,30).forEach(function(t){var item=el('button','btn calendar-undated-item',t.title);item.onclick=function(){document.querySelector('[data-tab="tasks"]').click();setTimeout(function(){var target=document.getElementById('task-'+t.id);if(target)target.scrollIntoView({block:'center'});},50);};undated.appendChild(item);});}
  var list=document.getElementById('course-list');list.textContent='';if(!data.courses.length)list.appendChild(el('p','group-hint','尚未录入课程表。可手动添加课程，之后也可以提供课程表批量录入。'));
  data.courses.forEach(function(c){var row=el('div','course-row');row.appendChild(el('span','',c.title+' · 周'+'一二三四五六日'[c.weekday-1]+' '+c.start_time+'—'+c.end_time));var edit=el('button','btn','编辑');edit.onclick=function(){openCourse(c);};row.appendChild(edit);list.appendChild(row);});
  success(data.events);
 }).catch(function(e){document.getElementById('calendar-error').textContent=e.message;failure(e);});
}
function loadCalendar(){
 if(!window.FullCalendar){if(!calendarLoading){calendarLoading=true;window.addEventListener('load',function(){calendarLoading=false;loadCalendar();},{once:true});}document.getElementById('calendar-error').textContent='正在载入日历组件…';return;}
 if(personalCalendar){personalCalendar.updateSize();personalCalendar.refetchEvents();return;}
 var today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 personalCalendar=new FullCalendar.Calendar(document.getElementById('calendar-view'),{
  initialView:innerWidth<600?'listDay':'dayGridMonth',initialDate:today,timeZone:'Asia/Shanghai',firstDay:1,locale:'zh-cn',height:'auto',
  headerToolbar:{left:'prev,next today',center:'title',right:'dayGridMonth,timeGridWeek,listDay'},buttonText:{today:'今天',month:'月',week:'周',list:'日程'},allDayText:'全天',noEventsText:'这段时间暂无明确日程',
  eventTimeFormat:{hour:'2-digit',minute:'2-digit',hour12:false},events:calendarFetch,eventClick:function(info){showCalendarEvent(info.event);},displayEventEnd:false,editable:false,dateClick:function(info){personalCalendar.changeView('listDay',info.dateStr.slice(0,10));}});personalCalendar.render();
}
function openCourse(course){
 course=course||{};var dialog=el('dialog','manual-dialog'),form=el('form');dialog.appendChild(form);form.appendChild(el('h2','',course.id?'编辑课程':'添加课程'));
 form.appendChild(el('p','manual-hint','按北京时间录入。第一周周一是整个学期第1周的周一；未提供的课程不会自动生成。'));
 var fields=[['title','课程名称','text',course.title||''],['weekday','星期（1为周一，7为周日）','number',course.weekday||1],['first_monday','第一周周一日期','date',course.first_monday||''],['week_start','起始周','number',course.week_start||1],['week_end','结束周','number',course.week_end||16],['start_time','上课时间','time',course.start_time||''],['end_time','下课时间','time',course.end_time||''],['location','教室／地点','text',course.location||'']];
 fields.forEach(function(f){var label=el('label','',f[1]),input=el('input');input.name=f[0];input.type=f[2];input.value=f[3];input.required=f[0]!=='location';input.maxLength=100;if(f[2]==='number'){input.min=1;input.max=f[0]==='weekday'?7:60;}label.appendChild(input);form.appendChild(label);});
 var parityLabel=el('label','','单双周'),select=el('select');select.name='parity';[['all','每周'],['odd','单周'],['even','双周']].forEach(function(pair){var option=el('option','',pair[1]);option.value=pair[0];option.selected=pair[0]===(course.parity||'all');select.appendChild(option);});parityLabel.appendChild(select);form.appendChild(parityLabel);
 var error=el('p','manual-error');error.setAttribute('role','alert');form.appendChild(error);var actions=el('div','actions'),cancel=el('button','btn','取消'),save=el('button','btn primary','保存课程');cancel.type='button';save.type='submit';cancel.onclick=function(){dialog.close();};actions.appendChild(cancel);actions.appendChild(save);form.appendChild(actions);
 form.onsubmit=function(e){e.preventDefault();if(isOffline()){error.textContent='当前离线，请恢复连接后保存。';return;}save.disabled=true;var body=Object.fromEntries(new FormData(form).entries());if(course.id)body.id=course.id;api('/api/courses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(){dialog.close();if(personalCalendar)personalCalendar.refetchEvents();}).catch(function(e){error.textContent=e.message;}).finally(function(){save.disabled=false;});};
 if(course.id){var remove=el('button','btn','删除这门课程');remove.type='button';remove.onclick=function(){if(isOffline()){error.textContent='当前离线，不能删除。';return;}if(!confirm('删除这门课程及其后续重复日程？'))return;api('/api/courses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',id:course.id})}).then(function(){dialog.close();personalCalendar.refetchEvents();}).catch(function(e){error.textContent=e.message;});};form.appendChild(remove);}
 dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}
document.getElementById('course-add').onclick=function(){openCourse();};
