const el = (id) => document.getElementById(id);

const thread = el("thread");
const opening = el("opening");
const composer = el("composer");
const promptBox = el("prompt");
const sendBtn = el("send");
const stopBtn = el("stop");
const modelSelect = el("model");
const thinkBox = el("think");
const statusRow = el("status");
const statusText = el("status-text");

const state = {
  messages: [],
  controller: null,
  health: null,
};

/* ---------- markdown ---------- */

const escapeHtml = (s) =>
  s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function inline(text) {
  // Pull code spans out first so emphasis rules cannot reach inside them.
  // NUL delimits the placeholder because it cannot appear in model output.
  const spans = [];
  const stashed = text.replace(/`([^`]+)`/g, (_, code) => {
    spans.push(code);
    return `\u0000${spans.length - 1}\u0000`;
  });

  const html = escapeHtml(stashed)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );

  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${escapeHtml(spans[i])}</code>`);
}

const BLOCK_START = /^(```|#{1,6}\s|>|\s*([-*+]|\d+\.)\s)/;

function renderMarkdown(src) {
  const lines = src.split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith("```")) {
      const buf = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i += 1;
      out.push(`<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`);
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        let text = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "");
        i += 1;
        while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i])) {
          text += ` ${lines[i++].trim()}`;
        }
        items.push(`<li>${inline(text)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const buf = [];
    while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i])) buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }

  return out.join("");
}

/* Math arrives as LaTeX in almost every answer about a mathematics standard,
   so it is typeset once the turn settles rather than on every frame. */
function typeset(node) {
  if (!window.renderMathInElement) return;
  try {
    window.renderMathInElement(node, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  } catch {
    // Leave the LaTeX source visible; it is still readable as written.
  }
}

/* ---------- turns ---------- */

function addTurn(role, label) {
  opening.hidden = true;

  const turn = document.createElement("article");
  turn.className = "turn";
  turn.dataset.role = role;

  const tag = document.createElement("p");
  tag.className = "turn-label";
  tag.textContent = label;

  const body = document.createElement("div");
  body.className = "turn-body";

  turn.append(tag, body);
  thread.append(turn);
  return body;
}

function atBottom() {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
}

function scrollToEnd() {
  thread.scrollTop = thread.scrollHeight;
}

/* ---------- streaming ---------- */

async function* sseFrames(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      const data = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}

async function send(text) {
  state.messages.push({ role: "user", content: text });

  const userBody = addTurn("user", "You");
  userBody.textContent = text;

  const model = modelSelect.value;
  const body = addTurn("assistant", model || "assistant");
  body.classList.add("is-streaming");
  scrollToEnd();

  setBusy(true);
  state.controller = new AbortController();

  // The answer lives in its own node so repaints cannot collapse an open
  // reasoning panel or disturb the stats line.
  const answerEl = document.createElement("div");
  answerEl.className = "answer";
  body.append(answerEl);

  let answer = "";
  let reasoning = "";
  let reasoningEl = null;
  let pending = false;

  const paint = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      const stick = atBottom();
      answerEl.innerHTML = renderMarkdown(answer);
      if (stick) scrollToEnd();
    });
  };

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: state.messages,
        model,
        think: thinkBox.checked,
      }),
      signal: state.controller.signal,
    });

    if (!response.ok || !response.body) {
      throw new Error(`The server returned HTTP ${response.status}.`);
    }

    for await (const { event, data } of sseFrames(response)) {
      if (event === "token") {
        answer += data.text;
        paint();
      } else if (event === "thinking") {
        reasoning += data.text;
        if (!reasoningEl) {
          reasoningEl = document.createElement("details");
          reasoningEl.className = "reasoning";
          reasoningEl.innerHTML = '<summary>Reasoning</summary><p class="reasoning-text"></p>';
          body.insertBefore(reasoningEl, answerEl);
        }
        reasoningEl.querySelector(".reasoning-text").textContent = reasoning;
      } else if (event === "error") {
        throw new Error(data.message);
      } else if (event === "done") {
        body.classList.remove("is-streaming");
        answerEl.innerHTML = renderMarkdown(answer);
        typeset(answerEl);
        body.append(statsLine(data));
      }
    }

    if (answer.trim()) state.messages.push({ role: "assistant", content: answer });
  } catch (error) {
    if (error.name === "AbortError") {
      if (answer.trim()) state.messages.push({ role: "assistant", content: answer });
    } else {
      const failure = document.createElement("p");
      failure.className = "failure";
      failure.textContent = error.message;
      body.append(failure);
      checkHealth();
    }
  } finally {
    body.classList.remove("is-streaming");
    typeset(answerEl);
    state.controller = null;
    setBusy(false);
    promptBox.focus();
  }
}

function statsLine(stats) {
  const p = document.createElement("p");
  p.className = "meta";
  const parts = [];
  if (stats.response_tokens) parts.push(`${stats.response_tokens} tokens`);
  if (stats.total_seconds) parts.push(`${stats.total_seconds}s`);
  if (stats.tokens_per_second) parts.push(`${stats.tokens_per_second} tokens per second`);
  p.textContent = parts.length ? parts.join(", ") : "";
  return p;
}

function setBusy(busy) {
  sendBtn.hidden = busy;
  stopBtn.hidden = !busy;
  promptBox.disabled = busy;
}

/* ---------- rail data ---------- */

async function checkHealth() {
  try {
    const health = await (await fetch("/api/health")).json();
    state.health = health;
    if (health.ollama_reachable) {
      statusRow.dataset.state = "ok";
      statusText.textContent = `Ollama ${health.version} on ${health.host.replace(/^https?:\/\//, "")}`;
    } else {
      statusRow.dataset.state = "down";
      statusText.textContent = "Ollama is not answering. Start it with: ollama serve";
    }
  } catch {
    statusRow.dataset.state = "down";
    statusText.textContent = "The app server dropped the connection.";
  }
}

