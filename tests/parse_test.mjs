// Unit tests for the pure command parser (no browser needed). Run: node tests/parse_test.mjs
import assert from 'node:assert/strict';
import { parseCommand, toAction, formatObservation, toAnthropic } from '../extension/agent.js';

const act = reply => { const c = parseCommand(reply); return c && (c.name === 'done' ? ['done', c.rest] : toAction(c)); };

// clean replies
assert.deepEqual(act('click 3'), ['click', { ref: 3 }]);
assert.deepEqual(act('goto example.com'), ['navigate', { url: 'https://example.com' }]);
assert.deepEqual(act('goto http://127.0.0.1:9101/x'), ['navigate', { url: 'http://127.0.0.1:9101/x' }]);
assert.deepEqual(act('type 5 | hello world | enter'), ['type', { ref: 5, text: 'hello world', submit: true }]);
assert.deepEqual(act('type 5 | hello world'), ['type', { ref: 5, text: 'hello world', submit: false }]);
assert.deepEqual(act('scroll down'), ['scroll', { direction: 'down' }]);
assert.deepEqual(act('scroll top'), ['scroll', { direction: 'top' }]);
assert.deepEqual(act('key Enter'), ['press_key', { key: 'Enter' }]);
assert.deepEqual(act('select 2 | Red'), ['select', { ref: 2, value: 'Red' }]);
assert.deepEqual(act('done | it worked'), ['done', '| it worked']);
assert.deepEqual(act('back'), ['back', {}]);

// messy replies a small model might produce
assert.deepEqual(act('ACTION: click 7'), ['click', { ref: 7 }]);
assert.deepEqual(act('```\nclick [7]\n```'), ['click', { ref: 7 }]);
assert.deepEqual(act('`click 4`'), ['click', { ref: 4 }]);
assert.deepEqual(act('I should log in first.\nclick 9'), ['click', { ref: 9 }]);
assert.deepEqual(act('1. click 2'), ['click', { ref: 2 }]);
assert.deepEqual(act('CLICK 3'), ['click', { ref: 3 }]);
assert.deepEqual(act('type 5 "hello there"'), ['type', { ref: 5, text: 'hello there', submit: false }]);
assert.deepEqual(act('type 5 | "quoted" | Enter'), ['type', { ref: 5, text: 'quoted', submit: true }]);
assert.deepEqual(act('type [5] | hi'), ['type', { ref: 5, text: 'hi', submit: false }]);

// not commands
assert.equal(parseCommand('I am not sure what to do.'), null);
assert.equal(parseCommand(''), null);
assert.equal(parseCommand('Sure! Here is the answer'), null);

// bad arguments throw a readable error instead of crashing the loop
assert.throws(() => act('click banana'), /element number/);
assert.throws(() => act('type nothing'), /usage/);

// page view formatting
const view = formatObservation({
  title: 'T', url: 'https://x.test/', text: 'hello',
  elements: [
    { ref: 1, tag: 'a', label: 'Home', href: 'https://x.test/home' },
    { ref: 2, tag: 'input', type: 'text', label: 'Name', value: 'Lani' },
    { ref: 3, tag: 'button', label: 'Go', frame: 'ads.test' },
    { ref: 4, tag: 'select', label: 'Color', value: 'r', options: ['Red', 'Blue'] },
  ],
});
assert.match(view, /\[1\] link "Home" -> https:\/\/x\.test\/home/);
assert.match(view, /\[2\] input\(text\) "Name" = "Lani"/);
assert.match(view, /\[3\] button "Go" \(in frame ads\.test\)/);
assert.match(view, /options: Red \| Blue/);
// OpenAI-style history -> Anthropic Messages: roles must alternate and tool results become user blocks
const history = [
  { role: 'system', content: 'be Lani' },
  { role: 'user', content: 'Task: x' },
  { role: 'assistant', content: null, tool_calls: [{ id: 'c1', type: 'function', function: { name: 'click', arguments: '{"ref":3}' } }] },
  { role: 'tool', tool_call_id: 'c1', content: 'clicked "Go"' },
  { role: 'user', content: 'Current page after your actions:\n\nPAGE: T' },
  { role: 'assistant', content: '' },
  { role: 'user', content: 'That was not a valid command.' },
];
const converted = toAnthropic(history);
assert.equal(converted.system, 'be Lani');
assert.deepEqual(converted.messages.map(m => m.role), ['user', 'assistant', 'user', 'assistant', 'user']);
assert.deepEqual(converted.messages[1].content, [{ type: 'tool_use', id: 'c1', name: 'click', input: { ref: 3 } }]);
assert.equal(converted.messages[2].content[0].type, 'tool_result'); // tool result comes before the page text
assert.equal(converted.messages[2].content[1].type, 'text');
assert.equal(converted.messages[3].content[0].text, '(no reply)'); // empty assistant turns are not allowed
assert.ok(converted.messages.every(m => m.content.length > 0));
console.log('parse tests passed');
