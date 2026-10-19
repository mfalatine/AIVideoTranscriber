const {test}=require('node:test');
const assert=require('node:assert/strict');
const {describeActivity}=require('../static/activity.js');
const now=Date.parse('2026-09-15T12:00:42Z');
const running={id:'job',video_id:'video',kind:'fetch',status:'running',started:'2026-09-15T12:00:00Z',created:'2026-09-15T12:00:00Z',active_provider:'supadata',batch_index:3,batch_total:8};

test('disconnected state never reports stale work as current',()=>{
  const view=describeActivity({jobs:[running]},'offline',now);
  assert.equal(view.tone,'error');assert.match(view.text,/Connection lost/);
  assert.doesNotMatch(view.text,/Retrieving/);
});
test('ready counts saved transcripts rather than failed video entries',()=>{
  assert.equal(describeActivity({videos:[{has_transcript:true},{has_transcript:false}]},'connected').text,'Ready · 1 saved transcript');
});
test('retrieval shows actual batch position, active service and elapsed time',()=>{
  assert.equal(describeActivity({jobs:[running]},'connected',now).text,'Retrieving 3 of 8 · Supadata · 00:42');
});
test('Gemini is named while it transcribes',()=>{
  const job={...running,active_provider:'gemini'};
  assert.equal(describeActivity({jobs:[job]},'connected',now).text,'Retrieving 3 of 8 · Gemini · 00:42');
});
test('fallback and provider comparison are identified',()=>{
  const view=describeActivity({jobs:[{...running,compare:true,active_provider:'fetchtranscript'}]},'connected',now);
  assert.equal(view.text,'Comparing 3 of 8 · FetchTranscript · 00:42');
});
test('AI progress uses completed source parts and queued work',()=>{
  const job={...running,kind:'formatted',active_provider:'codex',parts_done:2,parts_total:4};
  const view=describeActivity({jobs:[{status:'queued'},job]},'connected',now);
  assert.equal(view.text,'Formatting · 2/4 parts done · Codex · 00:42 · 1 queued');
});
test('queued state is distinct from ready',()=>{
  assert.equal(describeActivity({jobs:[{status:'queued'},{status:'queued'}]},'connected').text,'Queued · 2 tasks');
});
test('latest successful retry clears old failure without hiding other problems',()=>{
  const failure={video_id:'a',kind:'fetch',status:'failed',message:'Missing key',created:'2026-09-15T12:00:00Z'};
  const success={...failure,status:'completed',created:'2026-09-15T12:01:00Z'};
  assert.match(describeActivity({jobs:[failure,success]},'connected').text,/^Ready/);
  const view=describeActivity({jobs:[success,failure,{...failure,video_id:'b'}]},'connected');
  assert.equal(view.text,'Needs attention · 1 task');assert.equal(view.title,'Missing key');
});
test('old jobs without batch metadata still show useful activity',()=>{
  assert.equal(describeActivity({jobs:[{kind:'summary',status:'running',active_provider:'claude'}]},'connected',now).text,'Summarizing · Claude');
});
test('a running Antigravity CLI call is named, not shown as its lowercase id',()=>{
  assert.equal(describeActivity({jobs:[{kind:'formatted',status:'running',active_provider:'antigravity'}]},'connected',now).text,'Formatting · Antigravity');
});
const {jobProgress,describeJob,describeWork,videoTitle}=require('../static/activity.js');
test('Gemini part messages give part, total, clip range and finished share',()=>{
  const job={status:'running',message:'Gemini · gemini-3.8-flash · transcribing 20:00-40:00 (part 2 of 3)'};
  assert.deepEqual(jobProgress(job),{part:2,total:3,range:'20:00–40:00',percent:33,label:'part 2 of 3 (20:00–40:00)',timingLabel:''});
  const unknown=jobProgress({status:'running',message:'Gemini · gemini-3.8-flash · transcribing 20:00-40:00 (part 2)'});
  assert.equal(unknown.label,'part 2 (20:00–40:00)');assert.equal(unknown.percent,null);
});
test('AI parts come from the message, then from finished parts, and combining is near the end',()=>{
  assert.equal(jobProgress({status:'running',message:'Codex · formatted · part 1/3'}).label,'part 1 of 3');
  assert.equal(jobProgress({status:'running',message:'Starting',parts_done:2,parts_total:4}).label,'part 3 of 4');
  const combining=jobProgress({status:'running',message:'Codex · combining evidence from all 3 parts'});
  assert.equal(combining.percent,90);assert.equal(combining.label,'combining all parts');
  assert.equal(jobProgress({status:'running',message:'Requesting Gemini'}).percent,null);
});
test('a job is described with its video title, never just the id when a title exists',()=>{
  const videos=[{id:'video',title:'A real title'}];
  const view=describeJob({...running,active_provider:'gemini',message:'Gemini · m · transcribing 00:00-20:00 (part 1 of 2)'},videos,now);
  assert.equal(view.action,'Getting transcript');assert.equal(view.title,'A real title');
  assert.equal(view.detail,'Gemini · part 1 of 2 (00:00–20:00) · 00:42');
  assert.equal(videoTitle('other',videos),'YouTube video other');
  assert.equal(describeJob({id:'q',video_id:'video',kind:'summary',status:'queued'},videos,now).detail,'waiting to start');
});
test('local file activity uses the stored Whisper seconds for progress and keeps its label',()=>{
  const videos=[{id:'file-123',title:'speech',local_file:{name:'speech.mp4'}}];
  const job={id:'file-job',video_id:'file-123',kind:'file',status:'running',started:'2026-09-15T12:00:00Z',
    active_provider:'whisper',message:'Whisper · 00:23 of 01:10',file_seconds_done:23,file_seconds_total:70};
  const view=describeJob(job,videos,now);
  assert.equal(view.action,'Transcribing file');
  assert.equal(view.title,'speech.mp4');
  assert.equal(videoTitle('file-123',videos),'speech.mp4');
  assert.equal(jobProgress(job).percent,33);
  assert.equal(jobProgress(job).label,'00:23 of 01:10');
  assert.equal(jobProgress({...job,file_seconds_done:70}).percent,99);
  assert.equal(jobProgress({...job,status:'completed',file_seconds_done:70}).percent,100);
});
const fixedNow=Date.parse('2026-09-15T12:00:30Z');
let timerJobId=0;
function timedJob(overrides={}) {
  return {id:'timed-'+(++timerJobId),status:'running',message:'Working',step_index:1,steps_total:1,
    step_started:new Date(fixedNow-5000).toISOString(),step_expected_seconds:10,step_size:1,
    expected_remaining_seconds:0,...overrides};
}
test('history estimate interpolates within one step and gives a seconds countdown',()=>{
  const progress=jobProgress(timedJob(),fixedNow);
  assert.equal(progress.percent,50);
  assert.equal(progress.timingLabel,'about 5 s left');
});
test('an unknown-total Gemini step keeps the sliding bar without an estimate or label',()=>{
  const job=timedJob({kind:'fetch',active_provider:'gemini',
    message:'Gemini · gemini-3.8-flash · transcribing 20:00-40:00 (part 2)',steps_total:null,step_size:null});
  const first=jobProgress(job,fixedNow);
  const later=jobProgress(job,fixedNow+3000);
  assert.equal(first.percent,null);
  assert.equal(first.timingLabel,'');
  assert.equal(first.label,'part 2 (20:00–40:00)');
  assert.equal(later.percent,null);
  assert.equal(later.timingLabel,'');
});
test('a multi-step overrun holds at its boundary and names the delay',()=>{
  const progress=jobProgress(timedJob({steps_total:3,step_index:1,step_expected_seconds:60,
    step_started:new Date(fixedNow-61000).toISOString()}),fixedNow);
  assert.equal(progress.percent,33);
  assert.equal(progress.timingLabel,'taking longer than usual');
});
test('three-clip progress overruns only complete the current clip and reach 95 late on the last clip',()=>{
  const id='three-clip-overrun';
  const clip=(step_index,started,extra={})=>timedJob({id,step_index,steps_total:3,
    step_expected_seconds:step_index===3?30:60,
    step_size:step_index===3?30:60,step_started:new Date(started).toISOString(),...extra});
  assert.equal(jobProgress(clip(1,fixedNow-30000),fixedNow).percent,17);
  const firstLate=jobProgress(clip(1,fixedNow-30000),fixedNow+31000);
  assert.equal(firstLate.percent,33);
  assert.equal(firstLate.timingLabel,'taking longer than usual');
  const secondStart=fixedNow+32000;
  assert.equal(jobProgress(clip(2,secondStart),secondStart).percent,33);
  assert.equal(jobProgress(clip(2,secondStart),secondStart+30000).percent,50);
  const secondLate=jobProgress(clip(2,secondStart),secondStart+61000);
  assert.equal(secondLate.percent,67);
  assert.equal(secondLate.timingLabel,'taking longer than usual');
  const thirdStart=secondStart+62000;
  assert.equal(jobProgress(clip(3,thirdStart),thirdStart+15000).percent,83);
  assert.equal(jobProgress(clip(3,thirdStart),thirdStart+61000).percent,95);
});
test('multiple steps combine completed count with the current step fraction',()=>{
  const progress=jobProgress(timedJob({step_index:2,steps_total:4}),fixedNow);
  assert.equal(progress.percent,38);
});
test('an unestimated step keeps the existing sliding bar and says this is the first run',()=>{
  const progress=jobProgress(timedJob({message:'Requesting Supadata',steps_total:2,step_expected_seconds:null}),fixedNow);
  assert.equal(progress.percent,null);
  assert.equal(progress.timingLabel,'timing this first run');
});
test('an unestimated one-part AI job has an indeterminate bar and first-run label',()=>{
  const job=timedJob({kind:'formatted',active_provider:'codex',message:'Codex · formatted · part 1/1',
    step_expected_seconds:null,step_size:6877});
  const progress=jobProgress(job,fixedNow);
  assert.equal(progress.percent,null);
  assert.equal(progress.timingLabel,'timing this first run');
  assert.match(describeJob(job,[],fixedNow).detail,/timing this first run/);
  assert.equal(jobProgress({kind:'fetch',status:'running',message:'Starting',parts_total:1,parts_done:0}).percent,0);
});
test('a guessed one-part AI job fills the bar and shows its time left',()=>{
  const job=timedJob({kind:'summary',active_provider:'codex',message:'Codex · summary · part 1/1',
    step_expected_seconds:10,step_started:new Date(fixedNow-5000).toISOString()});
  const progress=jobProgress(job,fixedNow);
  assert.equal(progress.percent,50);
  assert.equal(progress.timingLabel,'about 5 s left');
});
test('Antigravity without a known length has no timing label until its size arrives',()=>{
  const job=timedJob({active_provider:'antigravity',steps_total:1,step_size:null,step_expected_seconds:null});
  const unknown=jobProgress(job,fixedNow);
  assert.equal(unknown.percent,null);
  assert.equal(unknown.timingLabel,'');
  assert.equal(jobProgress({...job,step_size:321},fixedNow).timingLabel,'timing this first run');
});
test('countdown switches to an approximate minute at 90 seconds',()=>{
  const progress=jobProgress(timedJob({step_expected_seconds:120,step_started:new Date(fixedNow).toISOString()}),fixedNow);
  assert.equal(progress.timingLabel,'about 2 min left');
});
test('a completed timed job reaches 100 percent',()=>{
  const progress=jobProgress(timedJob({status:'completed'}),fixedNow);
  assert.equal(progress.percent,100);
  assert.equal(progress.timingLabel,'');
});
test('progress for the same job never moves backward when a step finishes late',()=>{
  const job=timedJob({id:'monotonic-job',steps_total:3,step_started:new Date(fixedNow-11000).toISOString()});
  assert.equal(jobProgress(job,fixedNow).percent,33);
  const next=jobProgress({...job,step_index:2,step_started:new Date(fixedNow).toISOString(),step_expected_seconds:30,
    progress_floor:33},fixedNow);
  assert.equal(next.percent,33);
});
test('without a part count the bar names the step the service reports',()=>{
  const {jobStep}=require('../static/activity.js');
  assert.equal(jobStep({message:'Antigravity · downloading private local audio'}),'downloading private local audio');
  assert.equal(jobStep({message:'Gemini · gemini-3.8-flash temporarily busy (high demand) for 00:00-00:19; retrying this clip with gemini-3.6-flash'}),
    'gemini-3.8-flash busy · switching to gemini-3.6-flash');
  assert.equal(jobStep({message:'Gemini · exact YouTube duration unavailable; using clip evidence to detect the end.'}),'exact YouTube duration unavailable');
  assert.equal(jobStep({message:'Requesting Supadata (fallback)'}),'trying the fallback service');
  for(const message of ['Requesting Gemini','Starting','Queued',''])assert.equal(jobStep({message}),'');
  assert.equal(jobStep({message:'Cancel requested. The current provider is stopping safely.'}),'stopping…');
  assert.equal(jobStep({message:'x'.repeat(120)}).length,80);
  assert.equal(describeJob({...running,active_provider:'antigravity',message:'Antigravity · transcribing the complete local audio'},[],now).detail,
    'Antigravity · transcribing the complete local audio · 00:42');
  assert.equal(describeJob({...running,active_provider:'gemini',message:'Gemini · m · transcribing 00:00-20:00 (part 1 of 2)'},[],now).detail,
    'Gemini · part 1 of 2 (00:00–20:00) · 00:42');  // a part count wins over the step
});
test('the working bar shows the running job, its batch place and how many wait',()=>{
  const view=describeWork({jobs:[{id:'q',video_id:'v2',kind:'fetch',status:'queued'},running,{id:'old',status:'completed'}],videos:[]},now);
  assert.equal(view.job.id,'job');assert.equal(view.batch,'video 3 of 8');assert.equal(view.more,'1 more waiting');
  assert.equal(describeWork({jobs:[{id:'old',status:'failed'}]},now),null);
});
