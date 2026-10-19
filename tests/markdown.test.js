const {test}=require('node:test');
const assert=require('node:assert/strict');
const {renderMarkdown}=require('../static/markdown.js');

// A small DOM that only allows what the renderer is meant to build. Anything else throws, so a test fails
// if the renderer creates another element, sets another attribute, or ever tries innerHTML.
const TAGS=new Set(['p','h1','h2','h3','h4','h5','h6','hr','pre','code','blockquote','ul','ol','li','div','table','thead','tbody','tr','th','td','a','strong','em']);
const ATTRIBUTES=new Set(['href','target','rel','class','start']);
const ESCAPES={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'};
const escapeText=text=>text.replace(/[&<>]/g,c=>ESCAPES[c]), escapeAttribute=text=>text.replace(/[&<>"]/g,c=>ESCAPES[c]);
class Text {constructor(text){this.text=String(text);}}
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attributes=new Map();}
  appendChild(child){assert.ok(child instanceof Text||child instanceof Element,'appendChild needs a node');this.children.push(child);return child;}
  setAttribute(name,value){
    if(!ATTRIBUTES.has(name))throw new Error(`attribute not allowed: ${name}`);
    this.attributes.set(name,String(value));
  }
  set textContent(value){this.children=value===''?[]:[new Text(value)];}
  get textContent(){return this.children.map(c=>c instanceof Text?c.text:c.textContent).join('');}
  set innerHTML(value){throw new Error('innerHTML must never be used');}
  set outerHTML(value){throw new Error('outerHTML must never be used');}
  insertAdjacentHTML(){throw new Error('insertAdjacentHTML must never be used');}
}
const fakeDocument={
  createElement(tag){if(!TAGS.has(tag))throw new Error(`element not allowed: ${tag}`);return new Element(tag);},
  createTextNode:text=>new Text(text),
  createDocumentFragment:()=>new Element('#fragment'),
};
const serialize=node=>{
  if(node instanceof Text)return escapeText(node.text);
  const attributes=[...node.attributes].map(([k,v])=>` ${k}="${escapeAttribute(v)}"`).join('');
  const inner=node.children.map(serialize).join('');
  return node.tag==='#fragment'?inner:node.tag==='hr'?'<hr>':`<${node.tag}${attributes}>${inner}</${node.tag}>`;
};
const VIDEO='https://www.youtube.com/watch?v=jNQXAC9IVRw';
const tree=(markdown,options={})=>renderMarkdown(markdown,{document:fakeDocument,...options});
const html=(markdown,options)=>serialize(tree(markdown,options));
const walk=(node,visit)=>{if(node instanceof Element){visit(node);node.children.forEach(c=>walk(c,visit));}};
const LINK='target="_blank" rel="noopener noreferrer"';

test('the result is a fragment of elements built without HTML parsing',()=>{
  const fragment=tree('# Title\n\nbody');
  assert.equal(fragment.tag,'#fragment');
  assert.deepEqual(fragment.children.map(c=>c.tag),['h1','p']);
  assert.equal(html(''),'');assert.equal(html('  \n\n  '),'');
  assert.equal(html(null),'<p>null</p>');
});
test('headings run from level 1 to 6 and need a space after the hashes',()=>{
  assert.equal(html('# One\n## Two\n### Three\n#### Four\n##### Five\n###### Six'),'<h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4><h5>Five</h5><h6>Six</h6>');
  assert.equal(html('####### seven'),'<p>####### seven</p>');
  assert.equal(html('#hashtag'),'<p>#hashtag</p>');
  assert.equal(html('## Title ##'),'<h2>Title</h2>');
  assert.equal(html('# C#'),'<h1>C#</h1>');
  assert.equal(html('#'),'<h1></h1>');
  assert.equal(html('# A **bold** `code` title'),'<h1>A <strong>bold</strong> <code>code</code> title</h1>');
});
test('paragraphs are split by blank lines and keep their line breaks as text',()=>{
  assert.equal(html('First line\nsecond line\n\nNext paragraph'),'<p>First line\nsecond line</p><p>Next paragraph</p>');
  assert.equal(html('a\r\nb\r\n\r\nc'),'<p>a\nb</p><p>c</p>');
});
test('bold and italic follow the CommonMark rules',()=>{
  assert.equal(html('**bold** and *italic* and __bold__ and _italic_'),'<p><strong>bold</strong> and <em>italic</em> and <strong>bold</strong> and <em>italic</em></p>');
  assert.equal(html('***both***'),'<p><em><strong>both</strong></em></p>');
  assert.equal(html('**bold *nested italic* bold**'),'<p><strong>bold <em>nested italic</em> bold</strong></p>');
  assert.equal(html('snake_case_name and 5 * 3 * 2'),'<p>snake_case_name and 5 * 3 * 2</p>');
  assert.equal(html('**unclosed and *also'),'<p>**unclosed and *also</p>');
  assert.equal(html('\\*not italic\\*'),'<p>*not italic*</p>');
  assert.equal(html('an *italic with `code` inside*'),'<p>an <em>italic with <code>code</code> inside</em></p>');
});
test('inline code keeps everything inside literal',()=>{
  assert.equal(html('use `a <b> & **c**` here'),'<p>use <code>a &lt;b&gt; &amp; **c**</code> here</p>');
  assert.equal(html('`` a`b ``'),'<p><code>a`b</code></p>');
  assert.equal(html('a ` lone backtick'),'<p>a ` lone backtick</p>');
});
test('fenced code is literal, keeps blank lines and ends at the matching fence',()=>{
  assert.equal(html('```js\nlet a = "<x>";\n\n# not a heading\n```\nafter'),'<pre><code>let a = "&lt;x&gt;";\n\n# not a heading</code></pre><p>after</p>');
  assert.equal(html('~~~\nfenced\n~~~'),'<pre><code>fenced</code></pre>');
  assert.equal(html('````\n```\ninner\n```\n````'),'<pre><code>```\ninner\n```</code></pre>');
  assert.equal(html('```\nnever closed\n- still code'),'<pre><code>never closed\n- still code</code></pre>');
  assert.equal(html('```inline``` is not a fence'),'<p><code>inline</code> is not a fence</p>');
});
test('bullet and numbered lists',()=>{
  assert.equal(html('- a\n- b\n- c'),'<ul><li>a</li><li>b</li><li>c</li></ul>');
  assert.equal(html('* a\n* b'),'<ul><li>a</li><li>b</li></ul>');
  assert.equal(html('1. one\n2. two\n3. three'),'<ol><li>one</li><li>two</li><li>three</li></ol>');
  assert.equal(html('3) three\n4) four'),'<ol start="3"><li>three</li><li>four</li></ol>');
  assert.equal(html('- a\n* b'),'<ul><li>a</li></ul><ul><li>b</li></ul>');
  assert.equal(html('text\n- item'),'<p>text</p><ul><li>item</li></ul>');
  assert.equal(html('-5 degrees and 1.5 million'),'<p>-5 degrees and 1.5 million</p>');
  assert.equal(html('**Note:** this is not a bullet'),'<p><strong>Note:</strong> this is not a bullet</p>');
  assert.equal(html('- line one\n  continues here'),'<ul><li>line one\ncontinues here</li></ul>');
  assert.equal(html('- lazy line\ncontinues'),'<ul><li>lazy line\ncontinues</li></ul>');
});
test('nested lists, mixed kinds and several levels',()=>{
  assert.equal(html('- fruit\n  - apple\n  - pear\n    1. green\n    2. yellow\n- veg'),
    '<ul><li>fruit<ul><li>apple</li><li>pear<ol><li>green</li><li>yellow</li></ol></li></ul></li><li>veg</li></ul>');
  assert.equal(html('1. Setup\n   - install\n   - configure\n2. Run\n   1. start\n   2. stop'),
    '<ol><li>Setup<ul><li>install</li><li>configure</li></ul></li><li>Run<ol><li>start</li><li>stop</li></ol></li></ol>');
  assert.equal(html('- a\n    - four spaces\n        - eight spaces'),'<ul><li>a<ul><li>four spaces<ul><li>eight spaces</li></ul></li></ul></li></ul>');
});
test('a list with blank lines between items is loose and keeps paragraphs',()=>{
  assert.equal(html('- a\n\n- b'),'<ul><li><p>a</p></li><li><p>b</p></li></ul>');
  assert.equal(html('- first\n\n  second paragraph\n- next'),'<ul><li><p>first</p><p>second paragraph</p></li><li><p>next</p></li></ul>');
  assert.equal(html('- code:\n  ```\n  a\n\n  b\n  ```\n- next'),'<ul><li>code:<pre><code>a\n\nb</code></pre></li><li>next</li></ul>');
});
test('blockquotes hold other blocks and nest',()=>{
  assert.equal(html('> quote line\n> more\n>\n> - item\n\nafter'),'<blockquote><p>quote line\nmore</p><ul><li>item</li></ul></blockquote><p>after</p>');
  assert.equal(html('> outer\n> > inner'),'<blockquote><p>outer</p><blockquote><p>inner</p></blockquote></blockquote>');
  assert.equal(html('>no space'),'<blockquote><p>no space</p></blockquote>');
});
test('horizontal rules',()=>{
  for(const rule of ['---','***','___','- - -','_  _  _','-----------'])assert.equal(html(`above\n\n${rule}\n\nbelow`),'<p>above</p><hr><p>below</p>',rule);
  assert.equal(html('--'),'<p>--</p>');
});
test('tables with alignment, inline formatting, escaped pipes and short rows',()=>{
  const source='| Name | Qty | Note |\n|:-----|----:|:----:|\n| Apple | 3 | **fresh** |\n| Pear \\| Quince | 4 |\n';
  assert.equal(html(source),'<div class="md-table"><table><thead><tr><th class="md-left">Name</th><th class="md-right">Qty</th><th class="md-center">Note</th></tr></thead>'
    +'<tbody><tr><td class="md-left">Apple</td><td class="md-right">3</td><td class="md-center"><strong>fresh</strong></td></tr>'
    +'<tr><td class="md-left">Pear | Quince</td><td class="md-right">4</td><td class="md-center"></td></tr></tbody></table></div>');
  assert.equal(html('a | b\n--- | ---\n1 | 2'),'<div class="md-table"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>');
  assert.equal(html('| only | head |\n|---|---|'),'<div class="md-table"><table><thead><tr><th>only</th><th>head</th></tr></thead></table></div>');
  assert.equal(html('| a | b |\n|---|\n| 1 | 2 |'),'<p>| a | b |\n|---|\n| 1 | 2 |</p>');
  assert.equal(html('| a |\n|---|\n| 1 |\n\nafter'),'<div class="md-table"><table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></div><p>after</p>');
});
test('http and https links open in a new tab without giving the page away',()=>{
  assert.equal(html('see [the site](https://example.com/a?b=1&c=2)'),`<p>see <a href="https://example.com/a?b=1&amp;c=2" ${LINK}>the site</a></p>`);
  assert.equal(html('[plain](http://example.com)'),`<p><a href="http://example.com" ${LINK}>plain</a></p>`);
  assert.equal(html('[**bold** link](https://x.test "a title")'),`<p><a href="https://x.test" ${LINK}><strong>bold</strong> link</a></p>`);
  assert.equal(html('[wiki](https://en.wikipedia.org/wiki/Foo_(bar))'),`<p><a href="https://en.wikipedia.org/wiki/Foo_(bar)" ${LINK}>wiki</a></p>`);
  assert.equal(html('[angle](<https://example.com/a b>)'),'<p>[angle](&lt;https://example.com/a b&gt;)</p>');
  assert.equal(html('[a](https://one.test) and [b](https://two.test)'),`<p><a href="https://one.test" ${LINK}>a</a> and <a href="https://two.test" ${LINK}>b</a></p>`);
  assert.equal(html('[not closed](https://x.test and [brackets] stay'),'<p>[not closed](https://x.test and [brackets] stay</p>');
  assert.equal(html('![picture](https://example.com/a.png)'),'<p>![picture](https://example.com/a.png)</p>');
  assert.equal(html('a bare https://example.com url is plain text'),'<p>a bare https://example.com url is plain text</p>');
});
test('timestamps become links to that second of the video',()=>{
  const options={videoUrl:VIDEO};
  const link=(seconds,label)=>`<a href="${VIDEO}&amp;t=${seconds}" ${LINK}>${label}</a>`;
  assert.equal(html('[00:12:34] the topic',options),`<p>${link(754,'[00:12:34]')} the topic</p>`);
  assert.equal(html('at [12:34] and (00:01:05) and (2:30)',options),`<p>at ${link(754,'[12:34]')} and ${link(65,'(00:01:05)')} and ${link(150,'(2:30)')}</p>`);
  assert.equal(html('[01:02:03]',options),`<p>${link(3723,'[01:02:03]')}</p>`);
  assert.equal(html('[00:00:00]',options),`<p>${link(0,'[00:00:00]')}</p>`);
  assert.equal(html('## Part [00:10:00]\n\n- item [01:00]\n\n| t |\n|---|\n| [00:30] |',options),
    `<h2>Part ${link(600,'[00:10:00]')}</h2><ul><li>item ${link(60,'[01:00]')}</li></ul>`
    +`<div class="md-table"><table><thead><tr><th>t</th></tr></thead><tbody><tr><td>${link(30,'[00:30]')}</td></tr></tbody></table></div>`);
  assert.equal(html('**[00:05]** bold stamp',options),`<p><strong>${link(5,'[00:05]')}</strong> bold stamp</p>`);
});
test('timestamps stay text without a YouTube video link, inside code and when they are not times',()=>{
  const text='[00:12:34] and (12:34)';
  assert.equal(html(text),`<p>${text}</p>`);
  for(const videoUrl of ['','javascript:alert(1)','https://evil.test/watch?v=jNQXAC9IVRw','https://www.youtube.com/watch?v=short',`${VIDEO}&x=1`,'http://www.youtube.com/watch?v=jNQXAC9IVRw',`${VIDEO}"onmouseover="x`,undefined,null,42])
    assert.equal(html(text,{videoUrl}),`<p>${text}</p>`,String(videoUrl));
  assert.equal(html('`[00:12:34]` and\n\n```\n[00:12:34]\n```',{videoUrl:VIDEO}),'<p><code>[00:12:34]</code> and</p><pre><code>[00:12:34]</code></pre>');
  assert.equal(html('[00:75:00] [12:99] (1:2:3) [0:5] [12:34:56:78] [ 12:34 ]',{videoUrl:VIDEO}),'<p>[00:75:00] [12:99] (1:2:3) [0:5] [12:34:56:78] [ 12:34 ]</p>');
  assert.equal(html('[the part at 12:34](https://x.test)',{videoUrl:VIDEO}),`<p><a href="https://x.test" ${LINK}>the part at 12:34</a></p>`);
  assert.equal(html('[[00:12:34]](https://x.test)',{videoUrl:VIDEO}),`<p><a href="https://x.test" ${LINK}>[00:12:34]</a></p>`);
});

const HOSTILE=[
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<a href="javascript:alert(1)">x</a>',
  '<iframe src="https://evil.test"></iframe><svg onload=alert(1)>',
  '[x](javascript:alert(1))',
  '[x](JaVaScRiPt:alert(1))',
  '[x](  javascript:alert(1))',
  '[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
  '[x](vbscript:msgbox(1))',
  '[x](//evil.test/path)',
  '[x](/relative/path)',
  '[x](ftp://evil.test)',
  '[x](file:///etc/passwd)',
  '[x](https://ok.test"onmouseover="alert(1))',
  '[x](https://ok.test/a b)',
  '[x](java\nscript:alert(1))',
  '[x](\\javascript:alert(1))',
  '![x](javascript:alert(1))',
  '![x](https://evil.test/track.png)',
  '[<img src=x onerror=alert(1)>](https://ok.test)',
  '`<script>alert(1)</script>`',
  '```\n<script>alert(1)</script>\n```',
  '# <script>alert(1)</script>',
  '- <img src=x onerror=alert(1)>',
  '> <svg onload=alert(1)>',
  '| <script> |\n|---|\n| <img src=x onerror=alert(1)> |',
  '&lt;script&gt;alert(1)&lt;/script&gt; &#60;b&#62;',
  '[00:12:34](javascript:alert(1))',
  '\u0000<b> </b>',
  'a harmless line with a stamp [00:12:34] and (00:00:05) and [ordinary](https://ok.test) text',
];
test('script and image tags in the text appear as literal text',()=>{
  assert.equal(html('<script>alert(1)</script>'),'<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>');
  assert.equal(html('<img src=x onerror=alert(1)>'),'<p>&lt;img src=x onerror=alert(1)&gt;</p>');
  assert.equal(html('before <b>bold</b> after'),'<p>before &lt;b&gt;bold&lt;/b&gt; after</p>');
  assert.equal(html('# <script>x</script>\n\n- <img src=x onerror=alert(1)>\n\n> <svg onload=alert(1)>'),
    '<h1>&lt;script&gt;x&lt;/script&gt;</h1><ul><li>&lt;img src=x onerror=alert(1)&gt;</li></ul><blockquote><p>&lt;svg onload=alert(1)&gt;</p></blockquote>');
  assert.equal(html('`<script>alert(1)</script>`'),'<p><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></p>');
  assert.equal(html('| <script> |\n|---|\n| <img src=x onerror=alert(1)> |'),
    '<div class="md-table"><table><thead><tr><th>&lt;script&gt;</th></tr></thead><tbody><tr><td>&lt;img src=x onerror=alert(1)&gt;</td></tr></tbody></table></div>');
  assert.equal(html('&lt;b&gt; &amp; &#60;'),'<p>&amp;lt;b&amp;gt; &amp;amp; &amp;#60;</p>');
});
test('javascript: and other non-http link targets are never links',()=>{
  assert.equal(html('[x](javascript:alert(1))'),'<p>[x](javascript:alert(1))</p>');
  assert.equal(html('[x](JaVaScRiPt:alert(1))'),'<p>[x](JaVaScRiPt:alert(1))</p>');
  assert.equal(html('[x](data:text/html,hi)'),'<p>[x](data:text/html,hi)</p>');
  assert.equal(html('[x](//evil.test)'),'<p>[x](//evil.test)</p>');
  assert.equal(html('[x](https://ok.test"onmouseover="alert(1))'),'<p>[x](https://ok.test"onmouseover="alert(1))</p>');
});
test('whatever the text, only the expected elements, attributes and link targets are made',()=>{
  const seen={tags:new Set(),links:[],classes:new Set()};
  for(const text of [...HOSTILE,HOSTILE.join('\n\n'),HOSTILE.join('\n'),HOSTILE.map(h=>'> - '+h).join('\n'),HOSTILE.map(h=>`| ${h.replace(/\n/g,' ')} |`).join('\n')])
    for(const videoUrl of [undefined,VIDEO,'javascript:alert(1)']) {
      walk(tree(text,{videoUrl}),node=>{
        seen.tags.add(node.tag);
        for(const [name,value] of node.attributes) {
          if(name==='href')seen.links.push(value);
          if(name==='class')seen.classes.add(value);
          if(name==='target')assert.equal(value,'_blank');
          if(name==='rel')assert.equal(value,'noopener noreferrer');
          if(name==='start')assert.match(value,/^\d+$/);
        }
        if(node.tag==='a'){
          assert.equal(node.attributes.get('target'),'_blank');assert.equal(node.attributes.get('rel'),'noopener noreferrer');
          assert.match(node.attributes.get('href'),/^(https?:\/\/[^\s"<>]+)$/);
        }
      });
    }
  seen.tags.delete('#fragment');
  assert.ok(seen.tags.size>10,'many kinds of element were exercised');
  for(const tag of seen.tags)assert.ok(TAGS.has(tag),tag);
  for(const href of seen.links)assert.match(href,/^https?:\/\/[^\s"<>]+$/,href);
  for(const name of seen.classes)assert.match(name,/^md-(table|left|center|right)$/);
  assert.ok(seen.links.some(h=>h.includes('youtube.com/watch?v=jNQXAC9IVRw&t=754')),'the timestamp links were exercised');
  assert.ok(!seen.tags.has('script')&&!seen.tags.has('img')&&!seen.tags.has('iframe')&&!seen.tags.has('svg'));
});
test('the text of an answer survives rendering unchanged apart from Markdown marks',()=>{
  const source='Plain words, numbers 1,234.56 and symbols < > & " \' stay.\n\nSecond paragraph with ünïcödé and 日本語.';
  assert.equal(tree(source).children.map(c=>c.textContent).join('|'),'Plain words, numbers 1,234.56 and symbols < > & " \' stay.|Second paragraph with ünïcödé and 日本語.');
});
test('hostile input of great size or depth is handled quickly and does not throw',()=>{
  const began=Date.now();
  const inputs=['>'.repeat(5000)+' deep','- '.repeat(3000)+'x',Array.from({length:600},(_,i)=>' '.repeat(i*2)+'- level').join('\n'),
    '*a '.repeat(30000),'a* '.repeat(30000),'['.repeat(60000),'`'.repeat(1)+'a`'.repeat(1)+'``a'.repeat(20000),'[a]('.repeat(20000),
    '# '+' '.repeat(100000)+'x','| a |\n|---|\n'+'| x |\n'.repeat(20000),'_a_b'.repeat(40000),'(12:34) '.repeat(20000),'\\'.repeat(100000)];
  for(const text of inputs)assert.doesNotThrow(()=>tree(text,{videoUrl:VIDEO}));
  assert.ok(Date.now()-began<20000,`took ${Date.now()-began} ms`); // about 3 seconds here; a quadratic slowdown would take minutes
});
