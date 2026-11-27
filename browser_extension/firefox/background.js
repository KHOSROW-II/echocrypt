const HOST = "org.echocrypt.tab_audio";
let tabId = null, native = null, bridge = null;
function badge(text) { return browser.browserAction.setBadgeText({text}); }
async function stop() {
  if (tabId !== null) browser.tabs.sendMessage(tabId,{type:"STOP"}).catch(()=>{});
  if (bridge) { try { bridge.disconnect(); } catch (_) {} bridge=null; }
  if (native) { try { native.postMessage({type:"stop"}); native.disconnect(); } catch (_) {} native=null; }
  tabId=null; await badge("");
}
browser.browserAction.onClicked.addListener(async tab => {
  if (tabId !== null) { const same=tabId===tab.id; await stop(); if(same)return; }
  try {
    native=browser.runtime.connectNative(HOST);
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error("Native host timeout")),7000);
      native.onMessage.addListener(m=>{if(m.type==="ready"){clearTimeout(timer);resolve();}else if(m.type==="error")reject(new Error(m.message));});
      native.onDisconnect.addListener(()=>reject(new Error(browser.runtime.lastError?.message||"Native host disconnected")));
      native.postMessage({type:"start",sample_rate:48000});
    });
    tabId=tab.id;
    await browser.tabs.sendMessage(tab.id,{type:"START"});
    await badge("RX");
  } catch(e) { await stop(); console.error(e); await badge("ERR"); }
});
browser.runtime.onConnect.addListener(port=>{
  if(port.name!=="echocrypt-firefox-pcm")return;
  if(tabId===null||port.sender?.tab?.id!==tabId||bridge!==null){port.disconnect();return;}
  bridge=port;
  port.onMessage.addListener(m=>{
    if(tabId===port.sender?.tab?.id&&native&&m.type==="pcm"&&typeof m.pcm==="string"&&m.pcm.length<=12000)
      native.postMessage({type:"pcm",sample_rate:48000,pcm_s16le:m.pcm});
  });
  port.onDisconnect.addListener(()=>{if(bridge===port)bridge=null;});
});
browser.tabs.onRemoved.addListener(id=>{if(id===tabId)stop();});
