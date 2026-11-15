// The server owns every point. Animations only play after an accepted operation.
var petState=null,petBusy=false,petLoading=null,petToastTimer=null,petAnimationTimer=null;
var petPending=null;
try{petPending=JSON.parse(localStorage.getItem('qq_pet_pending')||'null');if(petPending&&(!petPending.request_id||!['feed','bonk','woodfish'].includes(petPending.action)))petPending=null;}catch(_){}
var petSoundEnabled=localStorage.getItem('qq_pet_sound')==='1',petAudio=null,petConfettiLoading=null;

function updatePetAvailability(){
 var unavailable=isOffline()||!petState;
 document.querySelectorAll('[data-pet-action]').forEach(function(button){var action=button.dataset.petAction,info=petState&&(petState.actions||[]).find(function(a){return a.id===action;});button.disabled=unavailable||petBusy||!!(petPending&&petPending.action!==action)||(!petPending&&(!info||petState.balance<info.cost));});
 var customize=document.getElementById('pet-customize');if(customize)customize.disabled=unavailable||petBusy;
 var status=document.getElementById('pet-network');if(!status)return;
 status.classList.toggle('error',isOffline());
 status.textContent=isOffline()?'离线查看中 · 积分和互动待连接电脑后同步':petBusy?'正在兑换这份小快乐…':petPending?'上次互动结果待确认，请点同一按钮重试，不会重复扣分。':petState?'积分已同步 · 完成任务后自动更新':'正在同步积分…';
}
function renderPet(data){
 if(!data)return;petState=data;
 document.querySelectorAll('[data-pet-balance]').forEach(function(node){node.textContent=data.balance;});
 var chip=document.getElementById('pet-points-chip');if(!chip){chip=el('button','pet-points-chip');chip.id='pet-points-chip';chip.type='button';chip.onclick=function(){document.querySelector('[data-tab="pet"]').click();};document.querySelector('.stats').appendChild(chip);}chip.textContent='✦ '+data.balance+' 积分';chip.setAttribute('aria-label','查看宠物，当前 '+data.balance+' 积分');
 document.getElementById('pet-name').textContent=data.name;
 document.getElementById('pet-character').setAttribute('aria-label',data.name+'的卡通形象');
 var image=document.getElementById('pet-avatar');
 if(data.avatar_url&&image.dataset.hash!==data.avatar_hash){image.dataset.hash=data.avatar_hash;image.src=data.avatar_url;}
 image.hidden=!data.avatar_url;document.getElementById('pet-default-face').toggleAttribute('hidden',!!data.avatar_url);
 document.getElementById('pet-rules-text').textContent=data.rules;
 document.getElementById('pet-earn-hint').textContent=data.balance<0?'有 '+(-data.balance)+' 积分待补回。完成任务，就能继续收获小快乐。':data.balance<2?'完成一项待办，收获 10 积分，再来玩一会儿。':'今天已经很努力了，挑一个喜欢的互动吧。';
 var totals=data.totals||{},count=Object.values(totals).reduce(function(sum,n){return sum+n;},0);document.getElementById('pet-interaction-count').textContent=count+' 次互动';
 var list=document.getElementById('pet-ledger');list.textContent='';
 (data.history||[]).forEach(function(item){var row=el('li'),copy=el('div','pet-log-copy');copy.appendChild(el('strong','',(item.kind==='reward'?'完成 · ':item.kind==='reversal'?'撤销完成 · ':'互动 · ')+item.label));var date=new Date(item.at);copy.appendChild(el('small','',isNaN(date.getTime())?'':date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})));row.appendChild(copy);row.appendChild(el('span','pet-log-amount'+(item.amount>0?' positive':''),(item.amount>0?'+':'')+item.amount));list.appendChild(row);});
 if(!(data.history||[]).length)list.appendChild(el('li','pet-log-empty','这里会记住你的努力。第一份奖励，等你来拿。'));
 updatePetAvailability();
}
function loadPet(){
 document.getElementById('pet-toast').hidden=true;
 if(petLoading)return petLoading;
 petLoading=api('/api/pet').then(renderPet).catch(function(error){document.getElementById('pet-network').textContent=error.message;}).finally(function(){petLoading=null;});return petLoading;
}
function ensurePetConfetti(){
 if(window.confetti)return Promise.resolve(window.confetti);
 if(petConfettiLoading)return petConfettiLoading;
 petConfettiLoading=new Promise(function(resolve,reject){var script=document.createElement('script');script.src='/vendor/canvas-confetti-1.9.4.js';script.onload=function(){resolve(window.confetti);};script.onerror=function(){script.remove();petConfettiLoading=null;reject(Error('animation unavailable'));};document.head.appendChild(script);});return petConfettiLoading;
}
function showPetReward(receipt){
 if(!receipt.delta)return;
 var positive=receipt.delta>0,node=document.getElementById('pet-toast');
 document.getElementById('pet-toast-title').textContent=positive?'完成啦！积分 +'+receipt.delta:'已撤销完成 · 积分 '+receipt.delta;
 document.getElementById('pet-toast-detail').textContent='当前 '+receipt.balance+' 积分'+(positive?'，去陪小搭子玩一会儿？':'，奖励已同步调整');
 node.hidden=false;clearTimeout(petToastTimer);petToastTimer=setTimeout(function(){node.hidden=true;},6000);
 if(positive&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches)ensurePetConfetti().then(function(confetti){confetti({particleCount:45,spread:65,origin:{y:.78},colors:['#ada0d5','#e2bd76','#a8c9b6','#e7b5cc'],disableForReducedMotion:true,zIndex:89});}).catch(function(){});
}
document.getElementById('pet-toast-go').onclick=function(){document.getElementById('pet-toast').hidden=true;document.querySelector('[data-tab="pet"]').click();};
function updatePetSound(){var button=document.getElementById('pet-sound');button.textContent='音效：'+(petSoundEnabled?'开':'关');button.setAttribute('aria-pressed',String(petSoundEnabled));button.setAttribute('aria-label',petSoundEnabled?'关闭互动音效':'开启互动音效');}
function preparePetSound(){if(!petSoundEnabled)return;try{if(!petAudio)petAudio=new(window.AudioContext||window.webkitAudioContext)();if(petAudio.state==='suspended')petAudio.resume().catch(function(){});}catch(_){} }
function playPetSound(action){
 if(!petSoundEnabled||!petAudio||petAudio.state!=='running')return;
 try{var oscillator=petAudio.createOscillator(),gain=petAudio.createGain(),now=petAudio.currentTime;oscillator.type='sine';oscillator.frequency.setValueAtTime(action==='woodfish'?620:action==='bonk'?200:780,now);oscillator.frequency.exponentialRampToValueAtTime(action==='woodfish'?300:action==='bonk'?85:1100,now+.12);gain.gain.setValueAtTime(.0001,now);gain.gain.exponentialRampToValueAtTime(.16,now+.008);gain.gain.exponentialRampToValueAtTime(.0001,now+.3);oscillator.connect(gain);gain.connect(petAudio.destination);oscillator.start(now);oscillator.stop(now+.32);oscillator.onended=function(){oscillator.disconnect();gain.disconnect();};}catch(_){}
}
document.getElementById('pet-sound').onclick=function(){petSoundEnabled=!petSoundEnabled;localStorage.setItem('qq_pet_sound',petSoundEnabled?'1':'0');preparePetSound();updatePetSound();};updatePetSound();
function rememberPetPending(value){petPending=value;if(value)localStorage.setItem('qq_pet_pending',JSON.stringify(value));else localStorage.removeItem('qq_pet_pending');}
function petInteract(action){
 if(petBusy||isOffline()||!petState)return;
 if(petPending&&petPending.action!==action)return;
 if(!petPending)rememberPetPending({action:action,request_id:crypto.randomUUID?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16))).map(function(n){return n.toString(16).padStart(2,'0');}).join('')});
 petBusy=true;preparePetSound();updatePetAvailability();
 document.getElementById('pet-toast').hidden=true;
 api('/api/pet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(petPending)}).then(function(result){
  rememberPetPending(null);renderPet(result.pet);document.getElementById('pet-speech').textContent=result.message;
  var character=document.getElementById('pet-character');clearTimeout(petAnimationTimer);character.removeAttribute('data-action');void character.offsetWidth;character.dataset.action=action;playPetSound(action);petAnimationTimer=setTimeout(function(){character.removeAttribute('data-action');},1900);
 }).catch(function(error){if(/积分不足|请选择|编号无效|发生变化/.test(error.message))rememberPetPending(null);document.getElementById('pet-speech').textContent=error.name==='AbortError'?'连接慢了一点，请点原来的按钮确认结果，不会重复扣分。':error.message;
 }).finally(function(){petBusy=false;updatePetAvailability();});
}
document.querySelectorAll('[data-pet-action]').forEach(function(button){button.onclick=function(){petInteract(button.dataset.petAction);};});

