const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const readJson = (relative) => JSON.parse(fs.readFileSync(path.join(root, relative), 'utf8'));
const readText = (relative) => fs.readFileSync(path.join(root, relative), 'utf8');
const exists = (relative) => fs.statSync(path.join(root, relative)).isFile();

function makeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    fire(...args) { return listeners.map((listener) => listener(...args)); }
  };
}

test('Chromium extension manifest points to a valid MV3 service worker', () => {
  const manifest = readJson('browser_extension/manifest.json');
  assert.equal(manifest.manifest_version, 3);
  assert.ok(manifest.background && typeof manifest.background.service_worker === 'string');
  assert.ok(exists(path.posix.join('browser_extension', manifest.background.service_worker)));
  assert.ok(manifest.permissions.includes('nativeMessaging'));
  assert.ok(exists('browser_extension/offscreen.html'));
  assert.ok(exists('browser_extension/offscreen.js'));
  assert.ok(exists('browser_extension/pcm_worklet.js'));
});

test('Firefox extension manifest wires background and content adapters', () => {
  const manifest = readJson('browser_extension/firefox/manifest.json');
  assert.equal(manifest.manifest_version, 2);
  assert.ok(Array.isArray(manifest.background.scripts));
  assert.ok(manifest.background.scripts.includes('background.js'));
  assert.ok(exists('browser_extension/firefox/background.js'));
  assert.ok(manifest.content_scripts.some((entry) => entry.js.includes('content.js')));
  assert.ok(manifest.permissions.includes('nativeMessaging'));
});

test('Firefox background rejects other tabs and forwards accepted PCM to native host', async () => {
  const nativeMessages = [];
  const tabMessages = [];
  const badges = [];
  const click = makeEvent();
  const runtimeConnect = makeEvent();
  const nativeIncoming = makeEvent();
  const nativeDisconnect = makeEvent();
  const native = {
    onMessage: nativeIncoming,
    onDisconnect: nativeDisconnect,
    postMessage(message) {
      nativeMessages.push(message);
      if (message.type === 'start') nativeIncoming.fire({ type: 'ready' });
    },
    disconnect() { nativeDisconnect.fire(); }
  };
  const browser = {
    browserAction: {
      onClicked: click,
      setBadgeText: async ({ text }) => badges.push(text)
    },
    runtime: {
      onConnect: runtimeConnect,
      connectNative: () => native,
      lastError: null
    },
    tabs: {
      onRemoved: makeEvent(),
      sendMessage: async (tabId, message) => tabMessages.push({ tabId, message })
    }
  };
  vm.runInNewContext(readText('browser_extension/firefox/background.js'), {
    browser, console: { error() {} }, setTimeout, clearTimeout
  });

  await Promise.all(click.fire({ id: 7 }));
  assert.deepEqual(nativeMessages[0], { type: 'start', sample_rate: 48000 });
  assert.deepEqual(tabMessages[0], { tabId: 7, message: { type: 'START' } });
  assert.equal(badges.at(-1), 'RX');

  const makePort = (tabId) => ({
    name: 'echocrypt-firefox-pcm',
    sender: { tab: { id: tabId } },
    disconnected: false,
    onMessage: makeEvent(),
    onDisconnect: makeEvent(),
    disconnect() { this.disconnected = true; }
  });
  const unrelated = makePort(8);
  runtimeConnect.fire(unrelated);
  assert.equal(unrelated.disconnected, true);

  const accepted = makePort(7);
  runtimeConnect.fire(accepted);
  accepted.onMessage.fire({ type: 'pcm', pcm: 'AQIDBA==' });
  accepted.onMessage.fire({ type: 'pcm', pcm: 'x'.repeat(12001) });
  assert.deepEqual(nativeMessages[1], {
    type: 'pcm', sample_rate: 48000, pcm_s16le: 'AQIDBA=='
  });
  assert.equal(nativeMessages.filter((message) => message.type === 'pcm').length, 1);

  await Promise.all(click.fire({ id: 7 }));
  assert.ok(tabMessages.some((entry) => entry.message.type === 'STOP'));
  assert.ok(nativeMessages.some((message) => message.type === 'stop'));
  assert.equal(badges.at(-1), '');
});

test('Chromium service worker starts capture, sends stream ID, and stops cleanly', async () => {
  const click = makeEvent();
  const contexts = [];
  const sent = [];
  const badges = [];
  let captures = [];
  const chrome = {
    action: {
      onClicked: click,
      setBadgeText: async ({ text }) => badges.push(text),
      setTitle: async () => {}
    },
    runtime: {
      onMessage: makeEvent(),
      getURL: (relative) => `chrome-extension://test/${relative}`,
      getContexts: async () => contexts.slice(),
      sendMessage: async (message) => { sent.push(message); return { ok: true }; }
    },
    offscreen: {
      createDocument: async () => contexts.push({ contextType: 'OFFSCREEN_DOCUMENT' }),
      closeDocument: async () => { contexts.length = 0; }
    },
    tabCapture: {
      getCapturedTabs: async () => captures,
      getMediaStreamId: async (options) => {
        assert.deepEqual(options, { targetTabId: 42 });
        return 'stream-id-42';
      }
    },
    tabs: { onRemoved: makeEvent() }
  };
  vm.runInNewContext(readText('browser_extension/service_worker.js'), {
    chrome, console: { error() {} }
  });

  await Promise.all(click.fire({ id: 42 }));
  assert.deepEqual(sent[0], {
    target: 'echocrypt-offscreen',
    type: 'START_CAPTURE',
    streamId: 'stream-id-42',
    tabId: 42
  });
  assert.equal(badges.at(-1), 'RX');

  captures = [{ tabId: 42, status: 'active' }];
  await Promise.all(click.fire({ id: 42 }));
  assert.deepEqual(sent[1], { target: 'echocrypt-offscreen', type: 'STOP_CAPTURE' });
  assert.equal(contexts.length, 0);
  assert.equal(badges.at(-1), '');
});

test('Chromium service worker reports failed native capture startup', async () => {
  const click = makeEvent();
  const badges = [];
  const chrome = {
    action: {
      onClicked: click,
      setBadgeText: async ({ text }) => badges.push(text),
      setTitle: async () => {}
    },
    runtime: {
      onMessage: makeEvent(),
      getURL: (relative) => relative,
      getContexts: async () => [],
      sendMessage: async () => ({ ok: false, error: 'native host unavailable' })
    },
    offscreen: { createDocument: async () => {}, closeDocument: async () => {} },
    tabCapture: {
      getCapturedTabs: async () => [],
      getMediaStreamId: async () => 'stream-id'
    },
    tabs: { onRemoved: makeEvent() }
  };
  vm.runInNewContext(readText('browser_extension/service_worker.js'), {
    chrome, console: { error() {} }
  });
  await Promise.all(click.fire({ id: 4 }));
  assert.equal(badges.at(-1), 'ERR');
});
