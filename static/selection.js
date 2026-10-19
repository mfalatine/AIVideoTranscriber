'use strict';
(function(root){
  // A polling refresh with the same signature must not rebuild transcript DOM: doing so clears browser text selection.
  function shouldRender(previous,next){return previous!==next;}
  // Keeping the comparison in this tiny dependency also makes the selection contract testable without emulating a
  // browser DOM. app.js computes the signature from the video, selected source, view and saved output. A changed source
  // still redraws immediately; only the identical 1.6-second polling refresh is skipped.
  function signature(parts){return JSON.stringify(parts);}
  root.AIVideoTranscriberSelection={shouldRender,signature};
  if(typeof module!=='undefined')module.exports={shouldRender,signature};
})(typeof window==='undefined'?globalThis:window);
