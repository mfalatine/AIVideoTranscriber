(function(root) {
  'use strict';
  // Read aloud: the browser's own voices read the text of the open view, and the word being spoken is highlighted.
  // Nothing leaves the computer except what the chosen voice itself needs (an "Online" voice is Microsoft's or Google's service).
  const BLOCKS='div,p,li,h1,h2,h3,h4,h5,h6,blockquote,pre,tr,td,th';
  const SKIP='.timestamp,.exchange-meta,.conversation-empty,[hidden],script,style';
  const SPACE=/\s/;
  // Pieces the speech engine reads reliably (some voices stop partway through a very long utterance): split at the
  // last sentence end or line break inside max characters, else at the last space. Returns [{start,end}] in order.
  function chunks(text, max=220) {
    const out=[];let start=0;
    while(start<text.length) {
      while(start<text.length && SPACE.test(text[start]))start++;
      if(start>=text.length)break;
      let end=Math.min(text.length,start+max);
      if(end<text.length) {
        const slice=text.slice(start,end), sentence=/[.!?…]["'”’)\]]*(?=\s)|\n/g;let cut=-1,match;
        while((match=sentence.exec(slice)))cut=match.index+match[0].length;
        if(cut<max*0.3){const space=slice.lastIndexOf(' ');cut=space>0?space:slice.length;}
        end=start+cut;
      }
      out.push({start,end});start=end;
    }
    return out;
  }
  // The word at a position (or the next word when the position is a space), as [start,end).
  function wordAt(text, index) {
    let start=Math.max(0,Math.min(index,text.length));
    while(start<text.length && SPACE.test(text[start]))start++;
    let end=start;
    while(start>0 && !SPACE.test(text[start-1]))start--;
    while(end<text.length && !SPACE.test(text[end]))end++;
    return [start,end];
  }
  // "Microsoft Aria Online (Natural) - English (United States)" -> "Aria (Natural) · en-US".
  function voiceLabel(voice) {
    const name=String(voice?.name||'').replace(/^Microsoft\s+/,'').replace(/\s+-\s+.*$/,'').replace(/\s+Online\b/,'').trim();
    return voice?.lang?`${name} · ${voice.lang}`:name;
  }
  // The voices for the transcript's language (all voices when none match), natural-sounding ones first.
  function voicesFor(voices, lang='en') {
    const list=[...(voices||[])], base=String(lang||'en').slice(0,2).toLowerCase();
    const same=list.filter(voice=>String(voice.lang||'').toLowerCase().startsWith(base));
    const rank=voice=>(/natural/i.test(voice.name)?0:1);
    return (same.length?same:list).sort((a,b)=>rank(a)-rank(b)||String(a.name).localeCompare(String(b.name)));
  }
  function pickVoice(voices, lang='en', saved='') {
    const list=voicesFor(voices,lang), all=voices||[];
    return all.find(voice=>voice.name===saved && list.includes(voice))
      || list.find(voice=>/natural/i.test(voice.name) && /en-US/i.test(voice.lang))
      || list.find(voice=>/natural/i.test(voice.name)) || list.find(voice=>voice.default) || list[0] || null;
  }
  // The text of an element as it is read, with each character's place in the page so the spoken word can be marked.
  function textMap(element) {
    const walker=element.ownerDocument.createTreeWalker(element,4), nodes=[], nodeOf=[], offsetOf=[];
    let text='', block=null, node;
    while((node=walker.nextNode())) {
      const parent=node.parentElement, value=node.nodeValue;
      if(!parent || !value || parent.closest(SKIP))continue;
      const own=parent.closest(BLOCKS);
      if(own!==block && text && !SPACE.test(text[text.length-1])){text+='\n';nodeOf.push(-1);offsetOf.push(-1);}
      block=own;
      const index=nodes.push(node)-1;
      for(let i=0;i<value.length;i++){nodeOf.push(index);offsetOf.push(i);}
      text+=value;
    }
    return {element,text,nodes,nodeOf,offsetOf};
  }
  function createReader(options) {
    const {synth,Utterance,highlights,Highlight}=options;
    const notify=options.notify||(()=>{}), onChange=options.onChange||(()=>{}), now=options.now||(()=>Date.now());
    let view=null, map=null, parts=[], part=0, position=0, status='idle', timer=0, heard=false, rate=1, voice=null, token=0;
    const setStatus=value=>{status=value;onChange(value);};
    function rangeFor(start, end) {
      while(start<end && map.nodeOf[start]<0)start++;
      let last=end-1;
      while(last>=start && map.nodeOf[last]<0)last--;
      if(last<start)return null;
      const range=map.element.ownerDocument.createRange();
      range.setStart(map.nodes[map.nodeOf[start]],map.offsetOf[start]);
      range.setEnd(map.nodes[map.nodeOf[last]],map.offsetOf[last]+1);
      return range;
    }
    // Reading starts at the first line showing in the box, so scrolling down and pressing Read aloud starts there.
    function firstVisible() {
      if(!(map.element.scrollTop>0))return 0;
      const top=map.element.getBoundingClientRect().top, length=map.text.length;
      const rectAt=index=>{
        while(index<length && (map.nodeOf[index]<0 || SPACE.test(map.text[index])))index++;
        return index<length?rangeFor(index,index+1).getBoundingClientRect():null;
      };
      let low=0, high=length;
      while(low<high){const middle=(low+high)>>1, rect=rectAt(middle);if(rect && rect.bottom<=top+2)low=middle+1;else high=middle;}
      return wordAt(map.text,low)[0];
    }
    // The view was redrawn (same text, new page nodes): map it again; if the text itself changed, stop.
    function fresh() {
      if(map.element.isConnected && (!map.nodes.length || map.nodes[0].isConnected))return true;
      const next=options.getView?.();
      if(!next || next.key!==view.key){stop();return false;}
      const again=textMap(next.root);
      if(again.text!==map.text){stop();notify('The text changed, so reading stopped.');return false;}
      map=again;view=next;return true;
    }
    function follow(range) {
      const box=map.element, word=range.getBoundingClientRect(), frame=box.getBoundingClientRect();
      if(word.height && (word.top<frame.top || word.bottom>frame.bottom-8))box.scrollTop+=word.top-frame.top-frame.height/3;
    }
    function show(index, length) {
      if(!fresh())return;
      const [start,end]=length>0?[index,index+length]:wordAt(map.text,index);
      if(end<=start)return;
      position=start;
      const range=rangeFor(start,end);if(!range)return;
      if(highlights && Highlight)highlights.set('read-aloud',new Highlight(range));
      follow(range);
    }
    // Voices that report no word positions still get a moving mark, timed from an average speaking speed.
    function guess(run, piece, began) {
      clearTimeout(timer);
      timer=setTimeout(function tick(){
        if(run!==token || heard)return;
        show(Math.min(piece.end-1,piece.start+Math.floor((now()-began)/1000*15*rate)),0);
        timer=setTimeout(tick,250);
      },700);
    }
    function speakNext(run) {
      if(run!==token)return;
      if(part>=parts.length){finish();return;}
      const piece=parts[part], utterance=new Utterance(map.text.slice(piece.start,piece.end));
      utterance.rate=rate;if(voice){utterance.voice=voice;utterance.lang=voice.lang;}
      heard=false;
      utterance.onstart=()=>{if(run!==token)return;show(piece.start,0);guess(run,piece,now());};
      utterance.onboundary=event=>{
        if(run!==token || (event.name && event.name!=='word'))return;
        heard=true;clearTimeout(timer);show(piece.start+event.charIndex,event.charLength||0);
      };
      utterance.onend=()=>{if(run!==token)return;clearTimeout(timer);part++;speakNext(run);};
      utterance.onerror=event=>{
        if(run!==token || ['interrupted','canceled'].includes(event.error))return;
        stop();notify(`Read aloud stopped: ${event.error||'the voice failed'}. Try another voice.`,true);
      };
      synth.speak(utterance);
    }
    function speakFrom(start) {
      const run=++token;synth.cancel();clearTimeout(timer);
      position=start;part=0;
      parts=chunks(map.text.slice(start)).map(piece=>({start:piece.start+start,end:piece.end+start}));
      if(!parts.length){finish();return;}
      setStatus('playing');speakNext(run);
    }
    function halt(){token++;clearTimeout(timer);synth.cancel();}
    function stop(){halt();highlights?.delete('read-aloud');if(status!=='idle')setStatus('idle');}
    function finish(){stop();}
    function start() {
      const next=options.getView?.();
      const built=next?textMap(next.root):null;
      if(!built || !built.text.trim()){notify('There is nothing to read on this screen.');return;}
      view=next;map=built;speakFrom(firstVisible());
    }
    return {
      get status(){return status;},
      toggle(){status==='idle'?start():stop();},
      start, stop,
      pause(){if(status!=='playing')return;halt();setStatus('paused');},
      resume(){if(status!=='paused' || !fresh())return;speakFrom(position);},
      setRate(value){rate=Number(value)||1;if(status==='playing' && fresh())speakFrom(position);},
      setVoice(value){voice=value||null;if(status==='playing' && fresh())speakFrom(position);},
      // The open video or view changed: reading belongs to what was on screen, so it stops.
      viewChanged(key){if(status!=='idle' && view && key!==view.key)stop();},
    };
  }
  const api={chunks,wordAt,voiceLabel,voicesFor,pickVoice,textMap,createReader};
  root.AIVideoTranscriberReadAloud=api;
  if(typeof module!=='undefined' && module.exports)module.exports=api;
})(globalThis);
