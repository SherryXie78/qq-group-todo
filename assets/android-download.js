// Keep authenticated network reads inside this browser; only save a verified blob.
var downloadRelease=null,downloadParts=[],downloadOffset=0,downloadRunning=false,downloadBlobURL=null;
var downloadButton=document.getElementById('browser-download'),downloadStatus=document.getElementById('download-status'),downloadProgress=document.getElementById('download-progress'),downloadSave=document.getElementById('save-apk');
var downloadNative=/GroupTodoNative/.test(navigator.userAgent);
async function refreshDownloadRelease(){
 var response=await fetch('/api/mobile/android',{headers:{'X-Token':token},credentials:'same-origin',cache:'no-store'});
 if(!response.ok)throw Error(response.status===401?'页面授权已失效，请重新扫电脑安装页二维码。':'暂时连接不到安装包，请检查电脑与私人连接。');
 var info=await response.json();
 if(!info.ready||!Number.isSafeInteger(info.bytes)||info.bytes<=0||info.bytes>512*1024*1024||!/^[a-f0-9]{64}$/.test(info.sha256)||!info.download_url)throw Error('安装包暂未就绪，请稍后重新打开本页。');
 if(downloadRelease&&downloadRelease.sha256!==info.sha256){downloadParts=[];downloadOffset=0;throw Error('安装包版本已更新，请重新打开本页。');}
 downloadRelease=info;
 var system=document.getElementById('download');system.href=info.download_url;system.hidden=false;
 document.getElementById('package').textContent='版本 '+info.version+' · '+Math.ceil(info.bytes/1024/1024)+' MB · 校验 '+info.sha256.slice(0,12)+'…';
 return info;
}
async function readDownloadChunk(start,end){
 var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},45000);
 try{
  var response=await fetch('/api/mobile/android.apk',{headers:{'X-Token':token,'Range':'bytes='+start+'-'+end},credentials:'same-origin',cache:'no-store',signal:controller.signal});
  if(response.status===401)throw Error('页面授权已失效，请重新扫电脑安装页二维码。');
  if(response.status!==206||response.headers.get('Content-Range')!=='bytes '+start+'-'+end+'/'+downloadRelease.bytes)throw Error('分段下载响应异常，请稍后重试。');
  var bytes=await response.arrayBuffer();if(bytes.byteLength!==end-start+1)throw Error('本段下载中断，请点继续下载。');return bytes;
 }finally{clearTimeout(timer);}
}
downloadButton.onclick=async function(){
 if(downloadRunning||downloadNative)return;
 downloadRunning=true;downloadButton.disabled=true;downloadProgress.hidden=false;downloadSave.hidden=true;
 try{
  await refreshDownloadRelease();
  while(downloadOffset<downloadRelease.bytes){
   var end=Math.min(downloadOffset+1024*1024,downloadRelease.bytes)-1,bytes;
   for(var attempt=0;;attempt++){
    try{bytes=await readDownloadChunk(downloadOffset,end);break;}
    catch(error){if(attempt>=2||error.message.includes('授权'))throw error;downloadStatus.textContent='网络有些慢，正在重试当前分段…';await new Promise(function(resolve){setTimeout(resolve,1000*(attempt+1));});}
   }
   downloadParts.push(bytes);downloadOffset+=bytes.byteLength;
   var percent=Math.floor(downloadOffset/downloadRelease.bytes*100);downloadProgress.value=percent;
   downloadStatus.textContent='下载 '+percent+'% · '+(downloadOffset/1024/1024).toFixed(1)+' / '+(downloadRelease.bytes/1024/1024).toFixed(1)+' MB';
  }
  downloadStatus.textContent='下载完成，正在校验安装包…';
  if(!window.crypto||!crypto.subtle)throw Error('此浏览器无法校验安装包，请改点“备用：系统下载”。');
  var blob=new Blob(downloadParts,{type:'application/vnd.android.package-archive'});
  var digest=await crypto.subtle.digest('SHA-256',await blob.arrayBuffer());
  var actual=Array.from(new Uint8Array(digest)).map(function(n){return n.toString(16).padStart(2,'0');}).join('');
  if(actual!==downloadRelease.sha256){downloadParts=[];downloadOffset=0;throw Error('下载校验不一致，请重新下载，不要安装这个文件。');}
  if(downloadBlobURL)URL.revokeObjectURL(downloadBlobURL);downloadBlobURL=URL.createObjectURL(blob);
  downloadParts=[];downloadSave.href=downloadBlobURL;downloadSave.download=downloadRelease.filename;downloadSave.hidden=false;
  downloadStatus.textContent='校验通过。请点“保存安装包到手机”，再从下载列表打开并选择“更新”。';
  downloadButton.hidden=true;
 }catch(error){downloadStatus.textContent=(error.name==='AbortError'?'当前分段超时，请保持私人连接并重试。':error.message)+' 已保留 '+(downloadOffset/1024/1024).toFixed(1)+' MB，停留本页可继续。';downloadButton.textContent=downloadOffset?'继续网页内下载':'重试网页内下载';}
 finally{downloadRunning=false;downloadButton.disabled=false;}
};
downloadSave.onclick=function(){downloadStatus.textContent='已请求浏览器保存。请在手机“下载”列表打开 '+downloadRelease.filename+'，选择“更新”。若仍提示失败，可改用下方“备用：系统下载”。';};
document.getElementById('download').onclick=async function(event){
 if(downloadRelease&&downloadRelease.download_expires_at>Date.now()/1000+30)return;
 event.preventDefault();try{await refreshDownloadRelease();this.click();}catch(error){downloadStatus.textContent=error.message;}
};
refreshDownloadRelease().then(function(){
 if(downloadNative){downloadButton.hidden=true;downloadStatus.textContent='当前是应用内页面，请使用下方“备用：系统下载”。网页内下载适用于手机浏览器。';}
 else{downloadButton.disabled=false;downloadStatus.textContent='下载入口已就绪。点“网页内下载”，传完并校验后再保存。';}
}).catch(function(error){downloadStatus.textContent=error.message;document.getElementById('package').textContent='下载入口检查未通过';});
window.addEventListener('beforeunload',function(event){if(downloadRunning){event.preventDefault();event.returnValue='';}});
window.addEventListener('pagehide',function(){if(downloadBlobURL)URL.revokeObjectURL(downloadBlobURL);});
