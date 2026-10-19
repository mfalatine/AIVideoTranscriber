(function(root) {
  'use strict';
  // What the settings dialog and the AI toolbar show for API keys, Gemini models and local AI CLIs.
  // Only decisions live here (no DOM), so tests/choices.test.js can check them.
  const OTHER='__other__';
  const option=(value, text)=>({value,text});
  const withId=item=>item.label&&item.label!==item.id?`${item.label} (${item.id})`:item.id;

  // One API key. state: configured (a key is available, saved or from the environment), saved (Save settings stored
  // one), remove (the user chose Remove key), typed (text is in the key field). Never carries any part of a key.
  function keyView({configured, saved, remove, typed}) {
    let view;
    if(saved&&remove)view={text:'Key will be removed when you save',tone:'warn',placeholder:'Paste a new key to keep using this service',action:'Keep key'};
    else if(saved)view={text:'✓ Key saved',tone:'ok',placeholder:'Key saved. Paste a new key only to replace it.',action:'Remove key'};
    else if(configured)view={text:'✓ Key from an environment variable',tone:'ok',placeholder:'Paste a key to save one in this app',action:''};
    else view={text:'No key saved',tone:'muted',placeholder:'Paste your API key',action:''};
    view.note=typed?(saved?'The new key replaces the saved one when you save.':'The key is saved when you save.'):'';
    return view;
  }
  // The notice after Save settings: exactly what changed. keys: [{label, action: 'saved' | 'removed'}].
  function saveMessage(keys, settingsChanged) {
    const parts=keys.map(key=>`${key.label} key ${key.action}.`);
    if(settingsChanged)parts.push(keys.length?'Other settings saved.':'Settings saved.');
    return parts.join(' ')||'No changes to save.';
  }

  // The Gemini model select. models: Google's list or null when it is not loaded. The saved model is always an option.
  function geminiModelChoices(models, saved, selected=saved) {
    // The name only: token limits made the label longer than the select and cut it off.
    const options=(models||[]).map(model=>option(model.id,withId(model)));
    if(saved&&!options.some(o=>o.value===saved))options.unshift(option(saved,models?`${saved} (not in Google's list)`:saved));
    options.push(option(OTHER,'Other model name…'));
    return {options,selected:options.some(o=>o.value===selected)?selected:saved};
  }
  // The value to save from the select and the custom text box.
  const modelValue=(selected, custom)=>selected===OTHER?String(custom).trim():selected;
  function geminiClipHelp(value) {
    const seconds=Number(value);
    if(!Number.isInteger(seconds)||seconds<60||seconds>3600)return 'Choose 60 to 3,600 whole seconds.';
    const minutes=seconds/60, requests=Math.ceil(3600/seconds);
    return `${Number.isInteger(minutes)?minutes:minutes.toFixed(1)} minutes per clip · about ${requests} successful clip request${requests===1?'':'s'} per hour of video, plus an end check only when the response cannot establish the end.`;
  }
  const geminiProvenance=entry=>entry&&Array.isArray(entry.models)&&entry.models.length?entry.models.join(' → '):entry?.model||'';
  const geminiPrimaryModel=entry=>{
    const clips=Array.isArray(entry?.clip_models)?entry.clip_models.filter(item=>item&&typeof item.model==='string'):[];
    const counts=new Map();for(const clip of clips)counts.set(clip.model,(counts.get(clip.model)||0)+1);
    let model=entry?.model||entry?.models?.[0]||'';
    if(counts.size)model=[...counts].sort((a,b)=>b[1]-a[1])[0][0];
    const otherModels=counts.size?counts.size-1:Math.max(0,new Set(entry?.models||[]).size-(model?1:0));
    return {text:model+(otherModels?` +${otherModels} ${otherModels===1?'model':'models'}`:''),title:entry?.clip_model_summary||''};
  };
  const completionWarning=entry=>typeof entry?.completion_warning==='string'?entry.completion_warning:'';

  // ---- local AI CLIs (an entry of ai_cli.availability(), plus last_model) ----
  const nameOf=(cli, id)=>{const found=(cli.models||[]).find(model=>model.id===id);return found&&found.label?found.label:id;};
  // A CLI that names no default model picks its own; the model it last answered with here is shown as that, not as its default.
  const defaultName=cli=>cli.default_model||(cli.last_model?`chosen by the CLI; last used here: ${cli.last_model}`:'chosen by the CLI');
  // The model select: the CLI default first, then the CLI's models, then Other. A saved value that is not
  // listed (a custom name, or a list that is missing) is shown as Other with that name in `custom`.
  function cliModelChoices(cli, saved) {
    const options=[option('',`CLI default (${defaultName(cli)})`),...(cli.models||[]).map(model=>option(model.id,withId(model))),option(OTHER,'Other model name…')];
    const listed=options.some(o=>o.value===saved);
    return {options,selected:listed?saved:OTHER,custom:listed?'':saved};
  }
  // The effort select for the model that will run: Codex lists efforts per model, other CLIs have one list.
  // `keepSaved` keeps a saved effort that the model does not list (kept when the dialog opens, dropped when the model changes).
  function cliEffortChoices(cli, model, saved, keepSaved=false) {
    const entry=(cli.models||[]).find(item=>item.id===(model||cli.default_model));
    const efforts=entry&&entry.efforts&&entry.efforts.length?entry.efforts:cli.efforts||[];
    const options=[option('',`CLI default${cli.default_effort?` (${cli.default_effort})`:''}`),...efforts.map(effort=>option(effort,effort))];
    if(keepSaved&&saved&&!efforts.includes(saved))options.push(option(saved,`${saved} (not listed for this model)`));
    return {options,selected:options.some(o=>o.value===saved)?saved:''};
  }
  // Whether the CLI has any reasoning effort to choose from. When it has none (Antigravity CLI), the effort select and its label are hidden.
  const hasEfforts=cli=>!!cli&&((cli.efforts||[]).length>0||(cli.models||[]).some(model=>(model.efforts||[]).length>0));
  // The toolbar's AI assistant select: one option per CLI the server reports, so a new CLI needs no change to the page.
  const aiProviderChoices=ai=>Object.entries(ai||{}).map(([id,cli])=>option(id,cli.label||id));
  // One radio row of the Local AI CLI list.
  function cliRow(cli) {
    const bits=[`Default model: ${cli.default_model||'chosen by the CLI'}`];
    if(cli.last_model)bits.push(`last used here: ${cli.last_model}`);
    if(cli.default_effort)bits.push(`default effort: ${cli.default_effort}`);
    return {name:cli.label,state:cli.installed?'Installed':'Not found',detail:bits.join(' · ')};
  }
  // The AI assistant toolbar: override is the model saved in Settings ('' for the CLI's default).
  function aiStatus(cli, override) {
    if(!cli||!cli.installed)return 'CLI not found';
    if(override)return `Installed · ${override}`;
    if(cli.default_model)return `Installed · ${nameOf(cli,cli.default_model)} (default)`;
    if(cli.last_model)return `Installed · last used ${cli.last_model}`;
    return 'Installed · model chosen by the CLI';
  }
  const api={OTHER,keyView,saveMessage,geminiModelChoices,modelValue,geminiClipHelp,geminiProvenance,geminiPrimaryModel,completionWarning,cliModelChoices,cliEffortChoices,hasEfforts,aiProviderChoices,cliRow,aiStatus};
  root.AIVideoTranscriberChoices=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(globalThis);
