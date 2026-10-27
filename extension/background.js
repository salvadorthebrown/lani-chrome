// Service worker. Opens Lani's side panel from the toolbar button and, when enabled in settings,
// serves as the bridge client that lets external agents (for example Claude Code through the
// MCP server in ../bridge) drive Chrome with the same tools Lani uses.
import { runAction } from './tools.js';

const BRIDGE_DEFAULTS = { bridgeUrl: 'http://127.0.0.1:8765', bridgeToken: '', bridgeEnabled: false };
let polling = false;

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const getConfig = async () => ({ ...BRIDGE_DEFAULTS, ...(await chrome.storage.local.get(BRIDGE_DEFAULTS)) });
const authHeaders = cfg => ({ Authorization: `Bearer ${cfg.bridgeToken}` });

chrome.sidePanel?.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

async function pollLoop() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const cfg = await getConfig();
      if (!cfg.bridgeEnabled || !cfg.bridgeToken) return; // the alarm restarts us once it is enabled
      try {
        // The bridge holds a poll for 20s; the timeout keeps a wedged request from stalling the loop.
        const res = await fetch(`${cfg.bridgeUrl}/ext/poll?ack=1`, {
          headers: authHeaders(cfg), signal: AbortSignal.timeout(28000),
        });
        if (res.status === 401) {
          console.warn('[lani] bridge rejected the token');
          await sleep(5000);
        } else if (res.status === 200) {
          const cmd = await res.json();
          // Ack first: an unacked command is handed to the next poll, in case this worker is suspended.
          await post(cfg, '/ext/ack', { id: cmd.id }).catch(() => {});
          runCommand(cfg, cmd); // not awaited: commands may run concurrently
        }
        // 204 means nothing was queued; poll again immediately.
      } catch {
        await sleep(2000); // bridge is not running
      }
    }
  } finally {
    polling = false;
  }
}

async function runCommand(cfg, cmd) {
  let reply;
  try {
    reply = { id: cmd.id, ok: true, result: await runAction(cmd.action, cmd.args || {}) };
  } catch (err) {
    reply = { id: cmd.id, ok: false, error: err?.message || String(err) };
  }
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await post(cfg, '/ext/result', reply);
      return;
    } catch (err) {
      if (attempt === 3) console.warn('[lani] could not post result', err);
      else await sleep(500 * attempt);
    }
  }
}

async function post(cfg, path, body) {
  const res = await fetch(`${cfg.bridgeUrl}${path}`, {
    method: 'POST',
    headers: { ...authHeaders(cfg), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) throw new Error(`bridge answered HTTP ${res.status}`);
}

// Alarms wake the service worker if Chrome suspended it; pollLoop is a no-op while already running.
chrome.alarms.create('keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => pollLoop());
chrome.storage.onChanged.addListener(() => pollLoop());
chrome.runtime.onStartup.addListener(() => pollLoop());
chrome.runtime.onInstalled.addListener(() => pollLoop());
pollLoop();
