const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {videoId,parseLinks,LIMIT}=require('../static/urls.js');
// The same cases tests/test_app.py runs against providers.video_id and Application.add_batch.
const cases=JSON.parse(fs.readFileSync(path.join(__dirname,'video_link_cases.json'),'utf8'));
const VID='jNQXAC9IVRw';

test('every shared link case is accepted or rejected exactly as providers.video_id does',()=>{
  assert.ok(cases.links.filter(c=>c.id).length>10);assert.ok(cases.links.filter(c=>!c.id).length>10);
  for(const c of cases.links) {
    if(c.id===null)assert.throws(()=>videoId(c.input),Error,JSON.stringify(c.input));
    else assert.equal(videoId(c.input),c.id,JSON.stringify(c.input));
  }
});
test('the forms from test_video_id_forms_and_duplicates all give the one id',()=>{
  for(const url of [VID,`https://youtu.be/${VID}?t=30`,`https://www.youtube.com/watch?v=${VID}&list=PL1`,`https://m.youtube.com/shorts/${VID}`,`https://youtube.com/live/${VID}`])
    assert.equal(videoId(url),VID);
});
test('the inputs from test_reject_other_hosts_and_non_videos are all rejected',()=>{
  for(const value of [`https://youtube.com.evil.test/watch?v=${VID}`,'http://127.0.0.1/private','file:///etc/passwd',`https://user@youtube.com/watch?v=${VID}`,'https://youtube.com/playlist?list=PL1'])
    assert.throws(()=>videoId(value),Error,value);
});
test('error text matches the server for the two kinds of rejection',()=>{
  assert.throws(()=>videoId('file:///etc/passwd'),{message:'Enter a YouTube video URL or 11-character video ID.'});
  assert.throws(()=>videoId('https://youtube.com/playlist?list=PL1'),{message:'Use individual YouTube video links. Playlist expansion is not included yet.'});
});
test('the shared batches count ready and invalid lines as Application.add_batch does',()=>{
  for(const c of cases.batches) {
    const result=parseLinks(c.text);
    assert.deepEqual([result.ready,result.entries.filter(e=>e.status==='invalid').length],[c.ready,c.invalid],JSON.stringify(c.text));
  }
});
test('each line is marked ready, duplicate or invalid, in the order typed',()=>{
  const text=`${VID}\nnot a link\nhttps://youtu.be/${VID}\n\n  \nabcdefghijk\nhttps://example.com/x`;
  const result=parseLinks(text);
  assert.deepEqual(result.entries.map(e=>[e.line,e.status]),[
    [VID,'ready'],['not a link','invalid'],[`https://youtu.be/${VID}`,'duplicate'],['abcdefghijk','ready'],['https://example.com/x','invalid']]);
  assert.equal(result.ready,2);assert.equal(result.lines,5);assert.equal(result.tooMany,false);
  assert.equal(result.entries[2].id,VID);assert.equal(result.entries[1].id,null);
});
test('lines are split and trimmed the way Python does it',()=>{
  const result=parseLinks(`　${VID} \r\n\r\nabcdefghijk zyxwvutsrqp\u0085\f${VID}`);
  assert.deepEqual(result.entries.map(e=>e.status),['ready','ready','ready','duplicate']);
  assert.equal(result.ready,3);
});
test('more than the limit of lines is flagged and an empty box has nothing ready',()=>{
  assert.equal(LIMIT,100);
  const many=Array.from({length:101},(_,i)=>`${'a'.repeat(7)}${String(i).padStart(4,'0')}`).join('\n');
  const result=parseLinks(many);
  assert.deepEqual([result.lines,result.ready,result.tooMany],[101,101,true]);
  assert.equal(parseLinks(Array.from({length:100},(_,i)=>`${'a'.repeat(7)}${String(i).padStart(4,'0')}`).join('\n')).tooMany,false);
  assert.deepEqual(parseLinks(''),{entries:[],ready:0,lines:0,limit:100,tooMany:false});
  assert.equal(parseLinks('  \n\n').lines,0);
});
