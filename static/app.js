'use strict';
const SPLIT_STORAGE_KEY = 'aivideotranscriber.split', TOP_TAB_STORAGE_KEY = 'aivideotranscriber.topTab';
function migrateStorageKey(newKey, oldKey) {
  try {
    if (localStorage.getItem(newKey) === null) {
      const oldValue = localStorage.getItem(oldKey);
      if (oldValue !== null) localStorage.setItem(newKey, oldValue);
    }
    localStorage.removeItem(oldKey);
  } catch {}
}
migrateStorageKey(SPLIT_STORAGE_KEY, 'transcriptdesk.split');
migrateStorageKey(TOP_TAB_STORAGE_KEY, 'transcriptdesk.topTab');
const SPLIT_DEFAULT = 182, SPLIT_MIN = 90, SPLIT_BOTTOM_MIN = 120;
function splitTotalForWidth(width) { return Number(width)>=900?630:518; }
function clampSplitHeight(value, total) {
  const numeric=Number(value), maximum=Math.max(SPLIT_MIN,Number(total)-SPLIT_BOTTOM_MIN);
  return Math.min(maximum,Math.max(SPLIT_MIN,Number.isFinite(numeric)?numeric:SPLIT_DEFAULT));
}
function splitBottomMax(height, width) {
  const total=splitTotalForWidth(width);
  return total-clampSplitHeight(height,total);
}
function storedSplitChoice() {
  try {
    const saved=localStorage.getItem(SPLIT_STORAGE_KEY);
    return saved===null?SPLIT_DEFAULT:Number(saved);
  } catch {return SPLIT_DEFAULT;}
}
function persistSplitHeight(height) {
  try {localStorage.setItem(SPLIT_STORAGE_KEY,String(height));} catch {}
}
let chosenSplitHeight=storedSplitChoice();
let splitHeight=clampSplitHeight(chosenSplitHeight,splitTotalForWidth(window.innerWidth));
function resizeSplitHeight(width=window.innerWidth) {
  splitHeight=clampSplitHeight(chosenSplitHeight,splitTotalForWidth(width));
  return splitHeight;
}
const $ = id => document.getElementById(id);
const labels = {gemini:'Gemini',supadata:'Supadata',fetchtranscript:'FetchTranscript',antigravity:'Antigravity local audio',whisper:'Whisper (this PC)',import:'Manual import'};
const services = ['gemini','supadata','fetchtranscript','antigravity'], keyedServices = ['gemini','supadata','fetchtranscript'];
const topTabNames = ['add','library','activity'], topTabKey = TOP_TAB_STORAGE_KEY;
const ACTIVE = ['queued','running'];
let boot, state = {videos:[],jobs:[],providers:[]}, current = null, selectedId = '', tab = 'raw', topTab = 'add', noticeTimer;
let polling = false, selecting = 0, connection = 'connecting', localFileReadiness = null, localFileBusy = false;
let localFilePhase = '', localFileUploadPercent = 0, localFileStatusOverride = '', localFileStatusTone = '';
// Jobs this page saw queued or running, so a finished one is reported once in the working bar (not old ones on load).
let seenActive = new Set(), finished = null, finishedAt = 0, librarySignature = '', activitySignature = '';
// Settings dialog state: keys marked for removal, Google's model list for the saved Gemini key (null until loaded) and the check of that key, the CLI picked in the list.
let removing = {}, geminiModels = null, geminiCheck = {text:'',tone:''}, geminiLoad = 0, opencodeLoad = 0, cliProvider = 'codex', aiMark = null;
const choices = AIVideoTranscriberChoices, OTHER = choices.OTHER, activity = AIVideoTranscriberActivity;
// The model and effort fields (gemini_model, ai_model, ai_effort) are selects with an "Other" text box, so they are read separately.
const settingKeys = ['primary','fallback','language','gemini_fallback_models','gemini_clip_seconds','instructions','format_instructions','summary_instructions','ask_instructions'];
function node(tag, text, cls) { const e = document.createElement(tag); if(text !== undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function notify(message, error=false) { $('notice').textContent=message; $('notice').className='notice'+(error?' error':''); $('notice').hidden=false; clearTimeout(noticeTimer); noticeTimer=setTimeout(()=>$('notice').hidden=true,error?14000:6500); }
async function api(path, body) {
  if(path==='/api/ai' && boot?.settings?.ai_provider==='opencode') {
    const catalog=await openCodeModels();
    if(catalog.error)throw Error(catalog.error);
    const model=boot.settings.ai_model||boot.ai.opencode.default_model;
    if(!(catalog.models||[]).some(item=>item.id===model))throw Error("Your saved OpenCode model is no longer listed. Choose a listed model in Settings.");
  }
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-AIVideoTranscriber-Token':boot.token},body:JSON.stringify(body)};
  let response=await fetch(path,options);
  if(response.status===403 && body!==undefined) {
    const refreshed=await fetch('/api/bootstrap');
    if(refreshed.ok) {
      boot=await refreshed.json();
      options.headers['X-AIVideoTranscriber-Token']=boot.token;
      response=await fetch(path,options);
    }
  }
  const result=await response.json();
  if(path==='/api/bootstrap' && typeof result.version==='string')$('appVersion').textContent=`v${result.version}`;
  if(!response.ok)throw Error(result.error||`Request failed (${response.status})`);
  return result;
}
async function guarded(fn) {try{return await fn();}catch(e){notify(e.message,true);}}
function fillSelect(select, options, selected) {
  select.replaceChildren(...options.map(o=>{const e=node('option',o.text);e.value=o.value;return e;}));
  select.value=selected;
}
function storedTopTab() {
  try {
    const saved=localStorage.getItem(topTabKey);
    return topTabNames.includes(saved)?saved:'add';
  } catch {return 'add';}
}
function renderTopWorkspace(remember=true) {
  document.querySelectorAll('[data-top-tab]').forEach(button=>{
    const active=button.dataset.topTab===topTab;
    button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
  });
  for(const item of topTabNames)$('top-panel-'+item).hidden=item!==topTab;
  if(remember)try{localStorage.setItem(topTabKey,topTab);}catch{}
}
function selectTopTab(name) {
  topTab=topTabNames.includes(name)?name:'add';
  renderTopWorkspace();
  if(topTab==='activity'){markedJob='';markActivity();}  // opening Activity shows the entry for what is on screen
}
function applySplitHeight() {
  const total=splitTotalForWidth(window.innerWidth);
  document.documentElement.style.setProperty('--split-height',`${splitHeight}px`);
  document.documentElement.style.setProperty('--split-bottom-max',`${total-splitHeight}px`);
  const separator=$('splitResizer');
  separator.setAttribute('aria-valuemin',String(SPLIT_MIN));
  separator.setAttribute('aria-valuemax',String(total-SPLIT_BOTTOM_MIN));
  separator.setAttribute('aria-valuenow',String(splitHeight));
  separator.setAttribute('aria-valuetext',`${splitHeight} pixels`);
}
function updateSplitHeight(value, save=true) {
  splitHeight=clampSplitHeight(value,splitTotalForWidth(window.innerWidth));
  if(save)chosenSplitHeight=splitHeight;
  applySplitHeight();
  if(save)persistSplitHeight(splitHeight);
}
function installSplitResizer() {
  const separator=$('splitResizer');
  applySplitHeight();
  let drag=null;
  separator.addEventListener('pointerdown',event=>{
    if(event.pointerType==='mouse' && event.button!==0)return;
    event.preventDefault();
    drag={pointerId:event.pointerId,originY:event.clientY,originHeight:splitHeight};
    separator.setPointerCapture(event.pointerId);
  });
  separator.addEventListener('pointermove',event=>{
    if(!drag || event.pointerId!==drag.pointerId)return;
    updateSplitHeight(drag.originHeight+event.clientY-drag.originY,false);
  });
  const finishDrag=event=>{
    if(!drag || event.pointerId!==drag.pointerId)return;
    drag=null;
    chosenSplitHeight=splitHeight;
    persistSplitHeight(splitHeight);
    if(separator.hasPointerCapture(event.pointerId))separator.releasePointerCapture(event.pointerId);
  };
  separator.addEventListener('pointerup',finishDrag);
  separator.addEventListener('pointercancel',finishDrag);
  separator.addEventListener('keydown',event=>{
    if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
    event.preventDefault();
    const next=event.key==='Home'?SPLIT_MIN:event.key==='End'?splitTotalForWidth(window.innerWidth)-SPLIT_BOTTOM_MIN:
      splitHeight+(event.key==='ArrowDown'?20:-20);
    updateSplitHeight(next);
  });
  window.addEventListener('resize',()=>{
    resizeSplitHeight();
    applySplitHeight();
  });
}
// Markdown from a model answer as DOM (never HTML). Falls back to plain text if rendering ever fails, so the view is never blank.
function markdownFragment(text) {
  try{return AIVideoTranscriberMarkdown.renderMarkdown(text,{videoUrl:current?.url});}
  catch{const fragment=document.createDocumentFragment();fragment.append(node('p',text));return fragment;}
}
function markdownBox(text, cls) {const box=node('div',undefined,cls?'md '+cls:'md');box.append(markdownFragment(text));return box;}
function source() {return current?.sources?.[current.selected_provider];}
function isActive(job) {return ACTIVE.includes(job.status);}
function activeJob(kind, id=selectedId) {return state.jobs.find(j=>j.video_id===id && (!kind || j.kind===kind) && isActive(j));}
function timeLabel(at) {try{const value=new Date(at);return !at||Number.isNaN(value.valueOf())?'—':value.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}catch{return '—';}}
// The time alone for today, otherwise the date too ("Oct 15, 01:17 PM"); always the date when asked.
function whenLabel(at, always=false) {try{const value=new Date(at);if(!at||Number.isNaN(value.valueOf()))return '—';return !always && value.toDateString()===new Date().toDateString()?timeLabel(at):value.toLocaleDateString([],{month:'short',day:'numeric'})+', '+timeLabel(at);}catch{return '—';}}
function timestamp(value) {const s=Math.max(0,Math.floor(value));return [Math.floor(s/3600),Math.floor(s/60)%60,s%60].map(n=>String(n).padStart(2,'0')).join(':');}
// An AI CLI by its name ("Codex"), not its lowercase id.
const aiName = provider => (boot?.ai?.[provider]?.label||provider||'').replace(/ CLI$/,'');
function cliLabel() {return boot?.ai?.[boot.settings.ai_provider]?.label||'your AI CLI';}
// A progress track: a filled share when the job tells how far along it is, a sliding bar when it cannot.
function setTrack(track, percent) {
  track.classList.toggle('indeterminate',percent===null || percent===undefined);
  track.firstElementChild.style.width=percent===null || percent===undefined?'':Math.max(3,Math.min(100,percent))+'%';
}
function renderHeaderActivity() {
  const view=activity.describeActivity(state,connection);
  $('headerActivityText').textContent=view.text;
  $('headerActivity').dataset.tone=view.tone;
  $('headerActivity').title=view.title;
  $('headerActivity').hidden=view.tone==='busy' || view.tone==='waiting' && connection==='connected';  // the working bar shows it
}
const DONE = {fetch:'Transcript saved',formatted:'Formatted version saved',summary:'Summary saved',ask:'Answer ready'};
const FAILED = {fetch:'Could not get the transcript',formatted:'Formatting failed',summary:'Summary failed',ask:'Question failed'};
// The working bar under the header: what is running right now, from any tab, and how a finished job ended.
function renderWorkBar() {
  const bar=$('workBar'), work=activity.describeWork(state);
  if(work) {
    bar.dataset.tone='busy';$('workSpinner').hidden=false;$('workMark').hidden=true;$('workTrack').hidden=false;
    $('workAction').textContent=work.action+(work.batch?` · ${work.batch}`:'');
    $('workTitle').textContent=work.title;
    $('workDetail').textContent=[work.detail,work.more].filter(Boolean).join(' · ');
    setTrack($('workTrack'),work.percent);
    bar.title=work.message;
    $('workOpen').hidden=work.job.video_id===selectedId;$('workOpen').dataset.video=work.job.video_id;
    $('workCancel').hidden=false;$('workCancel').dataset.job=work.job.id;
    $('workRetry').hidden=$('workDismiss').hidden=true;
    bar.hidden=false;return;
  }
  if(finished && Date.now()-finishedAt>120000)finished=null;  // a finished message stays two minutes, or until hidden
  if(finished) {
    const job=finished, ok=job.status==='completed', canceled=job.status==='canceled';
    const video=state.videos.find(v=>v.id===job.video_id);
    bar.dataset.tone=ok?'done':canceled?'muted':'error';$('workSpinner').hidden=$('workTrack').hidden=true;
    $('workMark').hidden=false;$('workMark').textContent=ok?'✓':canceled?'–':'✕';
    $('workAction').textContent=ok?DONE[job.kind]||'Done':canceled?'Canceled':FAILED[job.kind]||'Failed';
    $('workTitle').textContent=activity.videoTitle(job.video_id,state.videos);
    $('workDetail').textContent=ok?(job.kind==='fetch' && video?`${video.words.toLocaleString()} words`:''):canceled?'':job.message||'';
    bar.title=job.message||'';
    $('workOpen').hidden=!video || job.video_id===selectedId;$('workOpen').dataset.video=job.video_id;
    $('workRetry').hidden=ok||canceled;$('workCancel').hidden=true;$('workDismiss').hidden=false;
    bar.hidden=false;return;
  }
  bar.hidden=true;
}
// The toolbar's AI assistant select lists the CLIs the server reports. It is rebuilt only when that list changes, so a dropdown that is open is not closed by the next poll.
function renderAiProviders() {
  const view=choices.aiProviderChoices(boot.ai), signature=JSON.stringify(view);
  if($('aiProvider').dataset.options!==signature){fillSelect($('aiProvider'),view,boot.settings.ai_provider);$('aiProvider').dataset.options=signature;}
  $('aiProvider').value=boot.settings.ai_provider;
}
function updateHeader() {
  if(!boot)return;
  renderHeaderActivity();
  $('serviceNotice').hidden=Object.values(boot.settings.keys_configured).some(Boolean);
  const statuses=[];
  for(const p of services) {
    const checked=state.providers.find(v=>v.provider===p), configured=boot.settings.keys_configured[p];
    let text=!configured?'key missing':checked?(checked.status==='success'?'last request OK':checked.code||'last request failed'):(p==='antigravity'?'signed-in CLI used when requested':'ready to test');
    statuses.push(`${labels[p]} · ${text}`);
  }
  $('settingsButton').title=statuses.join('\n')+`\nPrimary: ${labels[boot.settings.primary]} → ${labels[boot.settings.fallback]||'no fallback'}`;
  renderAiProviders();
  $('aiStatus').textContent=choices.aiStatus(boot.ai[boot.settings.ai_provider],boot.settings.ai_model);
  $('exportAll').title='Download every saved transcript as a zip.\nLibrary folder: '+boot.data_dir;
  const running=state.jobs.filter(isActive).length;
  $('activityBadge').textContent=running||'';$('activityBadge').hidden=!running;
  $('libraryBadge').textContent=state.videos.length||'';
}
async function deleteVideo(video) {
  if(!confirm(`Delete “${video.title}”?\n\nThis removes its transcript, formatted version, summary and questions from your library. It cannot be undone.`))return;
  await api('/api/delete',{id:video.id});
  if(selectedId===video.id){selectedId='';current=null;}
  if(finished?.video_id===video.id)finished=null;
  notify(`Deleted “${video.title}”.`);
  await poll();
}
// Rebuilt only when something shown changes, so a click is never lost to a row replaced mid-click by the next poll.
function renderLibrary() {
  const list=$('library'), filter=$('search').value.toLowerCase();
  const videos=state.videos.filter(v=>(v.title+' '+v.id).toLowerCase().includes(filter));
  const busy=new Set(state.jobs.filter(isActive).map(j=>j.video_id));
  const signature=JSON.stringify([videos,[...busy],selectedId,filter]);
  if(signature===librarySignature)return;
  librarySignature=signature;list.replaceChildren();
  $('libraryCount').textContent=`${state.videos.length} saved`;
  if(!videos.length)list.append(node('div',filter?'No matching videos.':'Your saved videos will appear here. Add some in Add videos.','library-empty'));
  for(const video of videos) {
    const row=node('div',undefined,'video-row'+(video.id===selectedId?' active':'')), open=node('button',undefined,'video-open');
    const working=busy.has(video.id);
    const sourceLabel=video.has_transcript?(labels[video.provider]||video.provider):(working?'getting transcript…':video.file_retry_message||'no transcript yet');
    let info=video.has_transcript?`${sourceLabel} · ${video.words.toLocaleString()} words · saved ${whenLabel(video.saved||video.updated)}`:sourceLabel;
    if(video.is_sample)info='SAMPLE · '+info;
    open.append(node('span',video.title,'video-name'),node('span',info,'video-detail'));open.title=video.title;
    open.addEventListener('click',()=>guarded(()=>selectVideo(video.id)));
    const remove=node('button','Delete','video-delete');remove.disabled=working;
    remove.title=working?'A task for this video is running. Cancel it first.':'Delete this video and everything made from it';
    remove.addEventListener('click',()=>guarded(()=>deleteVideo(video)));
    row.append(open,remove);list.append(row);
  }
}
const KINDS = {fetch:'Transcript',file:'Local file transcript',formatted:'Formatted version',summary:'Summary',ask:'Question'};
const STATES = {queued:'Waiting',running:'Running',completed:'Done',failed:'Failed',canceled:'Canceled',interrupted:'Stopped'};
async function retryJob(job) {
  if(job.kind==='fetch')await api('/api/batch',{urls:job.video_id,mode:job.mode,compare:job.compare,refetch:job.rerun??job.refetch??job.force});
  else if(job.kind==='file')return;
  else await api('/api/ai',{id:job.video_id,kind:job.kind,question:job.question||''});
  finished=null;await poll();
}
// One plain line per task; the full service and model detail is in the tooltip.
function jobMessage(job) {
  const video=state.videos.find(v=>v.id===job.video_id);
  if(job.kind==='fetch' && job.status==='completed' && video?.has_transcript)return `Saved · ${labels[video.provider]||video.provider} · ${video.words.toLocaleString()} words`;
  if(job.kind==='file' && job.status==='completed' && video?.has_transcript)return `Saved · ${labels[video.provider]||video.provider} · ${video.words.toLocaleString()} words`;
  return job.kind==='ask' && job.question?`“${job.question}” · ${job.message}`:job.message;
}
const TAB_OF = {fetch:'raw',file:'raw',formatted:'formatted',summary:'summary',ask:'ask'};
// The Activity entry for what the view shows: the task running for it, else the newest finished one that saved it.
function shownJobId() {
  if(!current)return '';
  const mine=state.jobs.filter(j=>j.video_id===selectedId && TAB_OF[j.kind]===tab);
  return (mine.find(isActive)||mine.filter(j=>j.status==='completed').sort(newestFirst)[0])?.id||'';
}
// Finished tasks newest first by when they finished (not by when their entry was last saved).
const jobEnded=job=>Date.parse(job.finished||job.started||job.created)||0;
const newestFirst=(a,b)=>jobEnded(b)-jobEnded(a);
// When what is shown changes, the list scrolls just enough to show its entry (never on a plain refresh, so it does not fight your scrolling).
let markedJob='';
function markActivity() {
  const shown=shownJobId();let row=null;
  $('activity').querySelectorAll('.job-row').forEach(r=>{const on=!!shown && r.dataset.job===shown;r.classList.toggle('active',on);if(on)row=r;});
  if(row && shown!==markedJob && topTab==='activity')row.scrollIntoView({block:'nearest'});
  markedJob=shown;
}
const removeActivity=body=>guarded(async()=>{await api('/api/activity-remove',body);activitySignature='';await poll();});
function renderActivity() {
  const active=state.jobs.filter(isActive), recent=state.jobs.filter(j=>!isActive(j)).sort(newestFirst).slice(0,20);
  const signature=JSON.stringify([active,recent.map(j=>[j.id,j.status,j.message]),state.videos.map(v=>[v.id,v.title])]);
  if(signature===activitySignature)return markActivity();
  activitySignature=signature;
  const list=$('activity');list.replaceChildren();
  if(!state.jobs.length)list.append(node('div','Nothing yet. Tasks you start show here.','activity-empty'));
  if(recent.length) {
    const head=node('div',undefined,'activity-head'), clear=node('button','Clear finished','text-button');
    clear.title='Remove every finished entry from this list. Saved transcripts and results stay in the Library.';
    clear.onclick=()=>{if(confirm('Clear all finished entries from Activity?\n\nSaved transcripts and results stay in the Library.'))removeActivity({finished:true});};
    head.append(node('span',`${active.length?active.length+' running or waiting · ':''}${recent.length} finished`),clear);list.append(head);
  }
  for(const job of [...active,...recent]) {
    const row=node('div',undefined,'job-row'), body=node('div',undefined,'job-body'), title=node('div',undefined,'job-title');row.dataset.job=job.id;
    title.append(node('span',job.kind==='fetch' && job.compare?'Compare services':KINDS[job.kind]||job.kind,'job-kind'),activity.videoTitle(job.video_id,state.videos));
    const message=node('div',jobMessage(job),'job-message');message.title=job.message||'';
    body.append(title,message);
    row.append(node('span',STATES[job.status]||job.status,'job-state '+job.status),body,node('span',whenLabel(job.finished||job.started||job.created,true),'job-time'));
    if(state.videos.some(v=>v.id===job.video_id)) {
      // Clicking the row shows this task's own view: a transcript opens Raw, a summary opens Summary. Its buttons do their own job.
      const open=()=>guarded(()=>{tab=TAB_OF[job.kind]||'raw';return selectVideo(job.video_id);});
      row.classList.add('openable');row.tabIndex=0;row.title='Open this below';
      row.addEventListener('click',event=>{if(!event.target.closest('button'))open();});
      row.addEventListener('keydown',event=>{if(event.target===row && (event.key==='Enter' || event.key===' ')){event.preventDefault();open();}});
    }
    if(isActive(job)) {
      const cancel=node('button','Cancel','secondary');cancel.addEventListener('click',()=>guarded(async()=>{await api('/api/cancel',{job_id:job.id});await poll();}));row.append(cancel);
    } else if(['failed','interrupted','canceled'].includes(job.status) && job.kind!=='file') {
      const retry=node('button','Retry','secondary');retry.addEventListener('click',()=>guarded(()=>retryJob(job)));row.append(retry);
    }
    if(!isActive(job)) {
      const remove=node('button','Remove','job-remove');remove.title='Remove this entry from Activity. The saved transcript and results stay.';
      remove.addEventListener('click',()=>removeActivity({job_id:job.id}));row.append(remove);
    }
    list.append(row);
  }
  markActivity();
}
async function selectVideo(id) {
  const request=++selecting; selectedId=id; renderLibrary();
  const video=await api('/api/video?id='+encodeURIComponent(id));
  if(request!==selecting)return;
  current=video;renderDocument();renderWorkBar();
}
function displayText() {
  const s=source();if(!s)return '';
  if(tab==='raw')return s[$('timestamps').checked?'timed_text':'text'];
  if(tab==='ask')return current.conversation.map(c=>'Q: '+c.question+'\n\n'+c.answer).join('\n\n');
  return current.outputs[tab]?.text||'';
}
// The job shown inside the open video: a transcript being fetched, or the AI task of the open view.
function viewJob() {return current?(activeJob('file',current.id)||activeJob('fetch',current.id)||(tab!=='raw'?activeJob(tab,current.id):null)):null;}
function renderJobStatus() {
  // The working bar at the top already shows its one task; the card repeats nothing it shows.
  const job=viewJob(), inBar=Boolean(job) && activity.describeWork(state)?.job.id===job.id;
  $('jobStatus').hidden=!job || inBar;
  if(!job || inBar)return;
  const view=activity.describeJob(job,state.videos);
  $('jobStatusAction').textContent=view.action;
  let detail=view.detail;
  if(job.kind==='fetch' && source())detail+=' · the saved transcript stays until the new one is ready';
  $('jobStatusDetail').textContent=detail;$('jobStatus').title=view.message;
  setTrack($('jobStatusTrack'),view.percent);
  $('jobStatusCancel').dataset.job=job.id;
}
// One empty-state card at the top of the view: what is missing and the one button that fixes it.
function renderEmpty(view) {
  $('empty').hidden=!view;if(!view)return;
  $('emptyTitle').textContent=view.title;$('emptyText').textContent=view.text||'';$('emptyHint').textContent=view.hint||'';
  $('emptyAction').hidden=!view.action;$('emptyAction').textContent=view.label||'';$('emptyAction').dataset.action=view.action||'';$('emptyAction').disabled=!!view.disabled;
  $('emptySecondary').hidden=!view.secondary;$('emptySecondary').textContent=view.secondary||'';
}
function renderDocument() {
  markActivity();  // the Activity entry for what is shown stays highlighted
  const s=source(), content=$('transcript'), chat=$('conversation');
  const ai=['formatted','summary'].includes(tab), result=ai?current?.outputs?.[tab]:null;
  const shortImport=s?.provider==='import' && s.words<2;
  const fetching=current?(activeJob('file',current.id)||activeJob('fetch',current.id)):null, job=current && tab!=='raw'?activeJob(tab,current.id):null;
  const oldScroll=content.scrollTop, chatScroll=chat.scrollTop;
  // Title and the short "Transcript by" line; the per-clip model detail is in the tooltip and Provider checks.
  $('videoTitle').textContent=current?activity.videoTitle(current.id,[current]):'No video open';
  $('videoTitle').previousElementSibling.textContent=current?.local_file?'File:':'YouTube Title:';
  const hasUrl=current?.url?.startsWith('https://www.youtube.com/watch?v=');
  $('openVideo').hidden=!hasUrl; if(hasUrl)$('openVideo').href=current.url;
  const credit=choices.geminiPrimaryModel(s);
  $('videoMeta').textContent=s?[current.is_sample?'SAMPLE':'',labels[s.provider]||s.provider,credit.text==='CLI default'?'':credit.text,s.language==='unknown'?'':s.language,`${s.words.toLocaleString()} words`,`saved ${whenLabel(s.fetched_at)}`].filter(Boolean).join(' · ')
    :current?(fetching?'getting the transcript now…':'no transcript yet'):'—';
  $('videoMeta').title=s?[choices.geminiProvenance(s),credit.title].filter(Boolean).join('\n'):'';
  const warnings=[];
  const completionWarning=choices.completionWarning(s);
  if(completionWarning)warnings.push(completionWarning);
  if(s?.provider==='import')warnings.push(shortImport?'Only one word was saved. This is not a usable transcript. Paste the full text or get a transcript from the video.':'This is text you pasted; it was not retrieved from the video.');
  if(result && s && result.source_hash!==s.hash)warnings.push('The transcript changed after this was made. Regenerate to match it.');
  $('viewWarning').textContent=warnings.join(' · ');$('viewWarning').hidden=!warnings.length;
  // Tabs and the tools that belong to the open view.
  $('viewLabel').textContent={raw:'ORIGINAL TRANSCRIPT',formatted:'AI FORMATTED VERSION',summary:'AI SUMMARY',ask:'TRANSCRIPT CONVERSATION'}[tab];
  document.querySelectorAll('[data-tab]').forEach(e=>{e.setAttribute('aria-selected',String(e.dataset.tab===tab));e.tabIndex=e.dataset.tab===tab?0:-1;});
  document.querySelectorAll('[data-tools]').forEach(group=>group.hidden=!group.dataset.tools.split(' ').includes(tab));
  $('panel').setAttribute('aria-labelledby','tab-'+tab);
  $('retrieveCurrent').hidden=!hasUrl || !s || !!current?.local_file;
  $('retrieveCurrent').textContent=s?.provider==='import'?'Get transcript from video':'Get transcript again';
  $('retrieveCurrent').title=`Uses the "using" choice in Add videos: ${$('mode').selectedOptions[0]?.textContent||'Primary + fallback'}.`;
  $('retrieveCurrent').disabled=!!fetching;
  $('wrapControl').hidden=!s;$('timestampsControl').hidden=!s || s.timed_text===s.text;  // a transcript without timestamps (local audio) has nothing to toggle
  $('generate').hidden=!result;$('generate').disabled=shortImport || !!job;
  $('generate').title=`Make it again with ${cliLabel()} and your saved instructions.`;
  renderJobStatus();
  // What the view holds: an empty-state card, the text, or the conversation.
  let empty=null;
  if(!current)empty=state.videos.length?{title:'Pick a video',text:'Open one from the Library tab above.',action:'library',label:'Open Library'}
    :{title:'Start by adding a video',text:'Paste one or more YouTube links in Add videos above and click Get transcripts. Each transcript opens here when it is ready.',action:'add',label:'Paste links'};
  else if(!s && fetching)empty={title:'Getting the transcript…',text:'It appears here as soon as it is ready. You can keep using the app meanwhile.'};
  else if(!s)empty={title:'No transcript yet',text:current.local_file?'Choose the file again to retry transcription.':hasUrl?'Get the transcript for this video.':'This entry has no transcript.',action:hasUrl?'fetch':'',label:'Get transcript',hint:hasUrl?`Uses ${$('mode').selectedOptions[0]?.textContent||'Primary + fallback'} (the "using" choice in Add videos).`:''};
  else if(ai && !result && !job?.partial) {
    const formatted=tab==='formatted';
    empty=job?{title:formatted?'Writing the formatted version…':'Writing the summary…',text:'The draft appears here as each part finishes.'}
      :{title:formatted?'No formatted version yet':'No summary yet',
        text:formatted?'Make a clean, readable copy of the transcript, with punctuation, paragraphs and obvious word fixes. The original stays unchanged.':'Get the main points of the video, with timestamp links.',
        action:'generate',label:formatted?'Generate formatted version':'Generate summary',disabled:shortImport,secondary:'Edit instructions',
        hint:shortImport?'Paste the full transcript before using AI.':`Uses ${cliLabel()}. Long transcripts are done in parts; progress shows above.`};
  }
  renderEmpty(empty);
  const draft=ai && job?.partial;
  const showText=!!s && (tab==='raw' || (ai && !!(draft || result)));
  $('draftLabel').hidden=!draft;
  const ask=tab==='ask' && !!s, pending=ask?activeJob('ask',current.id):null;
  const signature=AIVideoTranscriberSelection.signature([current?.id,tab,s?.hash,$('timestamps').checked,result?.created,draft?job.partial.length:0,current?.conversation?.length,current?.conversation?.at(-1)?.created,pending?.id,pending?.partial?.length]);
  const redraw=AIVideoTranscriberSelection.shouldRender(content.dataset.signature,signature);
  if(redraw){content.replaceChildren();content.dataset.signature=signature;}
  content.hidden=!showText; content.classList.toggle('nowrap',tab==='raw' && !$('wrap').checked); content.classList.toggle('rendered',tab!=='raw');content.classList.toggle('draft',!!draft);
  chat.hidden=!ask;$('askForm').hidden=!ask;
  const info=$('viewInfo');info.className='view-info';info.textContent='';
  if(showText && tab==='raw' && redraw) {
    if($('timestamps').checked && s.segments.length) {
      for(const segment of s.segments) {
        const line=node('div',undefined,'transcript-line');
        const stampNode=node(current.local_file?'span':'a','['+timestamp(segment.start)+']','timestamp');
        if(!current.local_file){stampNode.href=current.url+'&t='+Math.floor(segment.start);stampNode.target='_blank';stampNode.rel='noopener noreferrer';}
        line.append(stampNode,document.createTextNode(segment.text));content.append(line);
      }
    } else content.textContent=displayText();
  } else if(showText && ai && redraw) content.append(markdownBox(draft?job.partial:result.text));
  if(ai && result && !draft)info.textContent=`Made by ${aiName(result.provider)} · ${result.model} · ${result.parts} part${result.parts===1?'':'s'} · ${whenLabel(result.created)}`;
  if(ask) {
    // Newest first, right under the question box; a question being answered sits at the top.
    if(redraw) {
      chat.replaceChildren();
      if(pending) {
        const block=node('div',undefined,'exchange pending');
        block.append(node('div',pending.question||'','exchange-question'),pending.partial?markdownBox(pending.partial,'exchange-answer'):node('div','Working on an answer…','exchange-answer'));
        chat.append(block);
      }
      if(!current.conversation.length && !pending)chat.append(node('p','Ask a question above to start a conversation about this transcript.','conversation-empty'));
      for(const exchange of [...current.conversation].reverse()) {
        const block=node('div',undefined,'exchange');
        block.append(node('div',exchange.question,'exchange-question'),markdownBox(exchange.answer,'exchange-answer'));
        block.append(node('div',`${aiName(exchange.provider)} · ${exchange.model} · ${whenLabel(exchange.created)}${exchange.source_hash!==s.hash?' · earlier/different source':''}`,'exchange-meta'));
        chat.append(block);
      }
    }
    $('askButton').disabled=shortImport || !!pending;
  }
  for(const id of ['copy','download'])$(id).disabled=!displayText();
  reader?.viewChanged(current?current.id+':'+tab:'');renderReadControls();
  renderChecks();content.scrollTop=oldScroll;chat.scrollTop=chatScroll;
}
function renderChecks() {
  $('checkRows').replaceChildren(); const attempts=current?.attempts||[];
  $('checksCount').textContent=attempts.length?`${attempts.length} attempt${attempts.length===1?'':'s'}`:'Not tested';
  for(const attempt of [...attempts].reverse()) {
    const row=node('tr');
    for(const value of [labels[attempt.provider],attempt.status,`${attempt.seconds??'—'}s`,attempt.words?.toLocaleString()||'—',attempt.language||'—',localFileUserMessage(attempt.message)])row.append(node('td',value));
    row.children[1].className='status-'+attempt.status;
    if(attempt.credits_remaining!==null && attempt.credits_remaining!==undefined)row.children[5].textContent+=' · '+attempt.credits_remaining+' credits remaining';
    const usedModels=choices.geminiProvenance(attempt);
    if(usedModels)row.children[5].textContent+=` · ${usedModels}`;
    if(attempt.clip_model_summary)row.children[5].textContent+=` · ${attempt.clip_model_summary}`;
    if(attempt.clips)row.children[5].textContent+=` · ${attempt.clips} clip${attempt.clips===1?'':'s'}`+(attempt.requests?` · ${attempt.requests} request${attempt.requests===1?'':'s'}`:'')+(attempt.input_tokens!=null?` · ${attempt.input_tokens.toLocaleString()} input tokens`:'')+(attempt.output_tokens!=null?` · ${attempt.output_tokens.toLocaleString()} output tokens`:'');
    $('checkRows').append(row);
  }
  if(!attempts.length){const row=node('tr'), cell=node('td','Use “Compare services” to compare live retrieval.');cell.colSpan=6;row.append(cell);$('checkRows').append(row);}
  const chooser=$('sourceProvider');chooser.replaceChildren();
  for(const p of Object.keys(current?.sources||{})){const option=node('option',labels[p]||p);option.value=p;chooser.append(option);}
  chooser.value=current?.selected_provider||'';chooser.disabled=!source();$('downloadJson').disabled=!source();
}
// The CLI's models and "last used" model change as AI answers are saved, so the page asks again when one finishes and when Settings opens.
async function refreshAi() {try{boot.ai=(await api('/api/bootstrap')).ai;updateHeader();}catch{}}
function trackFinished() {
  for(const job of state.jobs) {
    if(isActive(job))seenActive.add(job.id);
    else if(seenActive.delete(job.id)){finished=job;finishedAt=Date.now();}
  }
  if(finished)finished=state.jobs.find(j=>j.id===finished.id)||finished;
}
async function poll() {
  if(polling)return;polling=true;
  try {
    state=await api('/api/state');connection='connected';
    const mark=state.jobs.find(j=>j.kind!=='fetch' && j.status==='completed')?.id ?? '';
    if(aiMark!==null && mark!==aiMark)await refreshAi();
    aiMark=mark;trackFinished();
    if(selectedId && !state.videos.some(v=>v.id===selectedId)){selectedId='';current=null;}
    updateHeader();renderLibrary();renderActivity();renderWorkBar();
    if(!selectedId && state.videos.length)await selectVideo(state.videos[0].id);
    else if(selectedId) {
      const entry=state.videos.find(v=>v.id===selectedId);
      if(entry && (!current || current.id!==selectedId || entry.updated!==current.updated))await selectVideo(selectedId);
      else renderDocument();
    } else renderDocument();
  } finally {polling=false;}
}
// ---- Settings dialog: API keys ----
const typedKey = p => $(p+'_key').value.trim();
function setSettingsMessage(text, error=false) {$('settingsMessage').textContent=text;$('settingsMessage').dataset.tone=error?'error':'';}
// Status beside the key label, placeholder, Remove key button and note. Never shows any part of a key.
function renderKey(p) {
  const typed=!!typedKey(p), saved=!!boot.settings.keys_saved[p], remove=saved && !!removing[p];
  const view=choices.keyView({configured:!!boot.settings.keys_configured[p],saved,remove,typed});
  $(p+'_state').textContent=view.text;$(p+'_state').dataset.tone=view.tone;
  $(p+'_key').placeholder=view.placeholder;
  $(p+'_remove').hidden=!view.action;$(p+'_remove').textContent=view.action;
  const check=p==='gemini' && !typed && !remove ? geminiCheck : {text:'',tone:''};
  $(p+'KeyNote').textContent=view.note||check.text;$(p+'KeyNote').dataset.tone=view.note?'':check.tone;
}
for(const p of keyedServices) {
  $(p+'_key').oninput=()=>{if(typedKey(p))removing[p]=false;renderKey(p);};
  $(p+'_remove').onclick=()=>{removing[p]=!removing[p];if(removing[p])$(p+'_key').value='';renderKey(p);};
}
// ---- Settings dialog: Gemini model ----
function fillGemini(selected) {
  const view=choices.geminiModelChoices(geminiModels,boot.settings.gemini_model,selected);
  fillSelect($('gemini_model'),view.options,view.selected);
  $('gemini_model_custom').hidden=view.selected!==OTHER;
}
function updateGeminiClipHelp() {$('geminiClipHelp').textContent=choices.geminiClipHelp(Number($('gemini_clip_seconds').value));}
$('gemini_clip_seconds').oninput=updateGeminiClipHelp;
// Loads Google's model list for the saved key; the same call tells whether Google accepts the key. A newer call makes an older answer stale.
async function loadGemini(refresh=false) {
  const ticket=++geminiLoad, configured=!!boot.settings.keys_configured.gemini, note=$('geminiListNote');
  $('geminiRefresh').disabled=!configured;
  if(!configured) {
    geminiModels=null;geminiCheck={text:'',tone:''};fillGemini($('gemini_model').value);renderKey('gemini');
    note.textContent="Save a Gemini key to load Google's model list.";return;
  }
  $('geminiRefresh').disabled=true;note.textContent="Loading Google's model list…";geminiCheck={text:'Checking the key with Google…',tone:'muted'};renderKey('gemini');
  try {
    const result=await api('/api/gemini-models',{refresh});
    if(ticket!==geminiLoad)return;
    geminiModels=result.models;geminiCheck={text:'✓ Google accepted this key',tone:'ok'};
    note.textContent=`${result.models.length} models from Google.`;
  } catch(error) {
    if(ticket!==geminiLoad)return;
    geminiModels=null;geminiCheck={text:error.message,tone:'error'};
    note.textContent="Google's model list could not be loaded. The saved model is still available.";
  }
  $('geminiRefresh').disabled=false;fillGemini($('gemini_model').value);renderKey('gemini');
}
$('gemini_model').onchange=()=>{const other=$('gemini_model').value===OTHER;$('gemini_model_custom').hidden=!other;if(other)$('gemini_model_custom').focus();};
$('geminiRefresh').onclick=()=>loadGemini(true);
// ---- Settings dialog: local AI CLIs (radio list, with the model and effort of the chosen one) ----
const cliOptions = $('cliOptions');
const selectedCliModel = () => choices.modelValue($('ai_model').value,$('ai_model_custom').value);
const cliHasEfforts = () => choices.hasEfforts(boot.ai[cliProvider]);
function fillEffort(saved, keepSaved=false) {
  const view=choices.cliEffortChoices(boot.ai[cliProvider],selectedCliModel(),saved,keepSaved);
  fillSelect($('ai_effort'),view.options,view.selected);
  $('ai_effort').hidden=$('ai_effort_label').hidden=!cliHasEfforts();  // a CLI without an effort setting (Antigravity CLI) shows neither
}
function fillCli(model, effort, keepEffort=false) {
  const view=choices.cliModelChoices(boot.ai[cliProvider],model);
  if(cliProvider==='opencode') {
    const options=view.options.filter(option=>option.value!==OTHER), selected=model||boot.ai.opencode.default_model;
    const listed=options.some(option=>option.value===selected);
    fillSelect($('ai_model'),options,listed?selected:'');
    $('ai_model_custom').value='';$('ai_model_custom').hidden=true;
  } else {
    fillSelect($('ai_model'),view.options,view.selected);
    $('ai_model_custom').value=view.custom;$('ai_model_custom').hidden=view.selected!==OTHER;
  }
  fillEffort(effort,keepEffort);
}
// Antigravity CLI's models come from running `agy models`, which may open a Google sign-in page when its login has lapsed, so
// they are asked for only while Antigravity CLI is the CLI picked here, never when the page loads.
async function loadAntigravity() {
  if(cliProvider!=='antigravity' || !boot.ai.antigravity?.installed)return;
  try {
    const result=await api('/api/antigravity-models',{});
    if(boot.ai.antigravity)boot.ai.antigravity.models=result.models;
    if(!$('settingsDialog').open || cliProvider!=='antigravity')return;
    if(result.error)setSettingsMessage(result.error,true);
    fillCli(selectedCliModel(),'');
  } catch(error){if($('settingsDialog').open)setSettingsMessage(error.message,true);}
}
async function openCodeModels() {
  const result=await api('/api/opencode-models',{});
  if(boot.ai.opencode)boot.ai.opencode.models=result.models;
  return result;
}
async function loadOpenCode() {
  if(cliProvider!=='opencode' || !boot.ai.opencode?.installed)return;
  const ticket=++opencodeLoad, desired=selectedCliModel()||rememberedCli('opencode').model||boot.ai.opencode.default_model;
  try {
    const result=await openCodeModels();
    if(ticket!==opencodeLoad || !$('settingsDialog').open || cliProvider!=='opencode')return;
    if(result.error)setSettingsMessage(result.error,true);
    else if(!result.models.length)setSettingsMessage('OpenCode did not return any models.',true);
    else if(desired && !result.models.some(model=>model.id===desired))setSettingsMessage('The saved OpenCode model is no longer listed. Choose a listed model.',true);
    else if(!desired && !result.models.some(model=>model.id===boot.ai.opencode.default_model))setSettingsMessage("OpenCode's default model is not listed. Choose a listed model.",true);
    fillCli(desired,'');
  } catch(error){if(ticket===opencodeLoad && $('settingsDialog').open)setSettingsMessage(error.message,true);}
}
function placeCliOptions() {
  const row=$('cliList').querySelector(`[data-provider="${cliProvider}"]`);
  $('cliList').querySelectorAll('.cli-row').forEach(r=>r.classList.toggle('selected',r===row));
  if(row)row.append(cliOptions);
}
// The model and effort last saved for a CLI (CLI default when it was never saved).
function rememberedCli(provider) {
  try{const saved=JSON.parse(boot.settings.ai_choices||'{}')[provider];return {model:String(saved?.model||''),effort:String(saved?.effort||'')};}
  catch{return {model:'',effort:''};}
}
function renderCliList(model, effort) {
  const list=$('cliList');list.after(cliOptions);list.replaceChildren();
  for(const [provider,cli] of Object.entries(boot.ai)) {
    const info=choices.cliRow(cli), row=node('div',undefined,'cli-row'), choice=node('label',undefined,'cli-choice'), head=node('span',undefined,'cli-head'), radio=node('input');
    radio.type='radio';radio.name='ai_provider';radio.value=provider;radio.checked=provider===cliProvider;
    radio.onchange=()=>{cliProvider=provider;placeCliOptions();const saved=rememberedCli(provider);fillCli(saved.model,saved.effort,true);loadAntigravity();loadOpenCode();};
    const state=node('span',info.state,'cli-state');state.dataset.tone=cli.installed?'ok':'warn';
    head.append(node('strong',info.name),' ',state);choice.append(radio,head,node('span',info.detail,'cli-detail'));
    row.dataset.provider=provider;row.append(choice);list.append(row);
  }
  placeCliOptions();fillCli(model,effort,true);
}
$('ai_model').onchange=()=>{const other=$('ai_model').value===OTHER;$('ai_model_custom').hidden=!other;if(other)$('ai_model_custom').focus();fillEffort($('ai_effort').value);};
$('ai_model_custom').onchange=()=>fillEffort($('ai_effort').value);
async function openSettings(instructions=false) {
  const dialog=$('settingsDialog');if(dialog.open)return;
  await refreshAi();
  if(dialog.open)return;
  for(const key of settingKeys)$(key).value=boot.settings[key];
  updateGeminiClipHelp();
  removing={};geminiModels=null;geminiCheck={text:'',tone:''};
  for(const p of keyedServices){$(p+'_key').value='';renderKey(p);}
  cliProvider=boot.ai[boot.settings.ai_provider]?boot.settings.ai_provider:Object.keys(boot.ai)[0];
  renderCliList(boot.settings.ai_model,boot.settings.ai_effort);fillGemini(boot.settings.gemini_model);
  setSettingsMessage('');dialog.showModal();
  const field=instructions?$('instructions'):null;
  if(field){field.scrollIntoView({block:'center'});field.focus({preventScroll:true});}
  loadGemini();loadAntigravity();loadOpenCode();
}
$('settingsButton').onclick=()=>guarded(()=>openSettings());$('instructionsButton').onclick=()=>guarded(()=>openSettings(true));
$('closeSettings').onclick=()=>$('settingsDialog').close();
$('settingsDialog').onclose=()=>{geminiLoad++;opencodeLoad++;};
// A model select with an "Other model name…" box gives the model to save; an empty Other box is an error, not a silent reset.
function chosenModel(select, custom) {
  const value=choices.modelValue($(select).value,$(custom).value);
  if($(select).value===OTHER && !value)throw Error('Enter a model name, or choose one from the list.');
  return value;
}
async function saveSettings() {
  const body=Object.fromEntries(settingKeys.map(k=>[k,$(k).value]));
  body.gemini_model=chosenModel('gemini_model','gemini_model_custom');
  if(cliProvider==='opencode' && !$('ai_model').value)throw Error("Load OpenCode's model list and choose a listed model.");
  body.ai_provider=cliProvider;body.ai_model=chosenModel('ai_model','ai_model_custom');body.ai_effort=cliHasEfforts()?$('ai_effort').value:'';
  const changed=Object.keys(body).some(k=>body[k]!==boot.settings[k]), keys=[];
  // A new key replaces the saved one; Remove key clears it. Never both in one request (the clear would drop the new key).
  for(const p of keyedServices) {
    if(typedKey(p)){body[p+'_key']=typedKey(p);keys.push({label:labels[p],action:'saved'});}
    else if(boot.settings.keys_saved[p] && removing[p]){body['clear_'+p]=true;keys.push({label:labels[p],action:'removed'});}
  }
  if(!changed && !keys.length){setSettingsMessage(choices.saveMessage(keys,false));return;}
  boot.settings=await api('/api/settings',body);
  const message=choices.saveMessage(keys,changed), newGemini=keys.some(k=>k.label===labels.gemini && k.action==='saved');
  removing={};updateHeader();notify(message);
  if(!newGemini){$('settingsDialog').close();return;}
  // A new Gemini key keeps the dialog open so Google's answer about the key shows beside it.
  for(const p of keyedServices){$(p+'_key').value='';renderKey(p);}
  fillGemini(boot.settings.gemini_model);setSettingsMessage(message);loadGemini(true);
}
$('settingsForm').onsubmit=event=>{event.preventDefault();saveSettings().catch(error=>setSettingsMessage(error.message,true));};
$('primary').onchange=()=>{if($('primary').value===$('fallback').value)$('fallback').value=services.find(p=>p!==$('primary').value);};
// Switching the AI brings back the model and effort last saved for it.
$('aiProvider').onchange=()=>guarded(async()=>{
  const provider=$('aiProvider').value,saved=rememberedCli(provider);let model=saved.model;
  if(provider==='opencode') {
    const result=await openCodeModels();
    if(result.error)throw Error(result.error);
    const models=result.models||[];
    if(model && !models.some(item=>item.id===model))throw Error("The saved OpenCode model is no longer listed. Choose a listed model in Settings.");
    if(!model)model=boot.ai.opencode.default_model;
    if(!models.some(item=>item.id===model))throw Error("OpenCode's default model is not in its current model list. Choose a listed model in Settings.");
  }
  boot.settings=await api('/api/settings',{ai_provider:provider,ai_model:model,ai_effort:saved.effort});updateHeader();renderDocument();
}).finally(()=>{$('aiProvider').value=boot.settings.ai_provider;});  // a refused switch shows the AI still in use
// The live list under the Add videos box: what will be queued, line by line (same reading of each link as the server).
const URL_FLAGS = {invalid:'Not a YouTube video link',duplicate:'Already listed'}, URL_SHOWN = 200;
function renderUrlPreview() {
  const parsed=AIVideoTranscriberUrls.parseLinks($('urls').value), shown=parsed.entries.slice(0,URL_SHOWN);
  $('urlPreview').hidden=!parsed.lines;
  $('urlSummary').textContent=`${parsed.ready} video${parsed.ready===1?'':'s'} ready`;
  $('urlWarning').hidden=!parsed.tooMany;
  $('urlWarning').textContent=parsed.tooMany?`At most ${parsed.limit} links can be added at once. Remove ${parsed.lines-parsed.limit} to continue.`:'';
  $('urlList').replaceChildren(...shown.map(entry=>{
    const item=node('li',undefined,'url-'+entry.status);
    item.append(node('span',entry.status==='invalid' && entry.line.length>200?entry.line.slice(0,200)+'…':entry.line,'url-link'));
    if(URL_FLAGS[entry.status])item.append(node('span',URL_FLAGS[entry.status],'url-flag'));
    return item;
  }));
  if(parsed.entries.length>shown.length)$('urlList').append(node('li',`… and ${parsed.entries.length-shown.length} more lines`,'url-more'));
  $('processAll').textContent=parsed.ready?`Get transcripts (${parsed.ready})`:'Get transcripts';
}
$('urls').oninput=renderUrlPreview;addEventListener('pageshow',renderUrlPreview);
const GEMINI_LOCAL_FILE_CAP=2_000_000_000;
const GEMINI_LOCAL_FILE_EXTENSIONS=new Set(['.mp4','.m4v','.mov','.webm','.avi','.mpeg','.mpg','.mpe','.mp2','.m2v','.3gp','.wmv','.flv']);
function formatLocalFileSize(size) {
  return size<1_000_000_000?`${(size/1_000_000).toFixed(1)} MB`:`${(size/1_000_000_000).toFixed(2)} GB`;
}
function localWhisperReady() {return ['ready','cpu_only'].includes(localFileReadiness?.whisper?.status);}
function localFileUserMessage(message) {
  return String(message||'').replace(/Gemini supports local files up to 2,000,000,000 bytes\./g,
    'Gemini’s 2 GB limit applies to this file.')
    .replace(/This file exceeds Gemini['’]s 2,000,000,000-byte cap/g,'This file exceeds Gemini’s 2 GB limit')
    .replace(/2,000,000,000 bytes/g,'Gemini’s 2 GB limit')
    .replace(/2,000,000,000-byte cap/g,'Gemini’s 2 GB limit');
}
function renderLocalFile() {
  const file=$('localVideoFile').files[0], route=$('localFileRoute').value;
  const extension=file?.name.toLowerCase().match(/\.[^.]+$/)?.[0]||'';
  const geminiFileSupported=!file||GEMINI_LOCAL_FILE_EXTENSIONS.has(extension);
  const whisper=localFileReadiness?.whisper||{status:'checking'}, gemini=localFileReadiness?.gemini||{status:'not_configured'};
  const whisperText=whisper.status==='ready'?`Whisper ready${whisper.gpu_name?' · '+whisper.gpu_name:''}`:
    whisper.status==='cpu_only'?'Whisper ready · CPU, slow':whisper.status==='not_installed'?`Whisper not installed · ${whisper.reason||whisper.venv_path||'venv not found'}`:
      whisper.status==='broken'?`Whisper broken · ${whisper.reason||'check failed'}`:'Checking Whisper readiness…';
  const geminiText=gemini.status==='ready'?'Gemini ready':'Gemini needs a key in Settings';
  $('localFileReadiness').textContent=`${whisperText} · ${geminiText}`;
  $('localFileName').textContent=file?`${file.name} · ${formatLocalFileSize(file.size)}`:'No file selected';
  $('localFileName').title=file?.name||'';
  const overCap=!!file&&file.size>GEMINI_LOCAL_FILE_CAP;
  const canWhisper=localWhisperReady(), canGemini=gemini.status==='ready'&&!overCap&&geminiFileSupported;
  const ready=route==='whisper'?canWhisper:route==='gemini'?canGemini:canWhisper||canGemini;
  let message=!file?'Choose a video file to see its decimal size and route availability.':
    route==='gemini'&&overCap?'This file exceeds Gemini’s 2 GB limit. Use Whisper.':
    route==='gemini'&&!geminiFileSupported?'Gemini does not support this video file type. Use Whisper.':
    route==='whisper'&&!canWhisper?whisperText:
    route==='gemini'&&!canGemini?geminiText:
    route==='whisper_then_gemini'&&overCap&&!canWhisper?`This file exceeds Gemini’s 2 GB limit and Whisper is not ready. ${whisperText}`:
    route==='whisper_then_gemini'&&overCap?'This file exceeds Gemini’s 2 GB limit. Gemini fallback will be skipped.':
    route==='whisper_then_gemini'&&!geminiFileSupported&&!canWhisper?`Gemini does not support this file type and Whisper is not ready. ${whisperText}`:
    route==='whisper_then_gemini'&&!geminiFileSupported?'Gemini does not support this file type; Whisper only, with no Gemini fallback.':
    !ready?'Nothing in this route is ready yet.':"Ready to transcribe this local video.";
  if(localFileStatusOverride)message=localFileStatusOverride;
  $('localFileStatus').textContent=message;
  $('localFileStatus').dataset.tone=localFileStatusOverride?localFileStatusTone:
    file&&(overCap||!geminiFileSupported)&&route==='whisper_then_gemini'?'warning':ready?'ok':'error';
  $('transcribeLocalFile').disabled=!file||!ready||localFileBusy;
  $('transcribeLocalFile').textContent=!localFileBusy?'Transcribe file':localFilePhase==='preflight'?'Checking upload…':
    `Uploading… ${localFileUploadPercent}%`;
  $('transcribeLocalFile').title=message;
}
async function refreshLocalFileReadiness(recheck=false) {
  $('localFileReadiness').textContent='Checking Whisper readiness…';
  try {
    const response=await fetch('/api/local-file/readiness'+(recheck?'?recheck=1':''));
    const result=await response.json();
    if(!response.ok)throw Error(result.error||'Readiness check failed.');
    localFileReadiness=result;
  } catch(error) {
    localFileReadiness={whisper:{status:'broken',reason:error.message},gemini:{status:'not_configured'}};
  }
  renderLocalFile();
}
async function postLocalFile(file,route,onProgress) {
  const send=()=>new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST','/api/local-file');
    xhr.setRequestHeader('Content-Type',file.type||'application/octet-stream');
    xhr.setRequestHeader('X-AIVideoTranscriber-Token',boot.token);
    xhr.setRequestHeader('X-AIVideoTranscriber-Filename',encodeURIComponent(file.name));
    xhr.setRequestHeader('X-AIVideoTranscriber-File-Route',route);
    xhr.upload.onprogress=event=>{
      if(event.lengthComputable&&event.total>0)onProgress(Math.min(100,Math.floor(event.loaded/event.total*100)));
    };
    xhr.onload=()=>{
      let body;
      try {body=JSON.parse(xhr.responseText);} catch {body={error:'The local server returned an unreadable upload response.'};}
      resolve({status:xhr.status,body});
    };
    xhr.onerror=()=>reject(Error('The video upload connection failed before the local server returned a response.'));
    xhr.onabort=()=>reject(Error('The video upload was canceled before the local server accepted it.'));
    xhr.send(file);
  });
  let response=await send();
  if(response.status===403) {
    boot=await api('/api/bootstrap');
    response=await send();
  }
  if(response.status<200||response.status>=300)throw Error(response.body.error||`Upload failed (${response.status})`);
  return response.body;
}
async function transcribeLocalFile() {
  const file=$('localVideoFile').files[0],route=$('localFileRoute').value;
  renderLocalFile();
  if(!file||$('transcribeLocalFile').disabled)throw Error($('localFileStatus').textContent);
  if(!boot)boot=await api('/api/bootstrap');
  localFileBusy=true;localFilePhase='preflight';localFileUploadPercent=0;
  localFileStatusOverride='';localFileStatusTone='';renderLocalFile();
  try {
    const preflight=await api('/api/local-file/preflight',{filename:file.name,size:file.size,route});
    if(preflight.warning) {
      localFileStatusOverride=localFileUserMessage(preflight.warning);
      localFileStatusTone='warning';
    }
    localFilePhase='upload';renderLocalFile();
    const result=await postLocalFile(file,route,percent=>{
      localFileUploadPercent=percent;renderLocalFile();
    });
    localFileBusy=false;localFilePhase='';localFileUploadPercent=0;
    localFileStatusOverride='';localFileStatusTone='';renderLocalFile();
    if(result.warning)notify(localFileUserMessage(result.warning),true);
    finished=null;await poll();await selectVideo(result.video_id);
  } catch(error) {
    const message=localFileUserMessage(error.message||'The local video upload failed.');
    localFileStatusOverride=message;localFileStatusTone='error';renderLocalFile();
    throw Error(message);
  } finally {localFileBusy=false;localFilePhase='';localFileUploadPercent=0;renderLocalFile();}
}
$('localVideoFile').onchange=()=>{localFileStatusOverride='';localFileStatusTone='';renderLocalFile();};
$('localFileRoute').onchange=()=>{localFileStatusOverride='';localFileStatusTone='';renderLocalFile();};
$('recheckWhisper').onclick=()=>guarded(()=>refreshLocalFileReadiness(true));
$('transcribeLocalFile').onclick=()=>guarded(transcribeLocalFile);
refreshLocalFileReadiness();
setInterval(()=>refreshLocalFileReadiness(),600_000);
function checkService(mode) {
  const configured=boot.settings.keys_configured;
  if(!(mode==='auto'?Object.values(configured).some(Boolean):configured[mode]))throw Error('That transcript service is not set up. Open Settings in the header.');
}
async function batch(compare) {
  const configured=boot.settings.keys_configured, mode=$('mode').value;
  if(compare && services.filter(p=>configured[p]).length<2)throw Error('Compare services needs at least two available services. Open Settings to set up another one.');
  if(!compare)checkService(mode);
  const text=$('urls').value, result=await api('/api/batch',{urls:text,mode,compare});
  const first=AIVideoTranscriberUrls.parseLinks(text).entries.find(e=>e.status==='ready')?.id;
  if(result.errors.length)notify(`${result.errors.map(e=>e.error).join('\n')}`,true);
  else if(!result.jobs.length && result.reused)notify(result.reused===1?'That video is already in your library. It is open below.':`Those ${result.reused} videos are already in your library.`);
  // Queued: empty the box and the list. Lines the server rejected stay (so they can be fixed), and so does anything typed meanwhile.
  if(result.jobs.length+result.reused>0 && $('urls').value===text) {
    $('urls').value=result.errors.length?AIVideoTranscriberUrls.parseLinks(text).entries.filter(e=>e.status==='invalid').map(e=>e.line).join('\n'):'';
    renderUrlPreview();
  }
  if(compare)$('checks').open=true;
  finished=null;await poll();
  // Open the first new video right away: its live progress shows in the workspace, and the transcript replaces it when done.
  if(first && result.jobs.length+result.reused>0){tab='raw';await selectVideo(first);}
}
$('processAll').onclick=()=>guarded(()=>batch(false));$('compareServices').onclick=()=>guarded(()=>batch(true));
async function retrieveCurrent() {
  const mode=$('mode').value;checkService(mode);
  const result=await api('/api/batch',{urls:current.url,mode,compare:false,refetch:true});
  if(!result.jobs.length)notify('A transcript retrieval is already running for this video.');
  finished=null;await poll();
}
async function generate() {
  const kind=tab;
  await api('/api/ai',{id:selectedId,kind});finished=null;await poll();
}
$('retrieveCurrent').onclick=()=>guarded(retrieveCurrent);
$('emptyAction').onclick=()=>guarded(async()=>{
  const action=$('emptyAction').dataset.action;
  if(action==='add'){selectTopTab('add');$('urls').focus();}
  else if(action==='library')selectTopTab('library');
  else if(action==='fetch')await retrieveCurrent();
  else if(action==='generate')await generate();
});
$('emptySecondary').onclick=()=>guarded(()=>openSettings(true));
const cancelJob=id=>guarded(async()=>{if(id){await api('/api/cancel',{job_id:id});await poll();}});
$('workCancel').onclick=()=>cancelJob($('workCancel').dataset.job);$('jobStatusCancel').onclick=()=>cancelJob($('jobStatusCancel').dataset.job);
$('workOpen').onclick=()=>guarded(()=>selectVideo($('workOpen').dataset.video));
$('workRetry').onclick=()=>guarded(()=>finished && retryJob(finished));
$('workDismiss').onclick=()=>{finished=null;renderWorkBar();};
$('search').oninput=renderLibrary;
document.querySelectorAll('[data-top-tab]').forEach(button=>{
  button.onclick=()=>selectTopTab(button.dataset.topTab);
  button.onkeydown=event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();let index=topTabNames.indexOf(topTab);
    index=event.key==='Home'?0:event.key==='End'?topTabNames.length-1:(index+(event.key==='ArrowRight'?1:topTabNames.length-1))%topTabNames.length;
    const next=$('top-tab-'+topTabNames[index]);next.click();next.focus();
  };
});
topTab=storedTopTab();renderTopWorkspace(false);
installSplitResizer();
document.querySelectorAll('[data-tab]').forEach(button=>{
  button.onclick=()=>{tab=button.dataset.tab;renderDocument();};
  button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const tabs=['raw','formatted','summary','ask'];let index=tabs.indexOf(tab);index=event.key==='Home'?0:event.key==='End'?3:(index+(event.key==='ArrowRight'?1:3))%4;$('tab-'+tabs[index]).click();$('tab-'+tabs[index]).focus();};
});
$('timestamps').onchange=renderDocument;$('wrap').onchange=renderDocument;
$('generate').onclick=()=>guarded(generate);
$('askForm').onsubmit=event=>{event.preventDefault();guarded(async()=>{const r=await api('/api/ai',{id:selectedId,kind:'ask',question:$('question').value});if(r.job_id)$('question').value='';else notify('A question is already being answered for this transcript.');finished=null;await poll();});};
$('question').onkeydown=event=>{if(event.ctrlKey && event.key==='Enter' && !$('askButton').disabled){event.preventDefault();$('askForm').requestSubmit();}};
$('copy').onclick=()=>guarded(async()=>{await navigator.clipboard.writeText(displayText());notify('Copied to clipboard.');});
// ---- Read aloud: the browser's own voices read the open view, marking each word as it is spoken ----
const readAloud=AIVideoTranscriberReadAloud;
// The box the view shows its text in, while it has text to read.
function readView() {
  if(!current)return null;
  const box=tab==='ask'?$('conversation'):$('transcript');
  return box.hidden || !box.textContent.trim() || box.querySelector('.conversation-empty')?null:{key:current.id+':'+tab,root:box};
}
const reader=window.speechSynthesis && window.SpeechSynthesisUtterance?readAloud.createReader({synth:speechSynthesis,Utterance:SpeechSynthesisUtterance,
  highlights:window.CSS?.highlights,Highlight:window.Highlight,notify,getView:readView,onChange:renderReadControls}):null;
