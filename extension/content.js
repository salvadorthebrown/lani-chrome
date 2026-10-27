// Content script, run in every frame of the page being driven. Each export takes one object
// argument and returns plain JSON, or { error } when something goes wrong. Loading it twice is harmless.
(() => {
  if (window.__lani) return;

  const INTERACTIVE = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
    '[role="tab"]', '[role="menuitem"]', '[role="option"]', '[role="switch"]',
    '[role="textbox"]', '[role="combobox"]', '[contenteditable="true"]', '[onclick]',
  ].join(',');
  const MAX_ELEMENTS = 300;
  const KEY_CODES = {
    Enter: 13, Escape: 27, Tab: 9, Backspace: 8, ' ': 32,
    ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  };

  const clean = (s, max = 120) => (s || '').replace(/\s+/g, ' ').trim().slice(0, max);

  function isVisible(el) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function labelFor(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (clean(text)) return clean(text);
    }
    if (el.labels && el.labels.length) {
      const text = [...el.labels].map(label => label.innerText).join(' ');
      if (clean(text)) return clean(text);
    }
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type) && el.value) return clean(el.value);
    if (el.tagName === 'IMG') return clean(el.alt);
    return clean(el.innerText || el.placeholder || el.title || el.name);
  }

  function byRef(ref) {
    const el = document.querySelector(`[data-lani-ref="${Number(ref)}"]`);
    if (!el) throw new Error(`ref ${ref} not found; take a new snapshot`);
    return el;
  }

  function fireKey(el, key) {
    const keyCode = KEY_CODES[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    const init = { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, keyCode, which: keyCode, bubbles: true, cancelable: true };
    for (const type of ['keydown', 'keypress', 'keyup']) el.dispatchEvent(new KeyboardEvent(type, init));
  }

  // Lists this frame's visible interactive elements (refs are local to the frame) plus its text.
  function snapshot({ max_text_chars = 6000 } = {}) {
    document.querySelectorAll('[data-lani-ref]').forEach(el => el.removeAttribute('data-lani-ref'));
    const elements = [];
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (elements.length >= MAX_ELEMENTS) break;
      if (!isVisible(el)) continue;
      const ref = elements.length + 1;
      el.setAttribute('data-lani-ref', String(ref));
      const entry = { ref, tag: el.tagName.toLowerCase(), label: labelFor(el) };
      if (el.getAttribute('role')) entry.role = el.getAttribute('role');
      if (el.tagName === 'A') entry.href = el.href;
      if (el.tagName === 'INPUT') {
        entry.type = el.type;
        if (el.type === 'checkbox' || el.type === 'radio') entry.checked = el.checked;
        else if (el.type !== 'password') entry.value = clean(el.value, 80);
      }
      if (el.tagName === 'TEXTAREA') entry.value = clean(el.value, 80);
      if (el.tagName === 'SELECT') {
        entry.value = clean(el.value, 80);
        entry.options = [...el.options].slice(0, 20).map(option => clean(option.text, 60));
      }
      if (el.isContentEditable) entry.editable = true;
      if (el.disabled) entry.disabled = true;
      elements.push(entry);
    }
    const text = (document.body?.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
    return {
      url: location.href,
      title: document.title,
      is_top: window === window.top,
      text: text.slice(0, max_text_chars),
      text_truncated: text.length > max_text_chars,
      elements,
    };
  }

  function click({ ref }) {
    const el = byRef(ref);
    el.scrollIntoView({ block: 'center' });
    el.focus();
    el.click();
    return { clicked: labelFor(el), url: location.href };
  }

  function type({ ref, text, clear = true, submit = false }) {
    const el = byRef(ref);
    el.scrollIntoView({ block: 'center' });
    el.focus();
    if (el.isContentEditable) {
      if (clear) document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, text);
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      // The native setter lets React/Vue-controlled inputs see the change.
      const proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      const setValue = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setValue.call(el, clear ? text : el.value + text);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      throw new Error(`ref ${ref} is not a text field`);
    }
    if (submit) {
      fireKey(el, 'Enter');
      if (el.form) el.form.requestSubmit();
    }
    return { typed: true, length: (el.value ?? el.innerText ?? '').length };
  }

  function selectOption({ ref, value }) {
    const el = byRef(ref);
    if (el.tagName !== 'SELECT') throw new Error(`ref ${ref} is not a <select>`);
    const wanted = String(value);
    const index = [...el.options].findIndex(option =>
      option.value === wanted || clean(option.text, 200) === clean(wanted, 200));
    if (index < 0) throw new Error(`no option matching "${value}"`);
    el.selectedIndex = index;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { selected: clean(el.options[index].text) };
  }

  // Synthetic key events: many sites accept them, but they are not trusted input.
  function pressKey({ key, ref }) {
    let el = document.activeElement || document.body;
    if (ref != null) {
      el = byRef(ref);
      el.focus();
    }
    fireKey(el, key);
    return { pressed: key };
  }

  // Scrolls the window only; scrollable inner containers are not handled.
  function scroll({ direction = 'down', amount = 600 }) {
    if (direction === 'top') window.scrollTo(0, 0);
    else if (direction === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);
    else window.scrollBy(0, direction === 'up' ? -amount : amount);
    return {
      scrollY: Math.round(window.scrollY),
      maxScrollY: document.documentElement.scrollHeight - window.innerHeight,
    };
  }

  function getText({ ref }) {
    const el = byRef(ref);
    return { text: (el.innerText || el.value || '').trim().slice(0, 4000) };
  }

  function check({ text, selector }) {
    if (selector) return { found: document.querySelector(selector) !== null };
    if (text) return { found: (document.body?.innerText || '').includes(text) };
    throw new Error('wait_for needs text or selector');
  }

  const safe = fn => args => {
    try {
      return fn(args || {});
    } catch (err) {
      return { error: err.message || String(err) };
    }
  };

  window.__lani = {
    snapshot: safe(snapshot),
    click: safe(click),
    type: safe(type),
    selectOption: safe(selectOption),
    pressKey: safe(pressKey),
    scroll: safe(scroll),
    getText: safe(getText),
    check: safe(check),
  };
})();
