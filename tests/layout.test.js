'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const read=name=>fs.readFileSync(path.join(__dirname,'..',name),'utf8');
const page=read('static/index.html'), script=read('static/app.js'), css=read('static/style.css');
function splitTestApi(storage,width=1440) {
  const start=script.indexOf('const SPLIT_STORAGE_KEY'), end=script.indexOf('const $ =',start);
  assert.ok(start>=0 && end>start,'split helpers must precede DOM-bound app code');
  const context={window:{innerWidth:width},localStorage:storage,Number,Math};
  vm.runInNewContext(`${script.slice(start,end)}\nglobalThis.splitTestApi={splitTotalForWidth,clampSplitHeight,splitBottomMax,storedSplitChoice,persistSplitHeight,resizeSplitHeight,get chosenHeight(){return chosenSplitHeight;},get displayHeight(){return splitHeight;}};`,context);
  return context.splitTestApi;
}
function memoryStorage(initial={}) {
  const values=new Map(Object.entries(initial));
  return {values,getItem(key){return values.has(key)?values.get(key):null;},setItem(key,value){values.set(key,value);}};
}

test('top tabs, header status and resize separator have the requested order and semantics',()=>{
  assert.deepEqual([...page.matchAll(/data-top-tab="([^"]+)"/g)].map(match=>match[1]),['add','library','activity']);
  assert.match(page,/id="top-panel-add"[^>]*aria-labelledby="top-tab-add"(?![^>]* hidden)/);
  assert.match(page,/id="top-panel-library"[^>]* hidden/);
  assert.match(page,/id="top-panel-activity"[^>]* hidden/);
  assert.doesNotMatch(page,/<h2>Add videos<\/h2>|<h2>Library<\/h2>|<h2>Activity<\/h2>|>BATCH</);
  const panels=page.indexOf('id="topPanels"'),separator=page.indexOf('id="splitResizer"'),workspace=page.indexOf('id="workspace"');
  assert.ok(panels<separator && separator<workspace);
  assert.match(page,/id="splitResizer"[^>]*role="separator"[^>]*aria-orientation="horizontal"[^>]*tabindex="0"/);
  const headerStart=page.indexOf('<div class="header-controls">'),headerEnd=page.indexOf('id="workBar"',headerStart);
  const headerControls=page.slice(headerStart,headerEnd);
  assert.ok(headerControls.indexOf('class="top-actions"')<headerControls.indexOf('id="headerActivity"'));
  assert.match(headerControls,/<div class="top-actions">[\s\S]*?<\/div>\s*<span id="headerActivity"[\s\S]*<\/span>\s*<\/div>\s*<\/header>/);
  assert.match(script,/const topTabNames = \['add','library','activity'\]/);
  assert.match(script,/separator\.setPointerCapture\(event\.pointerId\)/);
  assert.match(script,/splitHeight\+\(event\.key==='ArrowDown'\?20:-20\)/);
  assert.match(css,/@media\(max-width:1000px\)\{\.header-activity\{display:none\}/);
  assert.match(css,/\.topbar\{[^}]*height:44px/);
  assert.match(css,/body\{[^}]*font:14px/);
  assert.match(css,/\.header-controls\{[^}]*justify-content:flex-end/);
  assert.match(css,/\.split-resizer\{[^}]*height:8px[^}]*cursor:row-resize[^}]*touch-action:none/);
  assert.match(css,/\.split-resizer::before\{content:"";width:40px;height:2px/);
  assert.match(css,/\.top-panels\{[^}]*min-height:var\(--split-height\)/);
  assert.doesNotMatch(css,/\.workspace\{[^}]*max-height:/);
});

test('local video panel streams a raw file and exposes only the three supported routes',()=>{
  assert.match(page,/Video file on this PC/);
  assert.ok(page.indexOf('class="local-file-box"')<page.indexOf('class="batch-controls"'));
  assert.match(page,/class="local-file-picker"[^>]*>Choose file/);
  assert.match(page,/id="localVideoFile" type="file" accept="video\/\*[^\"]*\.mp4[^\"]*\.mov[^\"]*\.webm/);
  assert.match(page,/option value="whisper_then_gemini" selected>Whisper, then Gemini/);
  assert.match(page,/option value="whisper">Whisper only/);
  assert.match(page,/option value="gemini">Gemini only/);
  const routeStart=page.indexOf('<select id="localFileRoute"'),routeEnd=page.indexOf('</select>',routeStart);
  assert.deepEqual([...page.slice(routeStart,routeEnd).matchAll(/<option value="([^"]+)"/g)].map(match=>match[1]),
    ['whisper_then_gemini','whisper','gemini']);
  assert.match(page,/id="recheckWhisper"[^>]*>Recheck/);
  assert.match(page,/for="localFileRoute">using<select/);
  assert.doesNotMatch(page,/for="localFileRoute">Route/);
  assert.match(script,/fetch\('\/api\/local-file\/readiness/);
  assert.match(script,/api\('\/api\/local-file\/preflight',\{filename:file\.name,size:file\.size,route\}\)/);
  assert.match(script,/xhr\.setRequestHeader\('X-AIVideoTranscriber-Token',boot\.token\)/);
  assert.match(script,/xhr\.setRequestHeader\('X-AIVideoTranscriber-File-Route',route\)/);
  assert.match(script,/xhr\.send\(file\)/);
  assert.doesNotMatch(script,/fetch\('\/api\/local-file'/);
  assert.match(script,/GEMINI_LOCAL_FILE_CAP=2_000_000_000/);
  assert.match(script,/GEMINI_LOCAL_FILE_EXTENSIONS=new Set/);
  assert.match(script,/Gemini does not support this file type/);
  assert.match(script,/no Gemini fallback/);
  assert.doesNotMatch(script,/file\.arrayBuffer\(\)|new FormData\(/);
  assert.match(css,/\.batch-panel\{[^}]*overflow:visible/);
  assert.match(css,/\.local-file-controls\{display:grid;grid-template-columns:minmax\(220px,auto\) minmax\(80px,1fr\) minmax\(180px,auto\) auto/);
  assert.match(css,/\.local-file-box>p\{margin:3px 0 0/);
});

test('local video preflight displays refusal reasons and Gemini wording',()=>{
  const render=script.slice(script.indexOf('function renderLocalFile()'),script.indexOf('async function refreshLocalFileReadiness'));
  const userMessage=script.slice(script.indexOf('function localFileUserMessage'),script.indexOf('function renderLocalFile()'));
  const context={};
  vm.runInNewContext(`${userMessage}\nglobalThis.formatLocalFileMessage=localFileUserMessage;`,context);
  assert.ok(script.indexOf("api('/api/local-file/preflight'")<script.indexOf('const result=await postLocalFile(file,route'));
  assert.match(script,/localFileStatusOverride=message;localFileStatusTone='error'/);
  assert.match(userMessage,/Gemini’s 2 GB limit/);
  assert.equal(context.formatLocalFileMessage('Gemini supports local files up to 2,000,000,000 bytes. Use Whisper.'),
    'Gemini’s 2 GB limit applies to this file. Use Whisper.');
  assert.equal(context.formatLocalFileMessage('Gemini fallback skipped because this file exceeds 2,000,000,000 bytes; use Whisper.'),
    'Gemini fallback skipped because this file exceeds Gemini’s 2 GB limit; use Whisper.');
  assert.match(script,/localFileUserMessage\(attempt\.message\)/);
  assert.doesNotMatch(render,/2,000,000,000|byte cap/);
});

test('local video size labels use decimal MB below one GB and decimal GB at or above it',()=>{
  const start=script.indexOf('function formatLocalFileSize('),end=script.indexOf('function localWhisperReady()',start);
  assert.ok(start>=0&&end>start,'the local video size formatter must stay independently testable');
  const context={};
  vm.runInNewContext(`${script.slice(start,end)}\nglobalThis.formatLocalFileSize=formatLocalFileSize;`,context);
  assert.equal(context.formatLocalFileSize(1_067_990),'1.1 MB');
  assert.equal(context.formatLocalFileSize(999_999_999),'1000.0 MB');
  assert.equal(context.formatLocalFileSize(1_000_000_000),'1.00 GB');
  assert.equal(context.formatLocalFileSize(2_000_000_001),'2.00 GB');
  assert.match(script,/const GEMINI_LOCAL_FILE_CAP=2_000_000_000/);
});

test('raw local video upload reports XHR progress and clears busy state at completion',()=>{
  const transcribe=script.slice(script.indexOf('async function transcribeLocalFile'),script.indexOf("$('localVideoFile').onchange"));
  assert.match(script,/xhr\.upload\.onprogress=event=>/);
  assert.match(script,/`Uploading… \$\{localFileUploadPercent\}%`/);
  assert.match(script,/localFileUploadPercent=percent;renderLocalFile\(\)/);
  assert.match(script,/localFileBusy=false;localFilePhase='';localFileUploadPercent=0/);
  assert.match(transcribe,/\}\s*finally\s*\{localFileBusy=false;localFilePhase='';localFileUploadPercent=0;renderLocalFile\(\);\}/);
  assert.match(script,/xhr\.onerror=\(\)=>reject\(Error\('The video upload connection failed/);
});

test('raw upload sends the selected File through XHR and surfaces its progress and server refusal',async()=>{
  const start=script.indexOf('async function postLocalFile'),end=script.indexOf('async function transcribeLocalFile',start);
  assert.ok(start>=0&&end>start,'the raw upload helper must stay independently testable');
  const file={name:'fixture.mp4',size:100,type:'video/mp4'},progress=[];
  class FakeXHR {
    constructor(){this.upload={};this.headers={};FakeXHR.instances.push(this);}
    open(method,url){this.method=method;this.url=url;}
    setRequestHeader(name,value){this.headers[name]=value;}
    send(body){
      this.file=body;
      this.upload.onprogress({lengthComputable:true,loaded:45,total:100});
      this.status=FakeXHR.status;
      this.responseText=JSON.stringify(FakeXHR.body);
      this.onload();
    }
  }
  FakeXHR.instances=[];FakeXHR.status=200;FakeXHR.body={job_id:'fixture-job'};
  const context={XMLHttpRequest:FakeXHR,boot:{token:'fixture-page-token'},api:async()=>({token:'refreshed'})};
  vm.runInNewContext(`${script.slice(start,end)}\nglobalThis.uploadLocalFile=postLocalFile;`,context);
  const result=await context.uploadLocalFile(file,'whisper',value=>progress.push(value));
  assert.equal(result.job_id,'fixture-job');
  assert.deepEqual(progress,[45]);
  const success=FakeXHR.instances.at(-1);
  assert.equal(success.method,'POST');
  assert.equal(success.url,'/api/local-file');
  assert.equal(success.file,file);
  assert.equal(success.headers['X-AIVideoTranscriber-Token'],'fixture-page-token');
  assert.equal(success.headers['X-AIVideoTranscriber-File-Route'],'whisper');

  FakeXHR.status=400;FakeXHR.body={error:'There is not enough free disk space.'};
  await assert.rejects(context.uploadLocalFile(file,'whisper',()=>{}),/not enough free disk space/);
  assert.equal(FakeXHR.instances.at(-1).file,file);
});

test('split height clamps and top plus bottom math follow the width breakpoint',()=>{
  const tools=splitTestApi(memoryStorage());
  assert.equal(tools.splitTotalForWidth(1440),630);
  assert.equal(tools.splitTotalForWidth(900),630);
  assert.equal(tools.splitTotalForWidth(899),518);
  assert.equal(tools.clampSplitHeight(0,630),90);
  assert.equal(tools.clampSplitHeight(900,630),510);
  assert.equal(tools.clampSplitHeight(900,518),398);
  assert.equal(tools.splitBottomMax(182,1440),448);
  assert.equal(tools.splitBottomMax(510,1440),120);
  assert.equal(tools.splitBottomMax(398,899),120);
});

test('split height persistence handles saved, missing, out-of-range and throwing storage',()=>{
  const saved=memoryStorage({'aivideotranscriber.split':'245'}),savedTools=splitTestApi(saved);
  assert.equal(savedTools.displayHeight,245);
  assert.equal(savedTools.chosenHeight,245);
  assert.equal(savedTools.storedSplitChoice(),245);
  savedTools.persistSplitHeight(265);
  assert.equal(saved.values.get('aivideotranscriber.split'),'265');

  const missingTools=splitTestApi(memoryStorage());
  assert.equal(missingTools.displayHeight,182);
  assert.equal(missingTools.storedSplitChoice(),182);

  const highStorage=memoryStorage({'aivideotranscriber.split':'900'}),highTools=splitTestApi(highStorage);
  assert.equal(highTools.displayHeight,510);
  assert.equal(highTools.chosenHeight,900);
  assert.equal(splitTestApi(memoryStorage({'aivideotranscriber.split':'900'}),899).displayHeight,398);
  const lowStorage=memoryStorage({'aivideotranscriber.split':'-10'}),lowTools=splitTestApi(lowStorage);
  assert.equal(lowTools.displayHeight,90);
  assert.equal(lowStorage.values.get('aivideotranscriber.split'),'-10');

  const throwing={getItem(){throw Error('storage blocked');},setItem(){throw Error('storage blocked');}};
  const throwingTools=splitTestApi(throwing);
  assert.equal(throwingTools.displayHeight,182);
  assert.equal(throwingTools.storedSplitChoice(),182);
  assert.doesNotThrow(()=>throwingTools.persistSplitHeight(200));
});

test('split choice survives narrow and wide resizes and a reload without storage writes',()=>{
  const storage=memoryStorage({'aivideotranscriber.split':'450'}),wide=splitTestApi(storage,1440);
  assert.equal(wide.displayHeight,450);
  assert.equal(storage.values.get('aivideotranscriber.split'),'450');

  assert.equal(wide.resizeSplitHeight(860),398);
  assert.equal(wide.displayHeight,398);
  assert.equal(storage.values.get('aivideotranscriber.split'),'450');

  assert.equal(wide.resizeSplitHeight(1440),450);
  assert.equal(wide.displayHeight,450);
  assert.equal(storage.values.get('aivideotranscriber.split'),'450');

  const reloaded=splitTestApi(storage,1440);
  assert.equal(reloaded.displayHeight,450);
  assert.equal(storage.values.get('aivideotranscriber.split'),'450');
});

test('selected top tab persists and every click keeps its panel visible',()=>{
  const storedTab=script.match(/function storedTopTab\(\) \{[\s\S]*?\n\}/)?.[0]||'';
  const render=script.match(/function renderTopWorkspace\(remember=true\) \{[\s\S]*?\n\}/)?.[0]||'';
  const select=script.match(/function selectTopTab\(name\) \{[\s\S]*?\n\}/)?.[0]||'';
  assert.match(storedTab,/try[\s\S]*localStorage\.getItem\(topTabKey\)[\s\S]*catch/);
  assert.match(render,/hidden=item!==topTab/);
  assert.match(render,/try\{localStorage\.setItem\(topTabKey,topTab\);\}catch/);
  assert.match(select,/topTab=topTabNames\.includes\(name\)\?name:'add'/);
  assert.doesNotMatch(script,/topPanelOpen|topPanelOpenKey|storedTopPanelOpen|folded/);
  assert.doesNotMatch(css,/\.top-panels\.folded/);
});

test('a working bar under the header shows live progress on every tab and stays on screen',()=>{
  const sticky=page.slice(page.indexOf('class="sticky-top"'),page.indexOf('<main'));
  assert.ok(sticky.includes('class="topbar"') && sticky.includes('id="workBar"'));
  for(const id of ['workAction','workTitle','workDetail','workTrack','workOpen','workCancel','workRetry','workDismiss'])assert.ok(sticky.includes(`id="${id}"`),id);
  assert.match(css,/\.sticky-top\{position:sticky;top:0/);
  // overflow-x:hidden on html/body would make body the scroll box and stop the header from sticking.
  assert.match(css,/html\{[^}]*overflow-x:clip/);
  assert.match(css,/body\{[^}]*min-height:100vh[^}]*overflow-x:clip/);
  assert.doesNotMatch(css,/(html|body)\{[^}]*overflow-x:hidden/);
  assert.match(script,/activity\.describeWork\(state\)/);
  assert.match(script,/setInterval\(\(\)=>\{renderHeaderActivity\(\);renderWorkBar\(\);if\(current\)renderJobStatus\(\);\},1000\)/);
  assert.doesNotMatch(script,/jobs queued/);  // the old lone "queued" toast is gone; the bar shows the work
});

test('transcript content boxes use the split bottom cap and keep their minimum height and overflow behavior',()=>{
  assert.match(css,/\.layout\{[^}]*flex:1 1 auto[^}]*overflow:visible/);
  assert.match(css,/\.workspace\{[^}]*overflow:visible/);
  assert.match(css,/\.batch-panel\{height:auto;min-height:var\(--split-height\);max-height:none;overflow:visible/);
  assert.match(css,/\.terminal-content,\.conversation\{flex:0 1 auto;height:auto;min-height:120px;max-height:var\(--split-bottom-max\);overflow:auto\}/);
  assert.doesNotMatch(css,/\.terminal-content,\.conversation\{[^}]*max-height:(448|336)px/);
  // Reading text sits in a centered column on wide windows.
  assert.match(css,/\.terminal-content\{padding:10px max\(14px,calc\(50% - 75ch\)\)/);
  assert.match(css,/\.terminal-content \.md,\.conversation \.md\{max-width:120ch;margin-inline:auto\}/);
  // The link check list is part of the Add videos panel, not a popup over the page.
  assert.doesNotMatch(css,/\.url-preview\{[^}]*position:absolute/);
  assert.match(css,/\.empty\{flex:0 0 auto;min-height:240px/);
  assert.match(css,/\.library-section\{height:var\(--split-height\);max-height:none;display:flex;flex-direction:column/);
  assert.match(css,/\.queue-section\{height:var\(--split-height\);max-height:none;display:flex;flex-direction:column/);
  assert.match(css,/\.library\{[^}]*overflow:auto/);
  assert.match(css,/\.activity\{[^}]*overflow:auto/);
  assert.doesNotMatch(css,/\.top-panels\.folded|\.checks\{[^}]*overflow:auto|\.url-list\{[^}]*overflow:auto|\.batch-panel\{[^}]*overflow:auto/);
  assert.match(css,/@media\(max-width:899px\)\{[\s\S]*\.batch-grid\{height:auto;grid-template-columns:1fr/);
});

test('generate, job status and the Ask box sit at the top of the view, and drafts stream into the main box',()=>{
  const card=page.slice(page.indexOf('<section class="terminal-shell"'),page.indexOf('<details id="checks"'));
  const at=id=>card.indexOf(`id="${id}"`);
  for(const id of ['jobStatus','askForm','empty','emptyAction'])assert.ok(at(id)>0 && at(id)<at('transcript'),id);
  assert.ok(at('askForm')<at('conversation'));
  assert.ok(at('draftLabel')<at('transcript'));
  assert.doesNotMatch(page,/id="generateBar"|id="draft"|id="draftText"|workspace-footer/);
  assert.match(script,/label:formatted\?'Generate formatted version':'Generate summary'/);
  assert.match(script,/content\.append\(markdownBox\(draft\?job\.partial:result\.text\)\)/);
  assert.match(script,/\[\.\.\.current\.conversation\]\.reverse\(\)/);
});

test('library rows keep one-line titles, show source, words and saved time, and can be deleted',()=>{
  const name=css.match(/\.video-row \.video-name\{([^}]*)\}/)?.[1]||'';
  const detail=script.match(/let info=video\.has_transcript\?`([^`]+)`/)?.[1]||'';
  assert.match(name,/text-overflow:ellipsis/);
  assert.match(name,/white-space:nowrap/);
  assert.match(detail,/sourceLabel/);
  assert.match(detail,/video\.words/);
  assert.match(detail,/saved \$\{whenLabel\(video\.saved\|\|video\.updated\)\}/);  // the transcript's own saved time, as on the card
  assert.match(script,/node\('button','Delete','video-delete'\)/);
  assert.match(script,/if\(!confirm\(`Delete/);
  assert.match(script,/api\('\/api\/delete',\{id:video\.id\}\)/);
});

test('card identity: yellow title, short credit line with detail in the tooltip, quiet provider link',()=>{
  const toolbar=page.slice(page.indexOf('class="transcript-toolbar"'),page.indexOf('<section class="terminal-shell"'));
  for(const label of ['Raw','Formatted','Summary','Ask AI','retrieveCurrent','copy','download','timestamps','wrap','aiProvider','instructionsButton','generate'])assert.ok(toolbar.includes(label),label);
  assert.match(page,/id="tab-raw"[^>]*title="Original retrieved text\. Formatting and corrections are saved separately\."/);
  const card=page.slice(page.indexOf('<section class="terminal-shell"'),page.indexOf('<details id="checks"'));
  assert.ok(card.indexOf('YouTube Title:')<card.indexOf('id="transcript"'));
  assert.ok(card.indexOf('Transcript by:')<card.indexOf('id="transcript"'));
  assert.ok(card.includes('id="openVideo"'));
  assert.match(card,/id="viewLabel" hidden/);
  assert.match(css,/#videoTitle\{[^}]*color:var\(--accent\)/);
  assert.match(script,/choices\.geminiPrimaryModel\(s\)/);
  assert.doesNotMatch(script,/clipModels\?' · '\+clipModels/);
  assert.match(css,/\.checks summary\{[^}]*color:var\(--blue\)[^}]*font-size:12px/);
  assert.match(css,/\.terminal-content\{[^}]*font:13px\/1\.5 var\(--mono\)/);
});

test('services dialog and both settings columns are horizontally contained',()=>{
  assert.match(css,/dialog\{[^}]*max-width:calc\(100vw - 24px\)[^}]*overflow-x:hidden/);
  assert.match(css,/#settingsDialog\{width:min\(1120px,calc\(100vw - 24px\)\)\}/);
  assert.match(css,/dialog form,dialog section,dialog div\{min-width:0;max-width:100%\}/);
  assert.match(css,/\.settings-grid\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1\.05fr\)/);
  assert.match(css,/\.settings-grid>\*\{min-width:0;max-width:100%;overflow-wrap:anywhere\}/);
  assert.match(css,/\.settings-grid input,\.settings-grid select,\.settings-grid textarea\{width:100%;max-width:100%\}/);
  assert.match(css,/#instructionsSection\{overflow:hidden\}/);
  assert.match(css,/#settingsDialog \.dialog-footer\{position:sticky;bottom:-16px/);  // Save stays in view
  const settings=page.slice(page.indexOf('id="settingsDialog"'),page.indexOf('id="importDialog"'));
  assert.ok(settings.indexOf('class="settings-column"')<settings.indexOf('id="cliHeading"') && settings.indexOf('id="cliHeading"')<settings.indexOf('id="instructionsSection"'));
  assert.match(css,/\.settings-grid \.key-row input,\.settings-grid \.key-row select\{[^}]*width:0[^}]*min-width:0/);
});
