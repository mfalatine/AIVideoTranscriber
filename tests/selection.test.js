'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {shouldRender}=require('../static/selection.js');

test('a polling refresh with unchanged transcript content does not replace selected DOM',()=>{
  assert.equal(shouldRender('same transcript signature','same transcript signature'),false);
  assert.equal(shouldRender('old transcript signature','new transcript signature'),true);
  const app=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
  assert.match(app,/if\(redraw\)\{content\.replaceChildren\(\)/);
  assert.doesNotMatch(app,/content\.replaceChildren\(\); content\.hidden/);
});
