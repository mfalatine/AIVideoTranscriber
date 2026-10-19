(function(root) {
  'use strict';
  // Markdown -> DOM for AI answers. Transcripts and model output are untrusted, so the result is built only with
  // createElement, createTextNode, textContent and setAttribute. Nothing here parses HTML or uses innerHTML, and
  // the only attribute values that come from the text are http(s) link targets and list start numbers.
  // Limits so that a hostile answer cannot make the page slow: nesting depth, how far a link, code span or emphasis looks for its other end.
  const MAX_DEPTH=20, MAX_SCAN=1000, MAX_SPAN=8000, MAX_EMPHASIS=2000;
  const VIDEO=/^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]{11}$/;
  const SAFE_URL=/^https?:\/\/[^\s\u0000-\u001f\u007f<>"]+$/i;
  const PUNCTUATION=/[\p{P}\p{S}]/u, SPACE=/\s/, ESCAPABLE=/[!-\/:-@\[-`{-~]/;
  const SQUARE=/\[(?:(\d{1,2}):(\d{2}):(\d{2})|(\d{1,3}):(\d{2}))\]/y, ROUND=/\((?:(\d{1,2}):(\d{2}):(\d{2})|(\d{1,3}):(\d{2}))\)/y;
  const FENCE=/^( {0,3})(`{3,}|~{3,})(.*)$/, HEADING=/^ {0,3}(#{1,6})(?:[ \t]+(.*))?$/;
  const RULE=/^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/, QUOTE=/^ {0,3}>/, ITEM=/^( *)([-*+]|(\d{1,9})([.)]))(?:([ \t]+)(.*))?$/;
  const blank=line=>!line.trim();
  const indentOf=line=>line.length-line.trimStart().length;

  // ---- inline text: code spans, links, timestamps, emphasis ----
  function seconds(m) {
    const long=m[1]!==undefined, hours=long?+m[1]:0, minutes=long?+m[2]:+m[4], secs=long?+m[3]:+m[5];
    return secs>59||(long&&minutes>59)?null:hours*3600+minutes*60+secs;
  }
  function parseLink(src, start) {
    let depth=0, j=start;
    for(;j<src.length&&j-start<=MAX_SCAN;j++) {
      if(src[j]==='\\'){j++;continue;}
      if(src[j]==='[')depth++;
      else if(src[j]===']'&&--depth===0)break;
    }
    if(depth!==0||src[j]!==']'||src[j+1]!=='(')return null;
    const label=src.slice(start+1,j), space=c=>c===' '||c==='\t'||c==='\n';
    let k=j+2, dest;
    while(space(src[k]))k++;
    if(src[k]==='<') {
      const close=src.indexOf('>',k+1);
      dest=close<0?null:src.slice(k+1,close);
      if(dest===null||/[\n<]/.test(dest))return null;
      k=close+1;
    } else {
      const from=k;
      let parens=0;
      for(;k<src.length&&k-from<=2*MAX_SCAN;k++) {
        const c=src[k];
        if(c==='\\'&&ESCAPABLE.test(src[k+1]||'')){k++;continue;}
        if(SPACE.test(c)||c<' ')break;
        if(c==='(')parens++;
        else if(c===')'){if(parens===0)break;parens--;}
      }
      if(parens!==0)return null;
      dest=src.slice(from,k);
    }
    while(space(src[k]))k++;
    if(src[k]==='"'||src[k]==="'"||src[k]==='(') {
      const end=src[k]==='('?')':src[k];
      let t=k+1;
      for(;t<src.length&&t-k<=MAX_SCAN;t++) {
        if(src[t]==='\\'){t++;continue;}
        if(src[t]===end)break;
      }
      if(src[t]!==end)return null;
      k=t+1;
      while(space(src[k]))k++;
    }
    if(src[k]!==')')return null;
    const href=dest.replace(/\\([!-\/:-@\[-`{-~])/g,'$1');
    return {end:k+1,label,href:SAFE_URL.test(href)?href:null};
  }
  function delimiter(src, i, ch) {
    let n=1;
    while(src[i+n]===ch)n++;
    const before=i>0?src[i-1]:'\n', after=i+n<src.length?src[i+n]:'\n';
    const spaceAfter=SPACE.test(after), spaceBefore=SPACE.test(before), punctAfter=PUNCTUATION.test(after), punctBefore=PUNCTUATION.test(before);
    const left=!spaceAfter&&(!punctAfter||spaceBefore||punctBefore), right=!spaceBefore&&(!punctBefore||spaceAfter||punctAfter);
    return {k:'delim',ch,n,orig:n,open:ch==='*'?left:left&&(!right||punctBefore),close:ch==='*'?right:right&&(!left||punctAfter)};
  }
  // CommonMark's emphasis rules: `*`/`_` runs pair up into em/strong, `_` does not pair inside a word.
  function emphasis(nodes) {
    for(let ci=0;ci<nodes.length;ci++) {
      const closer=nodes[ci];
      if(closer.k!=='delim'||!closer.close||closer.n===0)continue;
      for(let oi=ci-1;oi>=0&&ci-oi<=MAX_EMPHASIS;oi--) {
        const opener=nodes[oi];
        if(opener.k!=='delim'||!opener.open||opener.ch!==closer.ch||opener.n===0)continue;
        if((closer.open||opener.close)&&(opener.orig+closer.orig)%3===0&&!(opener.orig%3===0&&closer.orig%3===0))continue;
        const use=opener.n>=2&&closer.n>=2?2:1;
        opener.n-=use;closer.n-=use;
        nodes.splice(oi+1,ci-oi-1,{k:use===2?'strong':'em',kids:nodes.slice(oi+1,ci)});
        ci=oi+1;
        break;
      }
    }
    return nodes;
  }
  // `times`: link timestamps; `links`: allow [text](url) (off inside a link label, where links cannot nest).
  function parseInline(src, times, links=true) {
    const nodes=[];
    let text='', i=0;
    const flush=()=>{if(text){nodes.push({k:'text',v:text});text='';}};
    while(i<src.length) {
      const c=src[i];
      if(c==='\\') {
        const next=src[i+1];
        if(next!==undefined&&ESCAPABLE.test(next)){text+=next;i+=2;}
        else{if(next!=='\n')text+=c;i++;}
      } else if(c==='`') {
        let n=1;
        while(src[i+n]==='`')n++;
        let close=-1;
        for(let p=src.indexOf('`',i+n);p>=0&&p-i<=MAX_SPAN;p=src.indexOf('`',p)) {
          let m=0;
          while(src[p+m]==='`')m++;
          if(m===n){close=p;break;}
          p+=m;
        }
        if(close<0){text+='`'.repeat(n);i+=n;continue;}
        let code=src.slice(i+n,close).replace(/\n/g,' ');
        if(code.length>2&&code[0]===' '&&code[code.length-1]===' '&&code.trim())code=code.slice(1,-1);
        flush();nodes.push({k:'code',v:code});i=close+n;
      } else if(c==='['||(c==='!'&&src[i+1]==='[')) {
        const image=c==='!', link=links?parseLink(src,image?i+1:i):null;
        if(link) {
          flush();
          // Images are never loaded, and a target that is not http(s) is never made a link: both stay plain text.
          if(image||!link.href)nodes.push({k:'text',v:src.slice(i,link.end)});
          else nodes.push({k:'link',href:link.href,kids:parseInline(link.label,false,false)});
          i=link.end;continue;
        }
        SQUARE.lastIndex=i;
        const m=times&&!image?SQUARE.exec(src):null, at=m&&seconds(m);
        if(m&&at!==null){flush();nodes.push({k:'time',label:m[0],at});i+=m[0].length;}
        else{text+=c;i++;}
      } else if(c==='('&&times) {
        ROUND.lastIndex=i;
        const m=ROUND.exec(src), at=m&&seconds(m);
        if(m&&at!==null){flush();nodes.push({k:'time',label:m[0],at});i+=m[0].length;}
        else{text+=c;i++;}
      } else if(c==='*'||c==='_') {
        flush();
        const run=delimiter(src,i,c);
        nodes.push(run);i+=run.n;
      } else{text+=c;i++;}
    }
    flush();
    return emphasis(nodes);
  }

  // ---- blocks ----
  function splitRow(line) {
    let s=line.trim();
    if(s.startsWith('|'))s=s.slice(1);
    if(s.endsWith('|')&&!s.endsWith('\\|'))s=s.slice(0,-1);
    const cells=[];
    let cell='';
    for(let i=0;i<s.length;i++) {
      if(s[i]==='\\'&&s[i+1]==='|'){cell+='|';i++;}
      else if(s[i]==='|'){cells.push(cell.trim());cell='';}
      else cell+=s[i];
    }
    cells.push(cell.trim());
    return cells;
  }
  const isDelimiterRow=line=>line.includes('|')&&line.includes('-')&&splitRow(line).every(c=>/^:?-+:?$/.test(c));
  const tableAt=(lines,i)=>i+1<lines.length&&lines[i].includes('|')&&isDelimiterRow(lines[i+1])&&splitRow(lines[i]).length===splitRow(lines[i+1]).length;
  function matchItem(line) {
    const m=ITEM.exec(line);
    if(!m)return null;
    const content=m[6]||'', gap=content&&m[5].length<=4?m[5].length:1, indent=m[1].length, ordered=m[3]!==undefined;
    return {indent,ordered,number:ordered?parseInt(m[3],10):0,mark:ordered?m[4]:m[2],content,contentIndent:indent+m[2].length+gap};
  }
  const sameList=(a,b)=>a.ordered===b.ordered&&a.mark===b.mark;
  function opensFence(line) {
    const m=FENCE.exec(line);
    return m&&!(m[2][0]==='`'&&m[3].includes('`'))?m:null;
  }
  // Would this line start a block that can interrupt a paragraph?
  function interrupts(lines, i) {
    const line=lines[i], item=matchItem(line);
    return !!opensFence(line)||HEADING.test(line)||RULE.test(line)||QUOTE.test(line)||tableAt(lines,i)
      ||(!!item&&item.content!==''&&(!item.ordered||item.number===1));
  }
  function parseBlocks(lines, depth) {
    if(depth>MAX_DEPTH)return [{t:'p',text:lines.map(l=>l.trim()).filter(Boolean).join('\n')}];
    const blocks=[], n=lines.length;
    let i=0, gap=false;
    // `gap` marks a block that follows a blank line; an item with a gap between its blocks makes its list loose.
    const add=block=>{block.gap=gap&&blocks.length>0;gap=false;blocks.push(block);};
    while(i<n) {
      const line=lines[i];
      if(blank(line)){gap=true;i++;continue;}
      const fence=opensFence(line);
      if(fence) {
        const body=[], marker=fence[2], pad=fence[1].length;
        let j=i+1;
        for(;j<n;j++) {
          const close=/^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[j]);
          if(close&&close[1][0]===marker[0]&&close[1].length>=marker.length)break;
          body.push(lines[j].replace(new RegExp('^ {0,'+pad+'}'),''));
        }
        add({t:'code',text:body.join('\n')});i=j+1;continue;
      }
      const heading=HEADING.exec(line);
      if(heading) {
        let text=(heading[2]||'').trim();
        const closing=/(?:^|[ \t])#+$/.exec(text); // an optional closing run of #, as in "## Title ##"
        if(closing)text=text.slice(0,closing.index).trim();
        add({t:'h',level:heading[1].length,text});i++;continue;
      }
      if(RULE.test(line)){add({t:'hr'});i++;continue;}
      if(QUOTE.test(line)) {
        const inner=[];
        while(i<n&&QUOTE.test(lines[i])){inner.push(lines[i].replace(/^ {0,3}> ?/,''));i++;}
        add({t:'quote',kids:parseBlocks(inner,depth+1)});continue;
      }
      const first=matchItem(line);
      if(first) {
        const list={t:'list',ordered:first.ordered,start:first.number,loose:false,items:[]};
        while(i<n) {
          const item=matchItem(lines[i]);
          if(!item||!sameList(item,first))break;
          const body=[item.content];
          let j=i+1;
          while(j<n) {
            const next=lines[j];
            if(blank(next)) {
              let k=j;
              while(k<n&&blank(lines[k]))k++;
              if(k<n&&indentOf(lines[k])>item.indent){for(;j<k;j++)body.push('');continue;}
              break;
            }
            const pad=indentOf(next);
            if(pad>item.indent){body.push(next.slice(Math.min(pad,item.contentIndent)));j++;continue;}
            const before=body[body.length-1];
            if(before.trim()&&!/^\s*(```|~~~)/.test(before)&&!interrupts(lines,j)&&!matchItem(next)){body.push(next.trim());j++;continue;}
            break;
          }
          const kids=parseBlocks(body,depth+1);
          if(kids.some(kid=>kid.gap))list.loose=true;
          list.items.push(kids);
          let k=j;
          while(k<n&&blank(lines[k]))k++;
          const following=k<n?matchItem(lines[k]):null;
          if(following&&sameList(following,first)){list.loose=list.loose||k>j;i=k;}
          else{i=j;break;}
        }
        add(list);continue;
      }
      if(tableAt(lines,i)) {
        const align=splitRow(lines[i+1]).map(c=>c.startsWith(':')&&c.endsWith(':')?'center':c.endsWith(':')?'right':c.startsWith(':')?'left':'');
        const head=splitRow(lines[i]), rows=[];
        let j=i+2;
        while(j<n&&!blank(lines[j])&&!interrupts(lines,j)){rows.push(splitRow(lines[j]));j++;}
        add({t:'table',align,head,rows});i=j;continue;
      }
      const para=[line.trim()];
      let j=i+1;
      while(j<n&&!blank(lines[j])&&!interrupts(lines,j)){para.push(lines[j].trim());j++;}
      add({t:'p',text:para.join('\n')});i=j;
    }
    return blocks;
  }

  // ---- DOM ----
  function renderMarkdown(text, options={}) {
    const doc=options.document||root.document, video=VIDEO.test(String(options.videoUrl||''))?options.videoUrl:'';
    const element=(tag, content)=>{const e=doc.createElement(tag);if(content!==undefined)e.textContent=content;return e;};
    function inline(nodes, parent) {
      let buffer='';
      const flush=()=>{if(buffer){parent.appendChild(doc.createTextNode(buffer));buffer='';}};
      for(const node of nodes) {
        if(node.k==='text'){buffer+=node.v;continue;}
        if(node.k==='delim'){buffer+=node.ch.repeat(node.n);continue;}
        flush();
        if(node.k==='code')parent.appendChild(element('code',node.v));
        else if(node.k==='em'||node.k==='strong'){const e=element(node.k);inline(node.kids,e);parent.appendChild(e);}
        else if(node.k==='link') {
          const a=element('a');
          a.setAttribute('href',node.href);a.setAttribute('target','_blank');a.setAttribute('rel','noopener noreferrer');
          inline(node.kids,a);parent.appendChild(a);
        } else if(video) {
          const a=element('a',node.label);
          a.setAttribute('href',video+'&t='+node.at);a.setAttribute('target','_blank');a.setAttribute('rel','noopener noreferrer');
          parent.appendChild(a);
        } else buffer+=node.label;
      }
      flush();
    }
    const inlineInto=(source, parent)=>inline(parseInline(source,!!video),parent);
    function block(b, parent) {
      if(b.t==='p'){const e=element('p');inlineInto(b.text,e);parent.appendChild(e);}
      else if(b.t==='h'){const e=element('h'+b.level);inlineInto(b.text,e);parent.appendChild(e);}
      else if(b.t==='hr')parent.appendChild(element('hr'));
      else if(b.t==='code'){const pre=element('pre');pre.appendChild(element('code',b.text));parent.appendChild(pre);}
      else if(b.t==='quote'){const e=element('blockquote');b.kids.forEach(k=>block(k,e));parent.appendChild(e);}
      else if(b.t==='list') {
        const e=element(b.ordered?'ol':'ul');
        if(b.ordered&&b.start!==1)e.setAttribute('start',String(b.start));
        for(const item of b.items) {
          const li=element('li');
          // In a tight list a paragraph is the item's own text; in a loose list it keeps its <p>.
          for(const kid of item) {
            if(kid.t==='p'&&!b.loose)inlineInto(kid.text,li);
            else block(kid,li);
          }
          e.appendChild(li);
        }
        parent.appendChild(e);
      } else if(b.t==='table') {
        const wrap=element('div'), table=element('table'), head=element('thead'), body=element('tbody');
        wrap.setAttribute('class','md-table');
        const row=(cells, tag, into)=>{
          const tr=element('tr');
          b.align.forEach((align,index)=>{
            const cell=element(tag);
            if(align)cell.setAttribute('class','md-'+align);
            inlineInto(cells[index]||'',cell);tr.appendChild(cell);
          });
          into.appendChild(tr);
        };
        row(b.head,'th',head);
        b.rows.forEach(cells=>row(cells,'td',body));
        table.appendChild(head);
        if(b.rows.length)table.appendChild(body);
        wrap.appendChild(table);parent.appendChild(wrap);
      }
    }
    const fragment=doc.createDocumentFragment();
    const lines=String(text).replace(/\r\n?/g,'\n').split('\n').map(l=>l.replace(/^[ \t]+/,ws=>ws.replace(/\t/g,'    ')));
    parseBlocks(lines,0).forEach(b=>block(b,fragment));
    return fragment;
  }
  root.AIVideoTranscriberMarkdown={renderMarkdown};
  if(typeof module!=='undefined'&&module.exports)module.exports={renderMarkdown};
})(globalThis);
