const NATIVE_HOST = "org.echocrypt.tab_audio";
const PCM_RATE = 48000;

let activeStream = null;
let audioContext = null;
let mediaSource = null;
let pcmNode = null;
let nativePort = null;
let nativeReady = false;
let stopping = false;

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const stride = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += stride) {
    const part = bytes.subarray(offset, Math.min(offset + stride, bytes.length));
    binary += String.fromCharCode(...part);
  }
  return btoa(binary);
}

function reportError(error) {
  const detail = String(error?.message || error || "Unknown capture error");
  chrome.runtime.sendMessage({
    type: "TAB_CAPTURE_ERROR",
    error: detail
  }).catch(() => {});
}

function waitForNativeReady(port, timeoutMs = 7000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error("ECHOCRYPT native host did not become ready."));
      }
    }, timeoutMs);

    port.onMessage.addListener((message) => {
      if (message?.type === "ready" && !settled) {
        settled = true;
        clearTimeout(timeout);
        resolve();
      } else if (message?.type === "error") {
        if (!settled) {
          settled = true;
          clearTimeout(timeout);
          reject(new Error(message.message || "ECHOCRYPT native host failed."));
        } else if (!stopping) {
          const error = new Error(message.message || "ECHOCRYPT native host failed.");
          reportError(error);
          stopCapture();
        }
      }
    });

    port.onDisconnect.addListener(() => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(chrome.runtime.lastError?.message || "ECHOCRYPT native host disconnected."));
      } else if (!stopping) {
        reportError(chrome.runtime.lastError?.message || "ECHOCRYPT native host disconnected.");
        stopCapture();
      }
    });

    port.postMessage({ type: "start", sample_rate: PCM_RATE });
  });
}

async function stopCapture() {
  stopping = true;
  const port = nativePort;
  nativePort = null;
  nativeReady = false;

  if (pcmNode) {
    pcmNode.port.onmessage = null;
    pcmNode.disconnect();
    pcmNode = null;
  }
  if (mediaSource) {
    mediaSource.disconnect();
    mediaSource = null;
  }
  if (activeStream) {
    for (const track of activeStream.getTracks()) {
      track.stop();
    }
    activeStream = null;
  }
  if (audioContext) {
    try {
      await audioContext.close();
    } catch (_) {
      // Context may already have been closed by the browser.
    }
    audioContext = null;
  }

  if (port) {
    try {
      port.postMessage({ type: "stop" });
      await new Promise((resolve) => setTimeout(resolve, 100));
    } catch (_) {
      // Disconnect still causes the native host to release its PCM process.
    }
    try {
      port.disconnect();
    } catch (_) {
      // Already disconnected.
    }
  }
  stopping = false;
}

async function startCapture(streamId) {
  await stopCapture();

  try {
    activeStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId
        }
      },
      video: false
    });

    if (!activeStream.getAudioTracks().length) {
      throw new Error("The selected tab did not expose an audio track.");
    }

    audioContext = new AudioContext({ sampleRate: PCM_RATE });
    await audioContext.resume();
    if (audioContext.sampleRate !== PCM_RATE) {
      throw new Error(
        `The browser created a ${audioContext.sampleRate} Hz audio context; ECHOCRYPT tab capture requires 48000 Hz.`
      );
    }

    await audioContext.audioWorklet.addModule("pcm_worklet.js");

    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    await waitForNativeReady(nativePort);
    nativeReady = true;

    mediaSource = audioContext.createMediaStreamSource(activeStream);
    pcmNode = new AudioWorkletNode(audioContext, "echocrypt-pcm", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [2]
    });

    pcmNode.port.onmessage = (event) => {
      if (!nativeReady || !nativePort || stopping) {
        return;
      }
      try {
        nativePort.postMessage({
          type: "pcm",
          sample_rate: PCM_RATE,
          pcm_s16le: toBase64(event.data)
        });
      } catch (error) {
        reportError(error);
      }
    };

    // The worklet forwards the same tab audio to the usual output while also
    // sending a separate PCM copy to ECHOCRYPT. Other tabs and apps are untouched.
    mediaSource.connect(pcmNode);
    pcmNode.connect(audioContext.destination);
    return { ok: true };
  } catch (error) {
    await stopCapture();
    return { ok: false, error: String(error?.message || error) };
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "echocrypt-offscreen") {
    return false;
  }

  if (message.type === "START_CAPTURE") {
    startCapture(message.streamId)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }

  if (message.type === "STOP_CAPTURE") {
    stopCapture()
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }

  return false;
});