async function loadModels() {
  const models = await (await fetch("/api/models")).json();
  const preferred = localStorage.getItem("catpc.model") || state.health?.default_model;

  modelSelect.replaceChildren();
  if (!models.length) {
    modelSelect.append(new Option("No models installed", ""));
    modelSelect.disabled = true;
    sendBtn.disabled = true;
    return;
  }

  const local = models.filter((m) => !m.is_cloud);
  const cloud = models.filter((m) => m.is_cloud);

  const fill = (list, label) => {
    if (!list.length) return;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const m of list) {
      const size = m.size_bytes ? ` (${(m.size_bytes / 1e9).toFixed(1)} GB)` : "";
      group.append(new Option(m.name + (m.is_cloud ? "" : size), m.name));
    }
    modelSelect.append(group);
  };

  fill(local, "On this machine");
  fill(cloud, "Ollama cloud");

  if (models.some((m) => m.name === preferred)) modelSelect.value = preferred;
  else modelSelect.value = local[0]?.name ?? models[0].name;
}

async function loadCorpus() {
  const ledger = el("ledger");
  const caveat = el("caveat");
  try {
    const data = await (await fetch("/api/corpus")).json();
    if (data.error) throw new Error(data.error);

    const rows = data.subjects
      .map(
        (s) => `<tr data-subject="${escapeHtml(s.subject)}">
          <td>${escapeHtml(s.subject)}</td>
          <td>${s.total}</td>
          <td>${s.essential}</td>
          <td>${s.courses}</td>
        </tr>`
      )
      .join("");

    ledger.innerHTML = `<table>
      <thead><tr><th>Subject</th><th>Standards</th><th>Essential</th><th>Courses</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td>Total</td><td>${data.total}</td><td>${data.essential}</td><td></td></tr></tfoot>
    </table>`;
    ledger.setAttribute("aria-busy", "false");

    caveat.textContent =
      "General chat answers from the model alone. Open the lesson planner to select " +
      "standards, generate a source-grounded draft, and save its context and checks.";
  } catch (error) {
    ledger.innerHTML = `<p class="ledger-error">The standards index did not load. ${escapeHtml(
      error.message
    )}</p>`;
  }
}

/* ---------- events ---------- */

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = promptBox.value.trim();
  if (!text || state.controller) return;
  promptBox.value = "";
  resize();
  send(text);
});

promptBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

const resize = () => {
  promptBox.style.height = "auto";
  // scrollHeight excludes borders that border-box sizing counts, so without
  // this the box lands two pixels short and grows a scrollbar.
  const borders = promptBox.offsetHeight - promptBox.clientHeight;
  promptBox.style.height = `${promptBox.scrollHeight + borders}px`;
};

promptBox.addEventListener("input", resize);

stopBtn.addEventListener("click", () => state.controller?.abort());

modelSelect.addEventListener("change", () => localStorage.setItem("catpc.model", modelSelect.value));

el("clear").addEventListener("click", () => {
  state.controller?.abort();
  state.messages = [];
  thread.querySelectorAll(".turn").forEach((turn) => turn.remove());
  opening.hidden = false;
  promptBox.focus();
});

// Health lands first: the model list falls back to the server's default model.
checkHealth().then(loadModels);
loadCorpus();
promptBox.focus();
