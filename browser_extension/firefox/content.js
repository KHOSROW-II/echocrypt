(() => {
 if (window.__echocryptLoaded) return; window.__echocryptLoaded=true;
 let ctx=null, node=null, silent=null, port=null, observer=null, active=false;
 const seen=new WeakSet();
 function b64(buf){let s="";const a=new Uint8Array(buf);for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));return btoa(s);}
 function attach(el){
  if(!active||seen.has(el))return; seen.add(el);
  let stream; try { stream=el.captureStream?el.captureStream():el.mozCaptureStream?.(); } catch(_){return;}
  if(!stream)return;
  const connect=()=>{if(active&&stream.getAudioTracks().length){try{ctx.createMediaStreamSource(stream).connect(node);}catch(e){console.error(e);}}};
  if(stream.getAudioTracks().length)connect(); else {const timer=setInterval(()=>{if(!active||!el.isConnected){clearInterval(timer);return;}if(stream.getAudioTracks().length){clearInterval(timer);connect();}},250);setTimeout(()=>clearInterval(timer),12000);}
 }
 async function start(){
  if(active)return;active=true;port=browser.runtime.connect({name:"echocrypt-pcm"});
  ctx=new AudioContext({sampleRate:48000});await ctx.resume();
  if(ctx.sampleRate!==48000)throw Error("48 kHz AudioContext unavailable");
  await ctx.audioWorklet.addModule(browser.runtime.getURL("pcm_worklet.js"));
  node=new AudioWorkletNode(ctx,"echocrypt-firefox-pcm",{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
  silent=ctx.createGain();silent.gain.value=0;node.connect(silent);silent.connect(ctx.destination);
  node.port.onmessage=e=>{if(active&&port)port.postMessage({type:"pcm",pcm:b64(e.data)});};
  const scan=()=>document.querySelectorAll("audio,video").forEach(attach);scan();
  observer=new MutationObserver(scan);observer.observe(document.documentElement,{childList:true,subtree:true});
  if(!document.querySelector("audio,video"))throw Error("No capturable HTML audio/video media element found in this tab");
 }
 async function stop(){active=false;if(observer)observer.disconnect();if(node)node.disconnect();if(silent)silent.disconnect();if(ctx)await ctx.close().catch(()=>{});if(port)port.disconnect();ctx=node=silent=port=observer=null;}
 browser.runtime.onMessage.addListener(m=>{if(m.type==="START")start().catch(e=>{console.error(e);stop();});if(m.type==="STOP")stop();});
 browser.runtime.onConnect.addListener(p=>{if(p.name==="echocrypt-firefox-pcm")p.onDisconnect.addListener(()=>{});});
})();
