(function(root) {
  'use strict';
  const names={gemini:'Gemini',supadata:'Supadata',fetchtranscript:'FetchTranscript',whisper:'Whisper (this PC)',codex:'Codex',claude:'Claude',antigravity:'Antigravity'};
  const actions={fetch:'Retrieving',file:'Transcribing file',formatted:'Formatting',summary:'Summarizing',ask:'Answering'};
  function describeActivity(state, connection, now=Date.now()) {
    if(connection!=='connected')return {
      text:connection==='offline'?'Connection lost · reconnecting…':'Connecting…',
      tone:connection==='offline'?'error':'waiting',
      title:connection==='offline'?'The dashboard cannot reach its server. Retrying automatically.':'Waiting for the first server response.'
    };
    const jobs=state.jobs||[], videos=state.videos||[];
    const running=jobs.find(job=>job.status==='running');
    const queued=jobs.filter(job=>job.status==='queued');
    if(running) {
      let action=running.kind==='fetch' && running.compare?'Comparing':actions[running.kind]||'Working';
      if(running.kind==='fetch' && running.batch_total>1)action+=` ${running.batch_index} of ${running.batch_total}`;
      else if(running.parts_total>1)action+=` · ${running.parts_done||0}/${running.parts_total} parts done`;
      const bits=[action];
      if(running.active_provider)bits.push(names[running.active_provider]||running.active_provider);
      const started=Date.parse(running.started);
      if(Number.isFinite(started)) {
        const seconds=Math.max(0,Math.floor((now-started)/1000));
        bits.push(`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`);
      }
      if(queued.length && !(running.kind==='fetch' && running.batch_total>1))bits.push(`${queued.length} queued`);
      const video=videos.find(video=>video.id===running.video_id);
      return {text:bits.join(' · '),tone:'busy',title:[video?.title||running.video_id,running.message,`${queued.length} queued`].filter(Boolean).join('\n')};
    }
    if(queued.length)return {text:`Queued · ${queued.length} task${queued.length===1?'':'s'}`,tone:'waiting',title:'Waiting for the next task to start.'};
    // A later successful retry clears the earlier failure from this compact status.
    const latest=new Map();
    for(const job of [...jobs].sort((a,b)=>Date.parse(b.created)-Date.parse(a.created))) {
      const key=job.video_id+':'+job.kind;
      if(!latest.has(key))latest.set(key,job);
    }
    const problems=[...latest.values()].filter(job=>['failed','interrupted'].includes(job.status));
    if(problems.length)return {text:`Needs attention · ${problems.length} task${problems.length===1?'':'s'}`,tone:'error',title:problems.map(job=>job.message||job.status).join('\n')};
    const saved=videos.filter(video=>video.has_transcript).length;
    return {text:`Ready · ${saved} saved transcript${saved===1?'':'s'}`,tone:'ready',title:'No tasks are running or queued.'};
  }
  const verbs={fetch:'Getting transcript',file:'Transcribing file',formatted:'Formatting',summary:'Summarizing',ask:'Answering your question'};
  const lastPercentByJob=new Map();
  function clock(seconds) {
    const s=Math.max(0,Math.floor(seconds)), h=Math.floor(s/3600), m=String(Math.floor(s/60)%60).padStart(2,'0'), r=String(s%60).padStart(2,'0');
    return h?`${h}:${m}:${r}`:`${m}:${r}`;
  }
  // Where a job is, read from what the server reports: Gemini says "transcribing 20:00-40:00 (part 2 of 3)" (no "of 3"
  // when the video length is unknown), an AI CLI says "part 2/3" and fills parts_done/parts_total as parts finish.
  // percent counts finished parts, so it is null when nothing tells how far along the job is.
  function jobProgress(job, now=Date.now()) {
    const message=job?.message||'';
    let part=0,total=0,range='',filePercent=null;
    const fileSecondsDone=Number(job?.file_seconds_done),fileSecondsTotal=Number(job?.file_seconds_total);
    const hasFileProgress=job?.kind==='file' && job?.active_provider==='whisper' &&
      Number.isFinite(fileSecondsDone) && Number.isFinite(fileSecondsTotal) && fileSecondsTotal>0;
    const fileLabelPrefix='Whisper · ';
    const fileLabel=message.startsWith(fileLabelPrefix)?message.slice(fileLabelPrefix.length):message;
    if(hasFileProgress){range=fileLabel;part=1;total=1;filePercent=Math.max(0,Math.min(99,Math.round(fileSecondsDone*100/fileSecondsTotal)));}
    const clip=message.match(/transcribing ([\d:]+)-([\d:]+) \(part (\d+)(?: of (\d+))?\)/);
    if(clip){range=`${clip[1]}–${clip[2]}`;part=Number(clip[3]);total=Number(clip[4]||0);}
    const ai=!clip && message.match(/part (\d+)\/(\d+)/);
    if(ai){part=Number(ai[1]);total=Number(ai[2]);}
    if(!part && job?.parts_total){total=job.parts_total;part=Math.min((job.parts_done||0)+1,total);}
    const combining=/combining evidence/.test(message);
    const stepExpected=Number(job?.step_expected_seconds), stepStarted=Date.parse(job?.step_started);
    const stepIndex=Number(job?.step_index), stepsTotal=Number(job?.steps_total);
    const hasKnownSteps=Number.isInteger(stepsTotal)&&stepsTotal>0;
    const hasKnownStepSize=Number.isFinite(Number(job?.step_size))&&Number(job?.step_size)>0;
    const hasStep=job?.status==='running' && Number.isFinite(stepStarted);
    const hasEstimate=hasStep && hasKnownSteps && Number.isInteger(stepIndex) && stepIndex>0 &&
      stepIndex<=stepsTotal && Number.isFinite(stepExpected) && stepExpected>0;
    const onePartAi=job?.status==='running' && ['formatted','summary','ask'].includes(job?.kind) &&
      part===1 && total===1 && !combining;
    let percent=null, timingLabel='';
    if(job?.status==='completed')percent=100;
    else if(job?.status==='running' && combining)percent=90;
    else if(job?.status==='running' && hasFileProgress)percent=filePercent;
    else if(job?.status==='running' && total)percent=Math.round((part-1)*100/total);
    if(hasEstimate) {
      const elapsed=Math.max(0,(now-stepStarted)/1000);
      const remaining=Math.max(0,stepExpected-elapsed)+Math.max(0,Number(job?.expected_remaining_seconds)||0);
      if(elapsed>stepExpected) {
        percent=Math.min(95,Math.round(stepIndex*100/stepsTotal));
        timingLabel='taking longer than usual';
      } else {
        const stepFraction=Math.min(elapsed/stepExpected,1);
        percent=Number.isInteger(stepIndex) && stepIndex>0
          ?Math.min(95,Math.round(((stepIndex-1)+stepFraction)*100/stepsTotal)):null;
        timingLabel=remaining<90?`about ${Math.max(1,Math.ceil(remaining))} s left`:
          `about ${Math.max(1,Math.round(remaining/60))} min left`;
      }
    } else if(hasStep && hasKnownSteps && (job?.active_provider!=='antigravity' || hasKnownStepSize)) {
      timingLabel='timing this first run';
    }
    if(onePartAi && !hasEstimate)percent=null;
    if(job?.status==='running' && percent!==null) {
      percent=Math.min(hasFileProgress?99:95,Math.max(percent,Number(job?.progress_floor)||0,
        job?.id ? lastPercentByJob.get(job.id)||0 : 0));
      if(job?.id) {
        lastPercentByJob.set(job.id,percent);
        if(lastPercentByJob.size>300)lastPercentByJob.delete(lastPercentByJob.keys().next().value);
      }
    }
    const label=combining?'combining all parts':hasFileProgress?fileLabel:part?`part ${part}${total?' of '+total:''}${range?' ('+range+')':''}`:'';
    return {part,total,range,percent,label,timingLabel};
  }
  function videoTitle(id, videos) {
    const video=(videos||[]).find(v=>v.id===id);
    if(video?.local_file?.name)return video.local_file.name;
    return video?.title && video.title!==id?video.title:`YouTube video ${id}`;
  }
  // What a job is doing when there is no part to count, from the server's own words without the service name:
  // "downloading private local audio", "gemini-3.8-flash busy · switching to gemini-3.6-flash", "trying the fallback service".
  function jobStep(job) {
    const message=String(job?.message||'').trim();
    if(!message || /^(Queued|Starting)$/.test(message))return '';
    if(/^Cancel requested/.test(message))return 'stopping…';
    if(/^Requesting /.test(message))return /\(fallback\)/.test(message)?'trying the fallback service':'';
    const busy=message.match(/(\S+) (?:temporarily busy|unavailable|daily quota exhausted)\b.*retrying this clip with (\S+)/);
    if(busy)return `${busy[1]} busy · switching to ${busy[2]}`;
    const step=message.replace(/^[A-Z][A-Za-z ]*? · /,'').split(';')[0].replace(/\.$/,'');
    return step.length>80?step.slice(0,79)+'…':step;
  }
  // One job as words for the working bar and the view it belongs to.
  function describeJob(job, videos=[], now=Date.now()) {
    const action=job.kind==='fetch' && job.compare?'Comparing services':verbs[job.kind]||'Working';
    const progress=jobProgress(job), started=Date.parse(job.started);
    const bits=[];
    if(job.status==='queued')bits.push('waiting to start');
    else {
      if(job.active_provider)bits.push(names[job.active_provider]||job.active_provider);
      const step=progress.label||jobStep(job);
      if(step)bits.push(step);
      if(progress.timingLabel)bits.push(progress.timingLabel);
      if(Number.isFinite(started))bits.push(clock((now-started)/1000));
    }
    return {action,title:videoTitle(job.video_id,videos),detail:bits.join(' · '),percent:progress.percent,message:job.message||''};
  }
  // The working bar shows the running job (or the next queued one) and how many more are waiting.
  function describeWork(state, now=Date.now()) {
    const jobs=state.jobs||[], active=jobs.filter(job=>['queued','running'].includes(job.status));
    const job=active.find(item=>item.status==='running')||active[active.length-1];
    if(!job)return null;
    const view=describeJob(job,state.videos,now), waiting=active.length-1;
    view.job=job;
    view.more=waiting?`${waiting} more waiting`:'';
    if(job.kind==='fetch' && job.batch_total>1)view.batch=`video ${job.batch_index} of ${job.batch_total}`;
    return view;
  }
  const api={describeActivity,jobProgress,jobStep,describeJob,describeWork,videoTitle};
  root.AIVideoTranscriberActivity=api;
  if(typeof module!=='undefined' && module.exports)module.exports=api;
})(globalThis);
