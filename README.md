# Lani for Chrome

A Chrome extension (Manifest V3) that lets an agent operate your browser — from inside the browser, as a
side-panel assistant driven by a small local model, or from outside it, over a loopback HTTP bridge that
**any** model can call. Click the toolbar icon and a side panel opens. Tell it what to do in the page you
have open and it reads the page, picks an action, runs it, and repeats until it can answer.

Built by BFTS. Lani is a small model, so the design leans toward small models — which also happens to be
what makes the tool surface cheap enough for any agent to hold in context:

- **Compact page view.** Each turn the model sees a short list of numbered elements plus a little page text,
  for example `[3] button "Sign in"`, not raw HTML or a screenshot.
- **One-line commands.** `click 3`, `type 5 | hello | enter`, `goto example.com`, `done | the answer`.
  The parser tolerates prose, code fences and quoting, which small models add.
- **Automatic page refresh.** After every action the extension appends a fresh page view, so the model never
  has to remember to look. Older views shrink to a stub to keep the context small.
- **Iframes work.** Elements inside cross-origin iframes get page-wide numbers like everything else.
- **Traces for training.** Each run can be downloaded as JSONL: what the model saw, what it said, what ran.
- **18 browser tools** — tabs, navigate, snapshot, click, type, select, keys, scroll, read text, wait,
  screenshot, and an opt-in `eval_js`. The same 18 in both directions, whether the model sits in the panel
  or is a process on the same machine.

Models that support OpenAI-style function calling can use **Tool calling** reply style instead.

## Install

1. Chrome: `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → choose the `extension/` folder.
2. Pin the Lani icon, click it, then **Settings**: set the server URL (an OpenAI-compatible
   `/v1` endpoint, for example llama-server), the model name, and the reply style.
3. The dot next to the name turns green when the model server is reachable.

Requires Chrome 116 or newer (the side-panel API). No build step, no dependencies: the extension is plain
ES modules, and the bridge is Python 3 standard library only.

## Settings

| Setting | What it does |
|---|---|
| Server URL, model name, API key | Any OpenAI-compatible chat endpoint, or the Anthropic Messages API. |
| Reply style | *One-line commands* for small models, *Tool calling* for larger ones. |
| Allow JavaScript execution | Off by default. Lets a model run `eval_js` in the page. |
| Keep a trace | Adds a **Download trace** button after each run. |
| Connect to the bridge | Lets outside agents use the same browser tools. See below. |

## Drive it from any agent (the bridge)

`bridge/server.py` is a loopback-only HTTP server. The extension polls it; you hand commands to it.
Whatever speaks HTTP can drive the tab — Claude Code, an OpenAI-compatible agent of your own, or curl.

```bash
python3 bridge/server.py            # http://127.0.0.1:8765, token written to ~/.config/chrome-tab-bridge/token
```

Then in Lani's settings tick **Connect to the bridge** and paste that token; the extension starts polling.

- `GET /tools` — the 18 tools as OpenAI-style function schemas. Point a new agent at that one endpoint and
  it has the whole browser, no hand-written tool definitions.
- `POST /cmd` — `{"action": "click", "args": {"ref": 3}, "timeout_s": 60}` → `{"id", "ok", "result"|"error"}`.
- `GET /status` — `{"extension_connected", "last_poll_age_s", "queued"}`. Ask this before blaming your agent.
- `GET /cmd/<id>` — what became of a command you stopped waiting on (see *Delivery* below).

The extension side is `extension/background.js`: a service worker that long-polls, **acks before running**,
and is woken by a `chrome.alarms` keepalive, because Chrome suspends idle service workers and drops their
pending poll.

### Claude Code over MCP

`bridge/mcp_server.py` is a stdlib JSON-RPC-over-stdio MCP server. Add it to `~/.claude.json`:

```json
{
  "mcpServers": {
    "chrome-tab-bridge": { "command": "python3", "args": ["/path/to/lani-chrome/bridge/mcp_server.py"] }
  }
}
```

Tools then appear as `mcp__chrome-tab-bridge__*`. The bridge must already be running.

`bridge/agent.py` is the same idea without MCP: a minimal tool-calling loop against any OpenAI-compatible
endpoint, for trying the tools out from a terminal (`LLM_BASE_URL=… LLM_MODEL=… python3 bridge/agent.py
"open example.com and read the title"`; it needs the `openai` package, the only third-party dependency here).

### Delivery, and why a timeout is not a failure

Chrome can suspend the worker between the poll and the action, so the bridge is deliberately **at-most-once
once acknowledged**. That means a caller whose wait expired cannot tell "it never ran" from "it ran and I
missed the reply", and retrying a click in that doubt runs it twice — the failure that costs real money on
a site with a Checkout button. So the bridge keeps recent outcomes and says which state you are in:

| the bridge says | what happened | retry? |
|---|---|---|
| `never reached the extension` | nothing ran | yes, safely |
| `handed to the extension but never acknowledged` | most likely did not run | check `GET /cmd/<id>` |
| `WAS ACKNOWLEDGED` | the action may well have run | **no** — read `GET /cmd/<id>` first |

A result that lands after the caller gave up is kept and handed back rather than discarded, and
`mcp_server.py` goes and fetches it instead of reporting a timeout. `tests/bridge_reconnect_test.py` pins
all of this without a browser.

## Safety

- Lani acts inside your real Chrome profile, with your logins. Stop a run with the **Stop** button.
- The bridge binds loopback only and refuses to start on any other address — it can drive your browser, so
  it will not listen on your LAN. Every request needs the bearer token, and any request carrying a
  non-`chrome-extension://` `Origin` is refused, so ordinary web pages cannot reach it.
- Page text is untrusted. The system prompts tell the model to ignore instructions found on pages, but a small
  model may still follow them. Watch what it does on sites that matter.
- Clicks and key presses are synthetic events (`isTrusted` is false). Most sites accept them; some do not.
- No file upload, drag and drop, or hover. Scrolling moves the window only.

## Layout

```
extension/   the MV3 extension: service worker, side panel, the 18 tools (tools.js, tools.json)
bridge/      server.py (loopback HTTP), mcp_server.py (MCP for Claude Code), agent.py (plain loop)
tests/       parser, bridge delivery, Playwright end-to-end, screenshot harness
tools/       icon generation
art/         the icon art
```

## Tests

```bash
node tests/parse_test.mjs                # command parser, no browser needed
python3 tests/bridge_reconnect_test.py   # bridge never loses a command to a dead poll, no browser needed
python3 tests/e2e.py                     # loads the extension in Chromium; needs Playwright (see below)
python3 tests/screenshot.py DIR          # screenshots the side panel, light and dark
```

`e2e.py` runs the side-panel agent in both reply styles against a scripted fake model, clicks inside a
cross-origin iframe, and exercises the bridge, the eval gate, screenshots and error handling. Set `LANI_CHROME`
to a Chromium that can load extensions (Playwright's Chromium works; branded Chrome 137+ ignores
`--load-extension`).

## Icon

The icon is a stargazer lily made with Flux schnell. `tools/flux_icon.py` generates candidates through a ComfyUI
instance, and `tools/build_icons.py` cuts the chosen art in `art/` into the four Chrome sizes.
`art/lani-hibiscus.png` is the runner-up: `python3 tools/build_icons.py --src art/lani-hibiscus.png`.

## License

MIT — see [LICENSE](LICENSE).
