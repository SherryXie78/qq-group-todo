var qqLatestSession=null,qqPollTimer=null,qqPreviousState=null;
function qqFeedback(text,error){document.querySelectorAll('[data-qq-field="action"]').forEach(function(node){node.hidden=!text;node.textContent=text||'';node.classList.toggle('error',!!error);});}
function bindQQActions(root){root.querySelectorAll('[data-qq-action]').forEach(function(button){button.onclick=function(){qqSessionAction(button.getAttribute('data-qq-action'));};});}
function renderQQSession(session){
 qqLatestSession=session||{};var offline=isOffline(),online=!offline&&!!qqLatestSession.online,action=qqLatestSession.action||{};
 document.querySelectorAll('[data-qq-card]').forEach(function(card){
  card.setAttribute('data-state',offline?'offline':online?'online':qqLatestSession.state||'unknown');
  card.querySelector('[data-qq-field="title"]').textContent=offline?'离线缓存 · 状态待检测':qqLatestSession.title||(online?'QQ 采集在线':'QQ 连接待检查');
  card.querySelector('[data-qq-field="account"]').textContent=qqLatestSession.account_label?'采集账号：'+qqLatestSession.account_label:'';
  card.querySelector('[data-qq-field="message"]').textContent=offline?'当前为离线缓存，不能判断 QQ 的最新状态。恢复私人连接后再检测。':qqLatestSession.message||'请在电脑登录普通 QQ 的原账号。';
  var checked=card.querySelector('[data-qq-field="checked"]');checked.textContent=qqLatestSession.last_checked?'最后采集检测：'+new Date(qqLatestSession.last_checked).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚无最新采集检测结果';
  card.querySelectorAll('[data-qq-action]').forEach(function(button){button.disabled=offline||button.getAttribute('data-qq-action')!=='recheck'&&(!qqLatestSession.supported||action.active);});
 });
 var details=document.getElementById('qq-login-panel'),state=offline?'offline':qqLatestSession.state;
 if(details&&!online&&state!==qqPreviousState)details.open=true;
 qqPreviousState=state;
 if(action.message&&action.visible!==false)qqFeedback(online&&action.status==='done'?'已确认原账号采集在线，普通 QQ 保持登录。':action.message,!online&&(action.status==='error'||action.status==='interrupted'));else qqFeedback('',false);
}
function refreshQQSession(){
 return api('/api/qq-session').then(function(session){renderQQSession(session);return session;});
}
function pollQQAction(attempt){
 if(qqPollTimer)clearTimeout(qqPollTimer);
 qqPollTimer=setTimeout(function(){refreshQQSession().then(function(session){
  if((session.action||{}).active&&attempt<45)pollQQAction(attempt+1);else loadTasks();
 }).catch(function(error){qqFeedback(error.message,true);});},1500);
}
function qqSessionAction(action){
 if(isOffline()){qqFeedback('请先恢复手机与电脑的私人连接，再操作 QQ 登录。',true);return;}
 if(action==='recheck'){qqFeedback('正在重新检测…');refreshQQSession().then(function(session){qqFeedback(session.online?'已确认：QQ 采集在线。':session.message);loadTasks();}).catch(function(error){qqFeedback(error.message,true);});return;}
 document.querySelectorAll('[data-qq-action]').forEach(function(button){button.disabled=true;});
 qqFeedback(action==='open_login'?'正在电脑打开普通 QQ…':'正在恢复只读采集，普通 QQ 保持运行…');
 api('/api/qq-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action})}).then(function(session){renderQQSession(session);pollQQAction(0);}).catch(function(error){if(qqLatestSession)renderQQSession(qqLatestSession);qqFeedback(error.message,true);});
}
function qqLoginCard(){
 var card=el('div','group-panel qq-login-settings');card.setAttribute('data-qq-card','');
 card.appendChild(el('h2','section-title','QQ 登录与采集'));
 [['title','strong'],['account','p'],['message','p']].forEach(function(pair){var node=el(pair[1],pair[0]==='account'?'group-hint':'');node.setAttribute('data-qq-field',pair[0]);card.appendChild(node);});
 var actions=el('div','qq-login-actions');[['open_login','在电脑打开 QQ 登录'],['recheck','重新检测'],['reconnect','恢复采集']].forEach(function(pair){var button=el('button','btn'+(pair[0]==='open_login'?' primary':''),pair[1]);button.type='button';button.setAttribute('data-qq-action',pair[0]);actions.appendChild(button);});card.appendChild(actions);
 card.appendChild(el('p','group-hint','在电脑普通 QQ 窗口亲自扫码或确认登录。仅接入已授权的原账号；恢复采集不会关闭 QQ。'));
 var checked=el('small');checked.setAttribute('data-qq-field','checked');card.appendChild(checked);
 var feedback=el('p','qq-login-feedback');feedback.setAttribute('data-qq-field','action');feedback.setAttribute('role','status');feedback.hidden=true;card.appendChild(feedback);
 bindQQActions(card);setTimeout(function(){if(qqLatestSession)renderQQSession(qqLatestSession);refreshQQSession().catch(function(error){qqFeedback(error.message,true);});},0);return card;
}
bindQQActions(document.getElementById('qq-login-panel'));
if(location.hash==='#qq-login')document.getElementById('qq-login-panel').open=true;
