const {test}=require('node:test');
const assert=require('node:assert/strict');
const choices=require('../static/choices.js');
const {OTHER}=choices;
const values=options=>options.map(o=>o.value), texts=options=>options.map(o=>o.text);

const SOL={id:'gpt-6.1-sol',label:'GPT-6.1-Sol',efforts:['low','medium','high'],default_effort:'medium'};
const MINI={id:'gpt-6.1-mini',label:'GPT-6.1-Mini',efforts:['low'],default_effort:'low'};
const codex={installed:true,label:'Codex CLI',default_model:'gpt-6.1-sol',default_effort:'high',models:[SOL,MINI],efforts:['low','medium','high','xhigh','max'],last_model:null};
const claude={installed:true,label:'Claude CLI',default_model:null,default_effort:null,efforts:['low','medium','high','xhigh','max'],last_model:null,
  models:[{id:'opus',label:'Opus'},{id:'sonnet',label:'Sonnet'},{id:'haiku',label:'Haiku'}]};
// What `agy models` listed: the effort is part of each id, so the CLI has no efforts and names no default model.
const antigravity={installed:true,label:'Antigravity CLI',default_model:null,default_effort:null,efforts:[],last_model:null,
  models:[{id:'gemini-3.8-flash-high',label:'Gemini 3.8 Flash (High)'},{id:'claude-sonnet-5-5-medium',label:'Claude Sonnet 5.5 (Medium)'},{id:'gpt-oss-120b-medium',label:'gpt-oss-120b-medium'}]};
const opencode={installed:true,label:'OpenCode CLI',default_model:'opencode/glm-5.3-flash-free',default_effort:null,efforts:[],last_model:null,
  models:[{id:'opencode/glm-5.3-flash-free',label:'GLM 5.3 Flash · Free (limited time)'},
    {id:'openrouter/nvidia/nemotron-3-super-120b-a12b:free',label:'Nemotron · Free'},
    {id:'openrouter/test/model~v2',label:'Test · $1.00/M in · $2.00/M out'}]};

test('a key that is saved shows a green status, the replace hint and a Remove key button',()=>{
  const view=choices.keyView({configured:true,saved:true,remove:false,typed:false});
  assert.deepEqual([view.text,view.tone,view.placeholder,view.action],['✓ Key saved','ok','Key saved. Paste a new key only to replace it.','Remove key']);
});
test('no key shows a muted status and the plain placeholder with no Remove button',()=>{
  const view=choices.keyView({configured:false,saved:false,remove:false,typed:false});
  assert.deepEqual([view.text,view.tone,view.placeholder,view.action],['No key saved','muted','Paste your API key','']);
});
test('a key from the environment is reported as such, not as missing and not as saved',()=>{
  const view=choices.keyView({configured:true,saved:false,remove:false,typed:false});
  assert.equal(view.tone,'ok');assert.match(view.text,/environment/);assert.equal(view.action,'');assert.notEqual(view.text,'✓ Key saved');
});
test('choosing Remove key says what will happen and offers to undo it',()=>{
  const view=choices.keyView({configured:true,saved:true,remove:true,typed:false});
  assert.match(view.text,/will be removed/);assert.equal(view.tone,'warn');assert.equal(view.action,'Keep key');
});
test('typing a key explains whether it replaces a saved one',()=>{
  assert.match(choices.keyView({configured:true,saved:true,remove:false,typed:true}).note,/replaces the saved one/);
  assert.match(choices.keyView({configured:false,saved:false,remove:false,typed:true}).note,/saved when you save/);
  assert.equal(choices.keyView({configured:true,saved:true,remove:false,typed:false}).note,'');
});
test('no status text ever carries any part of a key',()=>{
  const secret='AIzaSyD-SECRET-KEY-0123456789';
  for(const state of [{configured:true,saved:true},{configured:false,saved:false},{configured:true,saved:false}])
    for(const remove of [false,true])for(const typed of [false,true]) {
      const view=choices.keyView({...state,remove,typed});
      assert.ok(!JSON.stringify(view).includes(secret));
    }
});

test('the save notice names exactly what changed',()=>{
  assert.equal(choices.saveMessage([{label:'Gemini',action:'saved'}],false),'Gemini key saved.');
  assert.equal(choices.saveMessage([{label:'Supadata',action:'removed'}],false),'Supadata key removed.');
  assert.equal(choices.saveMessage([],true),'Settings saved.');
  assert.equal(choices.saveMessage([{label:'Gemini',action:'saved'},{label:'FetchTranscript',action:'removed'}],false),'Gemini key saved. FetchTranscript key removed.');
  assert.equal(choices.saveMessage([{label:'Gemini',action:'saved'}],true),'Gemini key saved. Other settings saved.');
  assert.equal(choices.saveMessage([],false),'No changes to save.');
});

