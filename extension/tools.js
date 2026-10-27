// Browser tool engine. Imported by the side panel (Lani's agent loop) and by the service worker
// (the optional bridge for external agents), so both drive Chrome the same way.

let currentTabId = null;
const refMaps = new Map(); // tabId -> [{ frameId, ref }], indexed by (global ref - 1)

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
export const setCurrentTab = id => { currentTabId = id; };

const hostOf = url => {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
};

export const describeTab = tab => ({
  tab_id: tab.id,
  window_id: tab.windowId,
  title: tab.title,
  url: tab.url,
  status: tab.status,
  active: tab.active,
});

async function resolveTab(tabId) {
  if (tabId != null) return Number(tabId);
  if (currentTabId != null) {
    try {
      await chrome.tabs.get(currentTabId);
      return currentTabId;
    } catch {
      currentTabId = null; // that tab was closed
    }
  }
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!active) throw new Error('no active tab');
  currentTabId = active.id;
  return active.id;
}

function waitForLoad(tabId, timeoutMs = 15000) {
  return new Promise(resolve => {
    const finish = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === 'complete') finish();
    };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---- page access -------------------------------------------------------------------------------

async function ensureContent(tabId) {
  await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ['content.js'] });
}

// Runs one function from content.js inside a single frame and returns its JSON result.
async function callFrame(tabId, frameId, name, args = {}) {
  const [frame] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (fn, a) => (window.__lani ? window.__lani[fn](a) : { error: 'page script not ready; take a new snapshot' }),
    args: [name, args],
  });
  if (!frame || !frame.result) throw new Error('page returned no result');
  if (frame.result.error) throw new Error(frame.result.error);
  return frame.result;
}

// Snapshots every frame and merges them into one list with page-wide ref numbers.
async function snapshotTab(tabId, { max_text_chars = 6000, max_elements = 200 } = {}) {
  await ensureContent(tabId);
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: a => (window.__lani ? window.__lani.snapshot(a) : null),
    args: [{ max_text_chars }],
  });
  const frames = results
    .filter(r => r.result && !r.result.error)
    .sort((a, b) => a.frameId - b.frameId);

  const refMap = [];
  const elements = [];
  const texts = [];
  let title = '';
  let url = '';
  for (const { frameId, result } of frames) {
    const top = frameId === 0;
    if (top) {
      title = result.title;
      url = result.url;
    }
    if (!top && !result.elements.length && !result.text) continue;
    for (const el of result.elements) {
      if (elements.length >= max_elements) break;
      refMap.push({ frameId, ref: el.ref });
      elements.push({ ...el, ref: refMap.length, ...(top ? {} : { frame: hostOf(result.url) }) });
    }
    if (result.text) texts.push(top ? result.text : `[frame ${hostOf(result.url)}] ${result.text}`);
  }
  refMaps.set(tabId, refMap);

  const text = texts.join('\n\n');
  return {
    tab: describeTab(await chrome.tabs.get(tabId)),
    url,
    title,
    text: text.slice(0, max_text_chars),
    text_truncated: text.length > max_text_chars,
    elements,
  };
}

// Builds an action that targets one element by its page-wide ref.
const elementAction = name => async args => {
  const tabId = await resolveTab(args.tab_id);
  currentTabId = tabId;
  const entry = (refMaps.get(tabId) || [])[Number(args.ref) - 1];
  if (!entry) throw new Error(`ref ${args.ref} not found; take a new snapshot`);
  return callFrame(tabId, entry.frameId, name, { ...args, ref: entry.ref });
};

// Like elementAction, but works without a ref (acts on the top frame).
const pageAction = name => async args => {
  if (args.ref != null) return elementAction(name)(args);
  const tabId = await resolveTab(args.tab_id);
  currentTabId = tabId;
  return callFrame(tabId, 0, name, args);
};

const navigation = method => async ({ tab_id }) => {
  const tabId = await resolveTab(tab_id);
  await chrome.tabs[method](tabId);
  await waitForLoad(tabId);
  return describeTab(await chrome.tabs.get(tabId));
};

