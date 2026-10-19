'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const script=fs.readFileSync(path.join(__dirname,'..','static','app.js'),'utf8');
const start=script.indexOf('function migrateStorageKey(');
const end=script.indexOf('\n}',start)+2;
assert.ok(start>=0&&end>start,'browser storage migration must be defined before app initialization');
const keyPairs=[
  ['aivideotranscriber.split','transcriptdesk.split'],
  ['aivideotranscriber.topTab','transcriptdesk.topTab'],
];

function memoryStorage(initial={}) {
  const values=new Map(Object.entries(initial)), writes=[];
  const storage={
    getItem(key){return values.has(key)?values.get(key):null;},
    setItem(key,value){writes.push([key,String(value)]);values.set(key,String(value));},
    removeItem(key){values.delete(key);},
  };
  const context={localStorage:storage};
  vm.runInNewContext(`${script.slice(start,end)}\nglobalThis.migrateStorageKey=migrateStorageKey;`,context);
  return {migrate:context.migrateStorageKey,values,writes};
}

test('browser storage carry-over moves old-only values and removes both old keys',()=>{
  for(const [newKey,oldKey] of keyPairs) {
    const storage=memoryStorage({[oldKey]:'legacy value'});
    storage.migrate(newKey,oldKey);
    assert.equal(storage.values.get(newKey),'legacy value');
    assert.equal(storage.values.has(oldKey),false);
    assert.deepEqual(storage.writes,[[newKey,'legacy value']]);
  }
});

test('browser storage carry-over preserves new values and removes old keys when both exist',()=>{
  for(const [newKey,oldKey] of keyPairs) {
    const storage=memoryStorage({[newKey]:'new value',[oldKey]:'old value'});
    storage.migrate(newKey,oldKey);
    assert.equal(storage.values.get(newKey),'new value');
    assert.equal(storage.values.has(oldKey),false);
    assert.deepEqual(storage.writes,[]);
  }
});

test('browser storage carry-over writes nothing when neither key exists',()=>{
  const storage=memoryStorage();
  for(const [newKey,oldKey] of keyPairs)storage.migrate(newKey,oldKey);
  assert.equal(storage.values.size,0);
  assert.deepEqual(storage.writes,[]);
});
