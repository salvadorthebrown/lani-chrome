import { DEFAULTS, runTask } from './agent.js';

const $ = id => document.getElementById(id);
const log = $('log');

let settings = { ...DEFAULTS };
let controller = null;
let lastTrace = null;
let lastTask = '';

// ---- settings ------------------------------------------------------------------------------------

const fields = [...document.querySelectorAll('[data-key]')];

async function loadSettings() {
  settings = { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) };
  for (const el of fields) {
    const value = settings[el.dataset.key];
    if (el.type === 'checkbox') el.checked = Boolean(value);
    else el.value = value;
  }
}

for (const el of fields) {
  el.addEventListener('change', async () => {
    const key = el.dataset.key;
    let value = el.type === 'checkbox' ? el.checked : el.value;
    if (el.type === 'number') value = Number(value) || DEFAULTS[key];
    if (typeof value === 'string') value = value.trim();
    settings[key] = value;
    await chrome.storage.local.set({ [key]: value });
    if (key === 'baseUrl' || key === 'apiKey' || key === 'api') checkModelServer();
  });
}

$('gear').addEventListener('click', () => {
  const panel = $('settings');
  panel.hidden = !panel.hidden;
  $('gear').setAttribute('aria-expanded', String(!panel.hidden));
});

async function checkModelServer() {
  const dot = $('dot');
  const anthropic = settings.api === 'anthropic';
  const trimmed = settings.baseUrl.replace(/\/+$/, '');
  const base = anthropic ? trimmed.replace(/\/v1$/, '') : trimmed;
  try {
    const res = await fetch(anthropic ? base : `${base}/models`, {
      headers: settings.apiKey ? { Authorization: `Bearer ${settings.apiKey}` } : {},
      signal: AbortSignal.timeout(3000),
    });
    // An Anthropic-style server may not serve GET /, so any answer at all means it is up.
    const up = anthropic || res.ok;
    dot.className = up ? 'dot ok' : 'dot bad';
    dot.title = up ? 'Model server reachable' : `Model server answered HTTP ${res.status}`;
  } catch {
    dot.className = 'dot bad';
    dot.title = 'Model server not reachable';
  }
}

// ---- transcript ----------------------------------------------------------------------------------

function add(className, text) {
  log.querySelector('.empty')?.remove();
  const el = document.createElement('div');
  el.className = className;
  el.textContent = text;
  log.append(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function addStep(command, result) {
  log.querySelector('.empty')?.remove();
  const el = document.createElement('div');
  el.className = 'step';
  const code = document.createElement('code');
  code.textContent = command;
  const out = document.createElement('span');
  out.textContent = result;
  el.append(code, out);
  log.append(el);
  log.scrollTop = log.scrollHeight;
}

function setRunning(running) {
  $('send').hidden = running;
  $('stop').hidden = !running;
  $('task').disabled = running;
}

function handleEvent(event) {
  log.querySelector('.thinking')?.remove();
  if (event.type === 'start') {
    const chip = $('chip');
    chip.hidden = false;
    chip.textContent = `Working in: ${event.tab.title || event.tab.url}`;
  } else if (event.type === 'thinking') {
    add('thinking', 'Thinking...');
  } else if (event.type === 'step') {
    addStep(event.command, event.result);
  } else if (event.type === 'final') {
    add('bubble final', event.text);
  }
}

async function send() {
  const task = $('task').value.trim();
  if (!task || controller) return;
  $('task').value = '';
  lastTask = task;
  lastTrace = null;
  $('trace').hidden = true;
  add('bubble user', task);

  controller = new AbortController();
  setRunning(true);
  try {
    lastTrace = await runTask({ task, settings, signal: controller.signal, emit: handleEvent });
  } catch (err) {
    log.querySelector('.thinking')?.remove();
    if (err.name === 'AbortError') add('note', 'Stopped.');
    else add('bubble error', err.message);
  } finally {
    controller = null;
    setRunning(false);
    $('trace').hidden = !(settings.recordTraces && lastTrace?.length);
    $('task').focus();
  }
}

$('send').addEventListener('click', send);
$('stop').addEventListener('click', () => controller?.abort());
$('task').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

// Each line of the download is one step: what the model saw, what it said, what ran.
$('trace').addEventListener('click', () => {
  const header = { task: lastTask, model: settings.model, mode: settings.mode, recorded: new Date().toISOString() };
  const body = [header, ...lastTrace].map(row => JSON.stringify(row)).join('\n');
  const url = URL.createObjectURL(new Blob([body], { type: 'application/x-ndjson' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `lani-trace-${Date.now()}.jsonl`;
  link.click();
  URL.revokeObjectURL(url);
});

await loadSettings();
checkModelServer();
