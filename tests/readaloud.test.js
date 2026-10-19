const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chunks,wordAt,voiceLabel,voicesFor,pickVoice}=require('../static/readaloud.js');

test('long text is read in pieces cut at sentence ends, and no text is lost',()=>{
  const text='First sentence here. '.repeat(30)+'tail';
  const parts=chunks(text,100);
  assert.ok(parts.length>1);
  for(const part of parts){assert.ok(part.end-part.start<=100);}
  assert.ok(parts.slice(0,-1).every(part=>text.slice(part.start,part.end).endsWith('.')));
  assert.equal(parts.map(part=>text.slice(part.start,part.end)).join(' ').replace(/\s+/g,' '),text.trim().replace(/\s+/g,' '));
});
test('a long piece with no sentence end is cut at a space, never inside a word',()=>{
  const text='word '.repeat(100).trim();
  for(const part of chunks(text,50)){const piece=text.slice(part.start,part.end);assert.match(piece,/^(word ?)+$/);}
});
test('blank text has nothing to read',()=>{assert.deepEqual(chunks('  \n '),[]);});
test('the spoken word is found from any letter in it, or the next word from a space',()=>{
  const text='hello big world';
  assert.deepEqual(wordAt(text,0),[0,5]);assert.deepEqual(wordAt(text,7),[6,9]);
  assert.deepEqual(wordAt(text,5),[6,9]);assert.deepEqual(wordAt(text,99),[10,15]);
});
test('voice names are shortened for the menu',()=>{
  assert.equal(voiceLabel({name:'Microsoft Aria Online (Natural) - English (United States)',lang:'en-US'}),'Aria (Natural) · en-US');
  assert.equal(voiceLabel({name:'Google UK English Female',lang:'en-GB'}),'Google UK English Female · en-GB');
});
const voices=[{name:'Microsoft David - English (United States)',lang:'en-US',default:true},
  {name:'Microsoft Guy Online (Natural) - English (United States)',lang:'en-US'},
  {name:'Microsoft Hedda - German (Germany)',lang:'de-DE'},
  {name:'Microsoft Libby Online (Natural) - English (United Kingdom)',lang:'en-GB'}];
test('the menu lists voices for the transcript language, natural ones first',()=>{
  assert.deepEqual(voicesFor(voices,'en').map(v=>v.name.split(' ')[1]),['Guy','Libby','David']);
  assert.deepEqual(voicesFor(voices,'de').map(v=>v.name.split(' ')[1]),['Hedda']);
  assert.equal(voicesFor(voices,'ja').length,4);
});
test('the saved voice is kept, else a natural US voice is picked',()=>{
  assert.equal(pickVoice(voices,'en','Microsoft David - English (United States)').name.split(' ')[1],'David');
  assert.equal(pickVoice(voices,'en','').name.split(' ')[1],'Guy');
  assert.equal(pickVoice(voices,'en','Microsoft Hedda - German (Germany)').name.split(' ')[1],'Guy');
  assert.equal(pickVoice([],'en',''),null);
});