function openPetEditor(){
 if(isOffline()||!petState)return;
 var dialog=el('dialog','manual-dialog pet-editor'),form=el('form');dialog.appendChild(form);form.appendChild(el('h2','','定制你的小搭子'));
 form.appendChild(el('p','manual-hint','给卡通小人换上喜欢的头像，老师、舍友、导员，或原创角色，都由你决定。'));
 var label=el('label','','角色昵称'),name=el('input');name.value=petState.name;name.required=true;name.maxLength=24;name.name='pet-name';label.appendChild(name);form.appendChild(label);
 var nativeVersion=(navigator.userAgent.match(/GroupTodoNative\/([\d.]+)/)||[])[1];
 if(nativeVersion&&nativeVersion<'2026.11.01.1'){var update=el('p','pet-native-update','手机选照片需更新独立版。也可先在电脑上传，手机会同步。');var link=el('a','',' 下载更新（覆盖安装）');link.href='/android';update.appendChild(link);form.appendChild(update);}
 var fileLabel=el('label','','选择头像（JPG / PNG / WebP，最大 12 MB）'),file=el('input','pet-photo-input');file.type='file';file.accept='image/jpeg,image/png,image/webp';fileLabel.appendChild(file);form.appendChild(fileLabel);
 var canvas=el('canvas','pet-photo-preview');canvas.width=512;canvas.height=512;canvas.hidden=true;form.appendChild(canvas);
 var sliders=el('div');sliders.hidden=true;form.appendChild(sliders);var controls={};[['zoom','缩放',1,3,.01,1],['x','左右位置',0,100,1,50],['y','上下位置',0,100,1,50]].forEach(function(f){var label=el('label','',f[1]),input=el('input');input.type='range';input.min=f[2];input.max=f[3];input.step=f[4];input.value=f[5];input.setAttribute('aria-label',f[1]);label.appendChild(input);sliders.appendChild(label);controls[f[0]]=input;});
 var error=el('p','manual-error');error.setAttribute('role','alert');form.appendChild(error);
 var image=null,objectURL=null,saving=false,photoLoading=false,photoInvalid=false,selectSequence=0;
 function draw(){if(!image)return;var size=Math.min(image.naturalWidth,image.naturalHeight)/Number(controls.zoom.value),sx=(image.naturalWidth-size)*Number(controls.x.value)/100,sy=(image.naturalHeight-size)*Number(controls.y.value)/100,ctx=canvas.getContext('2d');ctx.clearRect(0,0,512,512);ctx.drawImage(image,sx,sy,size,size,0,0,512,512);}
 Object.values(controls).forEach(function(input){input.oninput=draw;});
 file.onchange=function(){var selected=file.files[0],sequence=++selectSequence;image=null;canvas.hidden=true;sliders.hidden=true;error.textContent='';photoLoading=false;photoInvalid=false;if(!selected)return;if(!['image/jpeg','image/png','image/webp'].includes(selected.type)||selected.size>12*1024*1024){photoInvalid=true;error.textContent='请选择 12 MB 以内的 JPG、PNG 或 WebP 图片。';file.value='';return;}if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(selected);var candidate=new Image();photoLoading=true;candidate.onload=function(){if(sequence!==selectSequence)return;photoLoading=false;if(candidate.naturalWidth*candidate.naturalHeight>20000000){photoInvalid=true;error.textContent='图片超过 2000 万像素，请先缩小后选择。';return;}image=candidate;controls.zoom.value=1;controls.x.value=50;controls.y.value=50;canvas.hidden=false;sliders.hidden=false;draw();};candidate.onerror=function(){if(sequence!==selectSequence)return;photoLoading=false;photoInvalid=true;error.textContent='这张图片无法读取，请换一张。';};candidate.src=objectURL;};
 var actions=el('div','pet-editor-actions'),cancel=el('button','btn','取消'),save=el('button','btn primary','保存形象');cancel.type='button';save.type='submit';cancel.onclick=function(){if(!saving)dialog.close();};actions.appendChild(cancel);actions.appendChild(save);form.appendChild(actions);
 if(petState.avatar_url){var remove=el('button','btn','恢复默认头像');remove.type='button';remove.onclick=function(){if(isOffline()){error.textContent='离线时不能修改头像。';return;}remove.disabled=true;api('/api/pet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove_avatar'})}).then(function(data){renderPet(data);dialog.close();}).catch(function(e){error.textContent=e.message;remove.disabled=false;});};actions.appendChild(remove);}
 form.onsubmit=function(event){event.preventDefault();if(saving)return;if(isOffline()){error.textContent='请恢复私人连接后保存。';return;}if(photoInvalid){error.textContent='所选头像无法使用，请换一张图片再保存。';return;}if(photoLoading){error.textContent='头像还在读取，请稍等一下。';return;}saving=true;save.disabled=true;cancel.disabled=true;error.textContent='正在保存…';
  var blob=image?new Promise(function(resolve,reject){canvas.toBlob(function(result){if(result)resolve(result);else reject(Error('头像生成失败，请换一张图片。'));},'image/png');}):Promise.resolve(null);
  blob.then(function(photo){return api('/api/pet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'rename',name:name.value})}).then(function(data){renderPet(data);return photo?api('/api/pet/avatar',{method:'POST',headers:{'Content-Type':'image/png'},body:photo}):data;});}).then(function(data){renderPet(data);dialog.close();}).catch(function(e){error.textContent=e.message;}).finally(function(){saving=false;save.disabled=false;cancel.disabled=false;});
 };
 dialog.addEventListener('cancel',function(event){if(saving)event.preventDefault();});dialog.addEventListener('close',function(){selectSequence++;if(objectURL)URL.revokeObjectURL(objectURL);dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}
document.getElementById('pet-customize').onclick=openPetEditor;