// ---- actions -----------------------------------------------------------------------------------

export const ACTIONS = {
  tabs_list: async () => (await chrome.tabs.query({})).map(describeTab),

  tab_open: async ({ url = 'about:blank' }) => {
    const tab = await chrome.tabs.create({ url, active: true });
    currentTabId = tab.id;
    await waitForLoad(tab.id);
    return describeTab(await chrome.tabs.get(tab.id));
  },

  tab_focus: async ({ tab_id }) => {
    const tabId = await resolveTab(tab_id);
    const tab = await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
    currentTabId = tabId;
    return describeTab(tab);
  },

  tab_close: async ({ tab_id }) => {
    const tabId = await resolveTab(tab_id);
    await chrome.tabs.remove(tabId);
    if (currentTabId === tabId) currentTabId = null;
    return { closed: tabId };
  },

  navigate: async ({ url, tab_id }) => {
    if (!url) throw new Error('navigate needs a url');
    const tabId = await resolveTab(tab_id);
    await chrome.tabs.update(tabId, { url, active: true });
    currentTabId = tabId;
    await waitForLoad(tabId);
    return describeTab(await chrome.tabs.get(tabId));
  },

  back: navigation('goBack'),
  forward: navigation('goForward'),
  reload: navigation('reload'),

  snapshot: async ({ tab_id, ...options }) => {
    const tabId = await resolveTab(tab_id);
    currentTabId = tabId;
    return snapshotTab(tabId, options);
  },

  click: elementAction('click'),
  type: elementAction('type'),
  select: elementAction('selectOption'),
  get_text: elementAction('getText'),
  press_key: pageAction('pressKey'),
  scroll: pageAction('scroll'),

  wait_for: async ({ tab_id, text, selector, timeout_ms = 10000 }) => {
    if (!text && !selector) throw new Error('wait_for needs text or selector');
    const tabId = await resolveTab(tab_id);
    currentTabId = tabId;
    const deadline = Date.now() + timeout_ms;
    while (Date.now() < deadline) {
      try {
        await ensureContent(tabId);
        const results = await chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          func: a => (window.__lani ? window.__lani.check(a) : null),
          args: [{ text, selector }],
        });
        if (results.some(r => r.result && r.result.found)) return { found: true };
      } catch {
        // The page may be mid-navigation; try again.
      }
      await sleep(250);
    }
    return { found: false, timed_out: true };
  },

  screenshot: async ({ tab_id }) => {
    const tabId = await resolveTab(tab_id);
    await chrome.tabs.update(tabId, { active: true }); // captureVisibleTab needs the tab on screen
    const tab = await chrome.tabs.get(tabId);
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
    return { tab: describeTab(tab), png_base64: dataUrl.split(',')[1] };
  },

  // Runs JavaScript in the page's own world. Off unless the user enables it in settings.
  eval_js: async ({ code, tab_id }) => {
    const { allowEval = false } = await chrome.storage.local.get('allowEval');
    if (!allowEval) throw new Error('eval_js is turned off; enable "Allow JavaScript execution" in Lani settings');
    const tabId = await resolveTab(tab_id);
    const [frame] = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      args: [code],
      func: async source => {
        try {
          const value = await (0, eval)(source);
          return { value: value === undefined ? null : JSON.parse(JSON.stringify(value)) };
        } catch (err) {
          return { error: String(err) };
        }
      },
    });
    if (!frame || !frame.result) throw new Error('page returned no result');
    if (frame.result.error) throw new Error(frame.result.error);
    return frame.result;
  },
};

export async function runAction(name, args = {}) {
  const handler = Object.hasOwn(ACTIONS, name) ? ACTIONS[name] : null;
  if (!handler) throw new Error(`unknown action "${name}". Valid actions: ${Object.keys(ACTIONS).join(', ')}`);
  return handler(args);
}
