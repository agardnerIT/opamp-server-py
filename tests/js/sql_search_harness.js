"use strict";
/**
 * Headless behaviour test for the SQL search editor (#132 follow-up).
 *
 * The component is plain JS in a single HTML file with no build step, so there is
 * nothing to import. This harness reads the real `index.html`, runs its `<script>`
 * against a minimal DOM stub, and drives it the way a browser would: set the input,
 * dispatch `input`/`keydown`, deliver `streamlit:render` messages, and read back the
 * popup contents and the `setFrameHeight` messages.
 *
 * It exists because the three defects it pins are only observable at that boundary:
 *
 *  1. Escape closed the popup but kept the tall iframe, leaving a blank band that
 *     pushed the results down the page.
 *  2. The rerun after a search carried the *pre-submit* `value`, and the editor
 *     adopted it, so the box emptied as soon as the query ran.
 *  3. An empty box offered `FROM` at the first position, which cannot compile.
 *
 * Run: node tests/js/sql_search_harness.js   (pytest runs this and skips without node)
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

// --------------------------------------------------------------- DOM stub

class ClassList {
  constructor() { this.set = new Set(); }
  add(name) { this.set.add(name); }
  remove(name) { this.set.delete(name); }
  toggle(name, force) {
    const want = force === undefined ? !this.set.has(name) : !!force;
    if (want) this.set.add(name); else this.set.delete(name);
  }
  contains(name) { return this.set.has(name); }
}

class Element {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.classList = new ClassList();
    this.listeners = {};
    this.attributes = {};
    this.style = { setProperty() {} };
    this.textContent = "";
    this.className = "";
    this._value = "";
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.placeholder = "";
  }
  set innerHTML(_) { this.children = []; }
  get innerHTML() { return ""; }
  set value(next) {
    this._value = String(next);
    // Browsers leave the caret at the end after a programmatic assignment.
    if (this.selectionStart > this._value.length) this.selectionStart = this._value.length;
    this.selectionEnd = this.selectionStart;
  }
  get value() { return this._value; }
  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }
  focus() { this.ownerDocument.activeElement = this; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); }
  dispatch(name, event) {
    const payload = Object.assign({ preventDefault() {}, type: name }, event || {});
    for (const handler of this.listeners[name] || []) handler(payload);
  }
}

function build() {
  const elements = {
    q: new Element("input"),
    pop: new Element("div"),
    label: new Element("div"),
    run: new Element("button"),
    wrap: new Element("div"),
  };
  const body = new Element("body");
  const documentElement = new Element("html");
  const messages = [];
  const document = {
    body,
    documentElement,
    activeElement: body,
    getElementById: (id) => elements[id],
    createElement: (tag) => {
      const element = new Element(tag);
      element.ownerDocument = document;
      return element;
    },
    addEventListener() {},
  };
  for (const element of Object.values(elements)) element.ownerDocument = document;
  const window = {
    parent: { postMessage: (message) => messages.push(JSON.parse(JSON.stringify(message))) },
    addEventListener(name, handler) {
      if (name === "message") (window._messageHandlers = window._messageHandlers || []).push(handler);
    },
    setTimeout: (fn) => setTimeout(fn, 0),
    _messageHandlers: [],
  };
  return { document, window, elements, messages };
}

// ------------------------------------------------------------- load script

const htmlPath = path.resolve(__dirname, "../../ui/components/sql_search/index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) throw new Error("no <script> block in " + htmlPath);

function boot() {
  const env = build();
  const sandbox = {
    document: env.document,
    window: env.window,
    setTimeout: env.window.setTimeout,
    console,
    Date,
    JSON,
    Set,
    String,
    Array,
    Object,
    Math,
    RegExp,
  };
  sandbox.window.document = env.document;
  vm.runInNewContext(scriptMatch[1], sandbox, { filename: "sql_search/index.html" });
  return env;
}

// ------------------------------------------------------------- helpers

const SCHEMA = {
  table: "agents",
  columns: [
    { name: "healthy", sql_name: "healthy", kind: "core", description: "1 healthy, 0 unhealthy", values: [] },
    { name: "type", sql_name: "type", kind: "core", description: "collector kind", values: ["collector"] },
    { name: "host_arch", sql_name: "host_arch", kind: "attribute", description: "description attribute", values: ["amd64"] },
  ],
};

function heights(env) {
  return env.messages.filter((m) => m.type === "streamlit:setFrameHeight").map((m) => m.height);
}
function lastHeight(env) {
  const all = heights(env);
  return all[all.length - 1];
}
function values(env) {
  return env.messages.filter((m) => m.type === "streamlit:setComponentValue").map((m) => m.value);
}
function labels(env) {
  return env.elements.pop.children.map((row) => {
    const parts = row.children.map((child) => child.textContent);
    return parts[1];
  });
}
function kinds(env) {
  return env.elements.pop.children.map((row) => row.children[0].textContent);
}
function activeLabel(env) {
  const row = env.elements.pop.children.find((child) => String(child.className).includes("active"));
  return row ? row.children[1].textContent : null;
}
function popupOpen(env) {
  return env.elements.pop.classList.contains("open");
}
function render(env, args) {
  const message = {
    type: "streamlit:render",
    theme: { base: "light" },
    args: Object.assign({ schema: SCHEMA, label: "", placeholder: "SELECT ...", value: "", revision: 0 }, args || {}),
  };
  for (const handler of env.window._messageHandlers || []) handler({ data: message });
}
function type(env, text, caret) {
  const input = env.elements.q;
  input.value = text;
  input.selectionStart = caret == null ? text.length : caret;
  input.dispatch("input", {});
}
function key(env, name, extra) {
  env.elements.q.dispatch("keydown", Object.assign({ key: name }, extra || {}));
}
function press(env, name, extra) {
  // keydown handlers call preventDefault(); mirror that the browser would then not
  // type the character, which is the case for every key this component handles.
  key(env, name, extra);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
function assertEqual(actual, expected, what) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${what}: expected ${e}, got ${a}`);
}

// ------------------------------------------------------------- scenarios

const results = [];
function scenario(name, fn) {
  try {
    fn();
    results.push(`ok   ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}\n     ${error.message}`);
    process.exitCode = 1;
  }
}

// 1. Escape closes the popup AND gives the height back.
scenario("Escape closes the popup and restores the frame height", () => {
  const env = boot();
  render(env, { value: "" });
  assertEqual(lastHeight(env), 64, "frame height before any typing");
  type(env, "SELECT * FROM agents WHERE ");
  assert(popupOpen(env), "typing opens the popup");
  assert(lastHeight(env) > 64, "an open popup is taller than the box");
  press(env, "Escape");
  assert(!popupOpen(env), "Escape closes the popup");
  assertEqual(lastHeight(env), 64, "Escape collapses the iframe, leaving no blank band");
  // Escape is sticky: a rerun from the page must not reopen the list.
  render(env, { value: "SELECT * FROM agents WHERE ", revision: 0 });
  assert(!popupOpen(env), "a rerun after Escape does not reopen the popup");
  assertEqual(lastHeight(env), 64, "and the height stays collapsed");
  // ... but the operator typing again does reopen it.
  type(env, "SELECT * FROM agents WHERE t");
  assert(popupOpen(env), "typing after Escape reopens the popup");
});

// 2. The submitted query survives the reruns that follow it.
scenario("submitting keeps the query in the box", () => {
  const env = boot();
  render(env, { value: "", revision: 0 });
  type(env, "SELECT * FROM agents WHERE healthy = 1");
  press(env, "Enter");
  const sent = values(env);
  assertEqual(sent.length, 1, "one submit message");
  assertEqual(sent[0].text, "SELECT * FROM agents WHERE healthy = 1", "the submitted text");
  assertEqual(sent[0].submit, true, "submit flag");
  assertEqual(lastHeight(env), 64, "the popup closes on submit");
  assertEqual(env.elements.q.value, "SELECT * FROM agents WHERE healthy = 1", "text survives the submit");

  // The rerun that the submission triggers still carries the *old* value ("") and the
  // old revision. That is the bug: adopting it emptied the box.
  render(env, { value: "", revision: 0 });
  assertEqual(env.elements.q.value, "SELECT * FROM agents WHERE healthy = 1", "a stale rerun does not clear the box");
  assert(!popupOpen(env), "and does not reopen the popup");

  // The page then stores the submission and bumps the revision: the echo is adopted.
  render(env, { value: "SELECT * FROM agents WHERE healthy = 1", revision: 1 });
  assertEqual(env.elements.q.value, "SELECT * FROM agents WHERE healthy = 1", "the stored query is echoed back");
  assert(!popupOpen(env), "the popup stays closed after a run");

  // Operator types again, then an unrelated rerun arrives with the same revision: the
  // half-typed text must not be replaced by what the page still holds.
  type(env, "SELECT * FROM agents WHERE healthy = 0");
  render(env, { value: "SELECT * FROM agents WHERE healthy = 1", revision: 1 });
  assertEqual(env.elements.q.value, "SELECT * FROM agents WHERE healthy = 0", "typing is never clobbered by the page's copy");

  // A deliberate change from the page (new revision) *is* adopted.
  render(env, { value: "SELECT * FROM agents", revision: 2 });
  assertEqual(env.elements.q.value, "SELECT * FROM agents", "a page-initiated change is adopted");
});

// 3. The first word of a statement is SELECT, and only SELECT.
scenario("the empty first position offers SELECT, never FROM", () => {
  const env = boot();
  render(env, { value: "", revision: 0 });
  type(env, "");
  assert(popupOpen(env), "an input event opens the list (this is what a click into an empty box does not do)");
  assertEqual(labels(env), ["SELECT"], "only SELECT can start a statement");
  assert(!labels(env).includes("FROM"), "FROM at position 1 is not a query");
  assert(!kinds(env).includes("column"), "and neither is a bare column");

  // A partial first word filters to the keyword instead of listing columns.
  type(env, "sel");
  assertEqual(labels(env), ["SELECT"], "a partial keyword filters the first position");

  // Typing the whole keyword appends what follows it, and `FROM` appears where it can.
  type(env, "SELECT");
  assert(labels(env).includes("* FROM agents"), "SELECT offers the full first clause");
  type(env, "SELECT ");
  assert(labels(env).includes("*"), "the select list is offered after SELECT");
  type(env, "SELECT * ");
  assertEqual(labels(env), ["FROM"], "FROM is offered exactly where it belongs");
  type(env, "SELECT * FROM ");
  assertEqual(labels(env), ["agents"], "the table name follows FROM");
});

// 4. Mid-statement the list continues the expression instead of restarting it.
scenario("mid-statement keywords continue the expression", () => {
  const env = boot();
  render(env, { value: "", revision: 0 });
  type(env, "SELECT * FROM agents WHERE healthy = 1 ");
  const shown = labels(env);
  assert(shown.includes("AND") && shown.includes("OR"), "AND/OR are offered after a complete condition; got " + JSON.stringify(shown));
  assert(!shown.includes("FROM"), "FROM is not offered once the query has one");
});

// 5. Ctrl+Space reopens the list for the word being edited.
scenario("Ctrl+Space reopens the list on demand", () => {
  const env = boot();
  render(env, { value: "", revision: 0 });
  type(env, "SELECT * FROM agents WHERE hea");
  press(env, "Escape");
  assert(!popupOpen(env), "Escape closed it");
  press(env, " ", { ctrlKey: true });
  assert(popupOpen(env), "Ctrl+Space reopens it");
});

// 6. Tab accepts the active suggestion and keeps the list open for the next word.
scenario("Tab accepts the highlighted suggestion", () => {
  const env = boot();
  render(env, { value: "", revision: 0 });
  type(env, "SELECT");
  press(env, "Tab");
  assertEqual(env.elements.q.value, "SELECT * FROM agents ", "Tab appends the first clause");
  assert(popupOpen(env), "and the next suggestions are shown");

  type(env, "SELECT * FROM agents WHERE ");
  press(env, "ArrowDown");
  press(env, "ArrowDown");
  const chosen = activeLabel(env);
  assert(chosen, "an arrow key highlights a row");
  press(env, "Enter");
  assertEqual(values(env).length, 0, "Enter with a highlighted row does not submit");
  assert(
    env.elements.q.value === "SELECT * FROM agents WHERE " + chosen.trim() + " " ||
      env.elements.q.value.indexOf(chosen.trim()) === 27,
    `Enter accepts the highlighted row (${JSON.stringify(chosen)}) instead of running; got ${JSON.stringify(env.elements.q.value)}`
  );
});

console.log(results.join("\n"));
if (process.exitCode) console.log("\nSome scenarios failed.");
else console.log("\nall scenarios passed");
