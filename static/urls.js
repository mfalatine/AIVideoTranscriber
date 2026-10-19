(function(root) {
  'use strict';
  // Browser copy of providers.video_id and of the line handling in Application.add_batch, so the Add videos
  // panel can show what will happen before anything is sent. The server stays the final validator.
  // The URL is split the way Python's urllib.parse.urlsplit does it (not with the browser's URL class), so
  // the same links are accepted and rejected. tests/video_link_cases.json is checked against both.
  const LIMIT=100;
  const ID=/^[A-Za-z0-9_-]{11}$/;
  const SPACE='\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'; // Python's str.isspace()
  const STRIP=new RegExp('^['+SPACE+']+|['+SPACE+']+$','g');
  const LINES=/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/; // Python's str.splitlines()
  const strip=text=>text.replace(STRIP,'');
  const HOSTS=['youtu.be','www.youtu.be'];
  const YOUTUBE=['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'];
  const NOT_URL='Enter a YouTube video URL or 11-character video ID.';
  const NOT_VIDEO='Use individual YouTube video links. Playlist expansion is not included yet.';

  function unquote(text) {
    const plain=text.replace(/\+/g,' ');
    try{return decodeURIComponent(plain);}catch{return plain;}
  }
  function firstValue(query, wanted) {
    for(const pair of query.split('&')) {
      const at=pair.indexOf('='), name=at<0?pair:pair.slice(0,at), value=at<0?'':pair.slice(at+1);
      if(value&&unquote(name)===wanted)return unquote(value);
    }
    return '';
  }
  // urlsplit(): scheme, netloc, path, query. Throws like it does for an invalid IPv6 host.
  function split(value) {
    let url=value.replace(/^[\u0000- ]+/,'').replace(/[\t\r\n]/g,'');
    let scheme='', netloc='', query='';
    const colon=url.indexOf(':');
    if(colon>0&&/^[A-Za-z]/.test(url)&&/^[A-Za-z0-9+.-]*$/.test(url.slice(0,colon))){scheme=url.slice(0,colon).toLowerCase();url=url.slice(colon+1);}
    if(url.startsWith('//')) {
      let end=url.length;
      for(const delimiter of '/?#') {
        const found=url.indexOf(delimiter,2);
        if(found>=0)end=Math.min(end,found);
      }
      netloc=url.slice(2,end);url=url.slice(end);
      // Python 3.12 accepts a bracketed host only when it is a valid IPv6 address, which is never a YouTube host.
      if(netloc.includes('[')||netloc.includes(']'))throw new Error('Invalid IPv6 URL');
    }
    if(!/^[\u0000-\u007f]*$/.test(netloc)) {
      const plain=netloc.replace(/[@:#?]/g,''), folded=plain.normalize('NFKC');
      if(plain!==folded&&/[\/?#@:]/.test(folded))throw new Error('Invalid netloc');
    }
    const hash=url.indexOf('#');
    if(hash>=0)url=url.slice(0,hash);
    const mark=url.indexOf('?');
    if(mark>=0){query=url.slice(mark+1);url=url.slice(0,mark);}
    return {scheme,netloc,path:url,query};
  }
  function videoId(input) {
    let value=strip(String(input));
    if(ID.test(value))return value;
    if(!value.includes('://'))value='https://'+value;
    const parsed=split(value);
    const at=parsed.netloc.lastIndexOf('@');
    if(at>=0) {
      const info=parsed.netloc.slice(0,at), colon=info.indexOf(':');
      const username=colon<0?info:info.slice(0,colon), password=colon<0?'':info.slice(colon+1);
      if(username||password)throw new Error(NOT_URL);
    }
    if(parsed.scheme!=='https'&&parsed.scheme!=='http')throw new Error(NOT_URL);
    const host=parsed.netloc.slice(at+1).split(':')[0].toLowerCase();
    const parts=parsed.path.replace(/^\/+|\/+$/g,'').split('/');
    let result='';
    if(HOSTS.includes(host))result=parts[0];
    else if(YOUTUBE.includes(host)) {
      if(parsed.path.replace(/\/+$/,'')==='/watch')result=firstValue(parsed.query,'v');
      else if(parts.length===2&&['shorts','live','embed'].includes(parts[0]))result=parts[1];
    }
    if(!ID.test(result))throw new Error(NOT_VIDEO);
    return result;
  }
  // The lines Application.add_batch would see, each marked 'ready' (first time its video appears), 'duplicate' or 'invalid'.
  function parseLinks(text) {
    const lines=String(text).split(LINES).map(strip).filter(Boolean), seen=new Set(), entries=[];
    for(const line of lines) {
      let id=null;
      try{id=videoId(line);}catch{id=null;}
      if(id===null)entries.push({line,status:'invalid',id:null});
      else if(seen.has(id))entries.push({line,status:'duplicate',id});
      else{seen.add(id);entries.push({line,status:'ready',id});}
    }
    return {entries,ready:seen.size,lines:lines.length,limit:LIMIT,tooMany:lines.length>LIMIT};
  }
  root.AIVideoTranscriberUrls={videoId,parseLinks,LIMIT};
  if(typeof module!=='undefined'&&module.exports)module.exports={videoId,parseLinks,LIMIT};
})(globalThis);