test('the Gemini model select lists Google models as "label (id)" with the saved one selected and Other last',()=>{
  const models=[{id:'gemini-3.8-flash',label:'Gemini 3.8 Flash',input_tokens:1048576,output_tokens:65536},{id:'gemini-3.8-pro',label:'gemini-3.8-pro',input_tokens:1}];
  const view=choices.geminiModelChoices(models,'gemini-3.8-flash');
  assert.deepEqual(texts(view.options),['Gemini 3.8 Flash (gemini-3.8-flash)','gemini-3.8-pro','Other model name…']);
  assert.deepEqual(values(view.options),['gemini-3.8-flash','gemini-3.8-pro',OTHER]);
  assert.equal(view.selected,'gemini-3.8-flash');
});
test('the saved Gemini model is always present, also when Google does not list it or nothing is loaded',()=>{
  const missing=choices.geminiModelChoices([{id:'gemini-3.8-flash',label:'Gemini 3.8 Flash'}],'gemini-private-1');
  assert.deepEqual(values(missing.options),['gemini-private-1','gemini-3.8-flash',OTHER]);
  assert.equal(missing.selected,'gemini-private-1');assert.match(missing.options[0].text,/not in Google's list/);
  const offline=choices.geminiModelChoices(null,'gemini-3.8-flash');
  assert.deepEqual(texts(offline.options),['gemini-3.8-flash','Other model name…']);assert.equal(offline.selected,'gemini-3.8-flash');
  assert.deepEqual(values(choices.geminiModelChoices([],'x1').options),['x1',OTHER]);
});
test('the Gemini select keeps the user\'s pick across a list refresh and falls back to the saved model',()=>{
  const models=[{id:'gemini-a',label:'A'},{id:'gemini-b',label:'B'}];
  assert.equal(choices.geminiModelChoices(models,'gemini-a','gemini-b').selected,'gemini-b');
  assert.equal(choices.geminiModelChoices(models,'gemini-a',OTHER).selected,OTHER);
  assert.equal(choices.geminiModelChoices(models,'gemini-a','gone').selected,'gemini-a');
});
test('the value to save is the chosen model or the trimmed custom name',()=>{
  assert.equal(choices.modelValue('gemini-a','ignored'),'gemini-a');
  assert.equal(choices.modelValue(OTHER,'  my-model  '),'my-model');
  assert.equal(choices.modelValue(OTHER,''),'');
  assert.equal(choices.modelValue('',''),'');
});
test('Gemini clip help gives honest request arithmetic and validates the range',()=>{
  assert.equal(choices.geminiClipHelp(1200),'20 minutes per clip · about 3 successful clip requests per hour of video, plus an end check only when the response cannot establish the end.');
  assert.match(choices.geminiClipHelp(59),/60 to 3,600/);
});
test('Gemini provenance supports mixed-model and old single-model records',()=>{
  assert.equal(choices.geminiProvenance({models:['gemini-a','gemini-b'],model:'gemini-a'}),'gemini-a → gemini-b');
  assert.equal(choices.geminiProvenance({model:'gemini-old'}),'gemini-old');
  assert.equal(choices.geminiProvenance({}),'');
});
test('Gemini transcript credit names the model used for most clips and exposes detail separately',()=>{
  assert.deepEqual(choices.geminiPrimaryModel({model:'gemini-a',models:['gemini-a','gemini-b'],
    clip_models:[{clip:1,model:'gemini-a'},{clip:2,model:'gemini-b'},{clip:3,model:'gemini-b'}],
    clip_model_summary:'clip 1: gemini-a; clips 2-3: gemini-b'}),
    {text:'gemini-b +1 model',title:'clip 1: gemini-a; clips 2-3: gemini-b'});
  assert.deepEqual(choices.geminiPrimaryModel({model:'gemini-old'}),{text:'gemini-old',title:''});
});
test('a saved completeness warning remains visible verbatim',()=>{
  const warning='end not confirmed: video length unknown';
  assert.equal(choices.completionWarning({completion_warning:warning}),warning);
  assert.equal(choices.completionWarning({}), '');
});

test('the CLI model select starts with the CLI default and ends with Other',()=>{
  const view=choices.cliModelChoices(codex,'');
  assert.deepEqual(texts(view.options),['CLI default (gpt-6.1-sol)','GPT-6.1-Sol (gpt-6.1-sol)','GPT-6.1-Mini (gpt-6.1-mini)','Other model name…']);
  assert.deepEqual(values(view.options),['','gpt-6.1-sol','gpt-6.1-mini',OTHER]);
  assert.deepEqual([view.selected,view.custom],['','']);
});
test('the CLI default text falls back to the last used model and then to "chosen by the CLI"',()=>{
  assert.equal(choices.cliModelChoices({...claude,last_model:'claude-opus-5-5'},'').options[0].text,'CLI default (chosen by the CLI; last used here: claude-opus-5-5)');
  assert.equal(choices.cliModelChoices(claude,'').options[0].text,'CLI default (chosen by the CLI)');
  assert.equal(choices.cliModelChoices({...claude,default_model:'sonnet'},'').options[0].text,'CLI default (sonnet)');
  assert.equal(choices.cliModelChoices({...claude,default_model:'sonnet',last_model:'claude-opus-5-5'},'').options[0].text,'CLI default (sonnet)');
  assert.equal(choices.cliModelChoices({installed:false,label:'X'},'').options[0].text,'CLI default (chosen by the CLI)');
});
test('a saved model that the CLI does not list opens as Other with its name filled in',()=>{
  const view=choices.cliModelChoices(claude,'claude-opus-5-5');
  assert.deepEqual([view.selected,view.custom],[OTHER,'claude-opus-5-5']);
  const listed=choices.cliModelChoices(claude,'sonnet');
  assert.deepEqual([listed.selected,listed.custom],['sonnet','']);
});
test('Codex efforts follow the chosen model, other CLIs use their one list',()=>{
  assert.deepEqual(texts(choices.cliEffortChoices(codex,'gpt-6.1-mini','').options),['CLI default (high)','low']);
  assert.deepEqual(texts(choices.cliEffortChoices(codex,'gpt-6.1-sol','').options),['CLI default (high)','low','medium','high']);
  assert.deepEqual(texts(choices.cliEffortChoices(codex,'','').options),['CLI default (high)','low','medium','high']);
  assert.deepEqual(texts(choices.cliEffortChoices(codex,'my-own-model','').options),['CLI default (high)','low','medium','high','xhigh','max']);
  assert.deepEqual(texts(choices.cliEffortChoices(claude,'opus','').options),['CLI default','low','medium','high','xhigh','max']);
  assert.deepEqual(values(choices.cliEffortChoices(claude,'','').options),['','low','medium','high','xhigh','max']);
});
test('a saved effort is kept when the dialog opens but dropped when the model changes to one without it',()=>{
  assert.equal(choices.cliEffortChoices(codex,'gpt-6.1-mini','high',false).selected,'');
  const kept=choices.cliEffortChoices(codex,'gpt-6.1-mini','high',true);
  assert.equal(kept.selected,'high');assert.match(kept.options.at(-1).text,/not listed for this model/);
  assert.equal(choices.cliEffortChoices(codex,'gpt-6.1-sol','medium',false).selected,'medium');
  assert.equal(choices.cliEffortChoices(codex,'gpt-6.1-sol','',false).selected,'');
});

test('a radio row shows the name, whether it is installed, its default model, the last used model and the Codex effort',()=>{
  assert.deepEqual(choices.cliRow(codex),{name:'Codex CLI',state:'Installed',detail:'Default model: gpt-6.1-sol · default effort: high'});
  assert.deepEqual(choices.cliRow({...codex,last_model:'gpt-6.1-mini'}),{name:'Codex CLI',state:'Installed',detail:'Default model: gpt-6.1-sol · last used here: gpt-6.1-mini · default effort: high'});
  assert.deepEqual(choices.cliRow(claude),{name:'Claude CLI',state:'Installed',detail:'Default model: chosen by the CLI'});
  assert.deepEqual(choices.cliRow({...claude,installed:false,last_model:'claude-opus-5-5'}),
    {name:'Claude CLI',state:'Not found',detail:'Default model: chosen by the CLI · last used here: claude-opus-5-5'});
});
test('the effort select is shown only for a CLI that has efforts to choose from',()=>{
  assert.equal(choices.hasEfforts(codex),true);
  assert.equal(choices.hasEfforts(claude),true);
  assert.equal(choices.hasEfforts(antigravity),false);
  assert.equal(choices.hasEfforts({...antigravity,efforts:undefined,models:undefined}),false);
  assert.equal(choices.hasEfforts({efforts:[],models:[SOL]}),true,'a model that lists its own efforts is enough');
  assert.equal(choices.hasEfforts({efforts:[],models:[{id:'x'},{id:'y',efforts:[]}]}),false);
  assert.equal(choices.hasEfforts(undefined),false);
});
test('a CLI without efforts is left with only the CLI default effort, which is what gets saved',()=>{
  const view=choices.cliEffortChoices(antigravity,'',''),flash=choices.cliEffortChoices(antigravity,'gemini-3.8-flash-high','');
  assert.deepEqual(values(view.options),['']);assert.equal(view.selected,'');assert.deepEqual(values(flash.options),['']);
  assert.equal(choices.cliEffortChoices(antigravity,'gemini-3.8-flash-high','high',false).selected,'');
});
test('the toolbar lists every CLI the server reports, in its order, by its label',()=>{
  const ai={codex,claude,antigravity};
  assert.deepEqual(choices.aiProviderChoices(ai),[{value:'codex',text:'Codex CLI'},{value:'claude',text:'Claude CLI'},{value:'antigravity',text:'Antigravity CLI'}]);
  assert.deepEqual(choices.aiProviderChoices({other:{installed:false}}),[{value:'other',text:'other'}]);
  assert.deepEqual(choices.aiProviderChoices({}),[]);
  assert.deepEqual(choices.aiProviderChoices(undefined),[]);
});
test('Antigravity CLI lists the models the CLI reports, names no default model and shows no effort in the list and the status',()=>{
  const view=choices.cliModelChoices(antigravity,'');
  assert.deepEqual(texts(view.options),['CLI default (chosen by the CLI)','Gemini 3.8 Flash (High) (gemini-3.8-flash-high)','Claude Sonnet 5.5 (Medium) (claude-sonnet-5-5-medium)','gpt-oss-120b-medium','Other model name…']);
  assert.deepEqual([view.selected,view.custom],['','']);
  assert.deepEqual(choices.cliModelChoices(antigravity,'claude-sonnet-5-5-medium').selected,'claude-sonnet-5-5-medium');
  assert.deepEqual(choices.cliModelChoices(antigravity,'gemini-9'),{options:view.options,selected:OTHER,custom:'gemini-9'});
  assert.deepEqual(choices.cliRow(antigravity),{name:'Antigravity CLI',state:'Installed',detail:'Default model: chosen by the CLI'});
  assert.equal(choices.aiStatus(antigravity,''),'Installed · model chosen by the CLI');
  assert.equal(choices.aiStatus({...antigravity,last_model:'gemini-3.8-flash-high'},''),'Installed · last used gemini-3.8-flash-high');
  assert.equal(choices.aiStatus(antigravity,'claude-sonnet-5-5-medium'),'Installed · claude-sonnet-5-5-medium');
  assert.equal(choices.aiStatus({...antigravity,installed:false},''),'CLI not found');
});
test('OpenCode appears by its settings label and its listed model labels include free and public prices',()=>{
  assert.deepEqual(choices.aiProviderChoices({opencode}),[{value:'opencode',text:'OpenCode CLI'}]);
  const view=choices.cliModelChoices(opencode,opencode.default_model);
  assert.equal(view.selected,opencode.default_model);
  assert.equal(view.options[1].text,'GLM 5.3 Flash · Free (limited time) (opencode/glm-5.3-flash-free)');
  assert.match(view.options[3].text, /\$1\.00\/M in · \$2\.00\/M out/);
  assert.equal(choices.hasEfforts(opencode),false);
  assert.deepEqual(choices.cliRow(opencode),{name:'OpenCode CLI',state:'Installed',detail:'Default model: opencode/glm-5.3-flash-free'});
});
test('Antigravity CLI with no model list (not signed in, or not run) still offers the CLI default and Other',()=>{
  const view=choices.cliModelChoices({...antigravity,models:[]},'');
  assert.deepEqual(texts(view.options),['CLI default (chosen by the CLI)','Other model name…']);
  assert.deepEqual([view.selected,view.custom],['','']);
});
test('the AI assistant status says which model will answer',()=>{
  assert.equal(choices.aiStatus(codex,''),'Installed · GPT-6.1-Sol (default)');
  assert.equal(choices.aiStatus(claude,'opus'),'Installed · opus');
  assert.equal(choices.aiStatus(codex,'gpt-6.1-mini'),'Installed · gpt-6.1-mini');
  assert.equal(choices.aiStatus({...claude,last_model:'claude-opus-5-5'},''),'Installed · last used claude-opus-5-5');
  assert.equal(choices.aiStatus(claude,''),'Installed · model chosen by the CLI');
  assert.equal(choices.aiStatus({...claude,installed:false},''),'CLI not found');
  assert.equal(choices.aiStatus(undefined,''),'CLI not found');
});
