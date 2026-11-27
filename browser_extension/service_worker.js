const OFFSCREEN_URL = "offscreen.html";
const OFFSCREEN_TARGET = "echocrypt-offscreen";

async function ensureOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_URL)]
  });

  if (contexts.length === 0) {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: ["USER_MEDIA", "AUDIO_PLAYBACK"],
      justification:
        "Capture audio from the user-selected tab and pass it through to the normal speakers."
    });
  }
}

async function setBadge(text, title) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setTitle({ title });
}

async function stopSelectedTabCapture() {
  try {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ["OFFSCREEN_DOCUMENT"],
      documentUrls: [chrome.runtime.getURL(OFFSCREEN_URL)]
    });

    if (contexts.length > 0) {
      await chrome.runtime.sendMessage({
        target: OFFSCREEN_TARGET,
        type: "STOP_CAPTURE"
      });
      await chrome.offscreen.closeDocument();
    }
  } finally {
    await setBadge("", "ECHOCRYPT: capture or stop this tab");
  }
}

async function startSelectedTabCapture(tab) {
  if (!tab || typeof tab.id !== "number") {
    throw new Error("Could not identify the active browser tab.");
  }

  // This call occurs directly from the extension action click. Chrome requires
  // a user gesture before issuing a tab-capture stream ID.
  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tab.id
  });

  await ensureOffscreenDocument();
  const response = await chrome.runtime.sendMessage({
    target: OFFSCREEN_TARGET,
    type: "START_CAPTURE",
    streamId,
    tabId: tab.id
  });

  if (!response || response.ok !== true) {
    throw new Error(response?.error || "ECHOCRYPT could not start tab capture.");
  }

  await setBadge("RX", "ECHOCRYPT is capturing this tab. Click again to stop.");
  return response;
}

chrome.action.onClicked.addListener(async (tab) => {
  try {
    const captures = await chrome.tabCapture.getCapturedTabs();
    const activeCapture = captures.find((item) =>
      item.status === "active" || item.status === "pending"
    );

    if (activeCapture) {
      await stopSelectedTabCapture();
      if (activeCapture.tabId === tab.id) {
        return;
      }
    }

    await startSelectedTabCapture(tab);
  } catch (error) {
    console.error("ECHOCRYPT tab capture:", error);
    await setBadge("ERR", "ECHOCRYPT tab capture failed. Check the native host and audio setup.");
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  try {
    const captures = await chrome.tabCapture.getCapturedTabs();
    if (captures.some((item) => item.tabId === tabId)) {
      await stopSelectedTabCapture();
    }
  } catch (error) {
    console.error("ECHOCRYPT capture cleanup:", error);
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "TAB_CAPTURE_ERROR") {
    console.error("ECHOCRYPT tab capture:", message.error);
    setBadge("ERR", message.error || "ECHOCRYPT tab capture stopped unexpectedly.");
  }
});