const readSaved=key=>{try{return localStorage.getItem(key)||'';}catch{return '';}};
const readSave=(key,value)=>{try{localStorage.setItem(key,value);}catch{}};
function renderReadControls() {
  const status=reader?.status||'idle';
  $('readAloud').hidden=!reader;$('readAloud').disabled=status==='idle' && !readView();
  $('readAloud').textContent=status==='idle'?'Read aloud':'Stop';$('readAloud').classList.toggle('reading',status!=='idle');
  $('readPause').hidden=$('readRate').hidden=$('readVoice').hidden=status==='idle';
  $('readPause').textContent=status==='paused'?'Resume':'Pause';
}
// The voices for the transcript's language; the one picked last time is kept.
function fillVoices() {
  if(!reader)return;
  const all=speechSynthesis.getVoices(), lang=source()?.language||'en', list=readAloud.voicesFor(all,lang);
  const chosen=readAloud.pickVoice(all,lang,readSaved('td-read-voice'));
  $('readVoice').replaceChildren(...list.map(voice=>{const o=node('option',readAloud.voiceLabel(voice));o.value=voice.name;return o;}));
  if(chosen)$('readVoice').value=chosen.name;
  return chosen;
}
if(reader) {
  const rate=readSaved('td-read-rate');if([...$('readRate').options].some(o=>o.value===rate))$('readRate').value=rate;
  reader.setRate($('readRate').value);
  speechSynthesis.addEventListener?.('voiceschanged',()=>{if(reader.status==='idle')fillVoices();});
  $('readAloud').onclick=()=>{if(reader.status==='idle')reader.setVoice(fillVoices());reader.toggle();};
  $('readPause').onclick=()=>reader.status==='paused'?reader.resume():reader.pause();
  $('readRate').onchange=()=>{readSave('td-read-rate',$('readRate').value);reader.setRate($('readRate').value);};
  $('readVoice').onchange=()=>{readSave('td-read-voice',$('readVoice').value);reader.setVoice(speechSynthesis.getVoices().find(v=>v.name===$('readVoice').value));};
  addEventListener('pagehide',()=>reader.stop());
}
$('download').onclick=()=>{if(selectedId)location.href=`/api/export?id=${encodeURIComponent(selectedId)}&tab=${tab}&timestamps=${$('timestamps').checked?1:0}`;};
$('downloadJson').onclick=()=>{if(selectedId)location.href=`/api/export?id=${encodeURIComponent(selectedId)}&tab=json`;};
$('exportAll').onclick=()=>{location.href='/api/export-all';};
$('sourceProvider').onchange=()=>guarded(async()=>{await api('/api/source',{id:selectedId,provider:$('sourceProvider').value});await selectVideo(selectedId);await poll();});
$('importButton').onclick=()=>$('importDialog').showModal();$('closeImport').onclick=()=>$('importDialog').close();
$('importForm').onsubmit=event=>{event.preventDefault();guarded(async()=>{const result=await api('/api/import',{title:$('importTitle').value,url:$('importUrl').value,text:$('importText').value});$('importDialog').close();$('importForm').reset();await selectVideo(result.id);await poll();});};
$('sampleButton').onclick=()=>guarded(async()=>{
  const result=await api('/api/import',{title:'Sample · Planning a community garden',is_sample:true,text:'[00:00:00] this is a synthetic example made to test transcript formatting and summaries it is not from a youtube video\n[00:00:12] our community garden has twelve raised beds six are for vegetables four for flowers and two are for herbs\n[00:00:25] volunteers will meet on saturday at nine in the morning bring gloves and water the library is lending us tools\n[00:00:40] we have a budget of six hundred dollars about two hundred is for soil the rest has not been allocated yet\n[00:00:55] we think the garden could be ready by the end of april but the date depends on weather and volunteer availability'});
  await selectVideo(result.id);await poll();notify('Sample loaded. Choose Formatted, Summary, or Ask AI to try your AI CLI.');
});
async function refreshDashboard() {
  if(polling)return;
  try {
    if(!boot)boot=await api('/api/bootstrap');
    await poll();
  } catch(error) {
    connection='offline';renderHeaderActivity();
    $('aiStatus').textContent='Server unavailable — reconnecting…';
  }
}
refreshDashboard();
setInterval(refreshDashboard,1600);
// The clocks in the working bar and the view tick every second between polls.
setInterval(()=>{renderHeaderActivity();renderWorkBar();if(current)renderJobStatus();},1000);
