/**
 * Claude Chat panel — vanilla JS custom element, no build step.
 * Markdown rendering uses marked.min.js (vendored alongside this file).
 */

import "./marked.min.js";

const STYLES = `
  :host {
    display: flex;
    height: 100%;
    width: 100%;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, "Roboto", sans-serif);
  }

  /* ===== Sidebar ===== */
  .sidebar {
    width: 280px;
    border-right: 1px solid var(--divider-color);
    display: flex;
    flex-direction: column;
    background: var(--card-background-color);
    flex-shrink: 0;
    z-index: 2;
  }
  .sidebar-backdrop {
    display: none;
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 1;
  }
  .menu-toggle {
    display: none;
    background: transparent;
    border: none;
    color: inherit;
    padding: 4px 8px;
    cursor: pointer;
    font-size: 22px;
    line-height: 1;
  }
  .sidebar header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--divider-color);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .sidebar header h2 {
    margin: 0;
    flex: 1;
    font-size: 16px;
    font-weight: 500;
  }
  .session-list { flex: 1; overflow-y: auto; padding: 8px; }
  .session {
    padding: 10px 12px;
    border-radius: 8px;
    cursor: pointer;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .session:hover { background: var(--secondary-background-color); }
  .session.active { background: var(--primary-color); color: white; }
  .session.active .session-meta { color: rgba(255,255,255,0.8); }
  .session-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session-meta { font-size: 11px; color: var(--secondary-text-color); }
  .session-pending-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--warning-color, #ff9800);
    flex-shrink: 0;
  }
  .session-delete {
    background: none; border: none; cursor: pointer; color: inherit;
    opacity: 0; padding: 0 4px; font-size: 16px;
  }
  .session:hover .session-delete { opacity: 0.6; }
  .session-delete:hover { opacity: 1; }
  button.primary {
    background: var(--primary-color);
    color: white;
    border: none;
    padding: 8px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  button.primary:hover { opacity: 0.9; }

  /* ===== Main column ===== */
  .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .chat-header {
    padding: 12px 24px;
    border-bottom: 1px solid var(--divider-color);
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .chat-header h1 { margin: 0; font-size: 16px; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .model-picker {
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    border: 1px solid var(--divider-color);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    cursor: pointer;
  }

  /* ===== Messages ===== */
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .empty-state {
    margin: auto;
    text-align: center;
    color: var(--secondary-text-color);
    max-width: 460px;
    line-height: 1.6;
  }
  .empty-state h2 { font-weight: 400; margin-bottom: 8px; }
  .empty-state .examples {
    margin-top: 16px;
    display: grid;
    gap: 8px;
  }
  .empty-state .example {
    background: var(--secondary-background-color);
    padding: 10px 14px;
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    border: 1px solid var(--divider-color);
  }
  .empty-state .example:hover { border-color: var(--primary-color); }

  .message-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .message-row.user { align-items: flex-end; }
  .message-row.assistant { align-items: flex-start; }

  .message {
    max-width: 80%;
    padding: 12px 16px;
    border-radius: 12px;
    line-height: 1.55;
    word-wrap: break-word;
  }
  .message.user {
    background: var(--primary-color);
    color: white;
    white-space: pre-wrap;
  }
  .message.assistant {
    background: var(--secondary-background-color);
  }
  /* Markdown styles inside assistant bubbles */
  .message.assistant > *:first-child { margin-top: 0; }
  .message.assistant > *:last-child { margin-bottom: 0; }
  .message.assistant p { margin: 0 0 8px; }
  .message.assistant h1, .message.assistant h2, .message.assistant h3 {
    margin: 12px 0 6px; font-weight: 600;
  }
  .message.assistant h1 { font-size: 18px; }
  .message.assistant h2 { font-size: 16px; }
  .message.assistant h3 { font-size: 14px; }
  .message.assistant ul, .message.assistant ol { margin: 4px 0 8px; padding-left: 22px; }
  .message.assistant li { margin: 2px 0; }
  .message.assistant code {
    background: rgba(0,0,0,0.2);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: var(--code-font-family, monospace);
    font-size: 0.9em;
  }
  .message.assistant pre {
    background: var(--code-editor-background-color, rgba(0,0,0,0.3));
    color: var(--primary-text-color);
    padding: 10px 12px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 12px;
    margin: 8px 0;
  }
  .message.assistant pre code { background: none; padding: 0; }
  .message.assistant table {
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 13px;
    width: 100%;
  }
  .message.assistant th, .message.assistant td {
    border: 1px solid var(--divider-color);
    padding: 6px 10px;
    text-align: left;
  }
  .message.assistant th {
    background: rgba(0,0,0,0.15);
    font-weight: 600;
  }
  .message.assistant a { color: var(--primary-color); }
  .message.assistant blockquote {
    border-left: 3px solid var(--divider-color);
    padding-left: 12px;
    color: var(--secondary-text-color);
    margin: 8px 0;
  }

  /* ===== Tool calls ===== */
  .tool-call {
    align-self: flex-start;
    max-width: 100%;
    background: var(--secondary-background-color);
    border-left: 3px solid var(--accent-color, #ff9800);
    border-radius: 6px;
    overflow: hidden;
  }
  .tool-call > summary {
    list-style: none;
    cursor: pointer;
    padding: 8px 14px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--secondary-text-color);
    font-family: var(--code-font-family, monospace);
    user-select: none;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .tool-call > summary::-webkit-details-marker { display: none; }
  .tool-call > summary::before {
    content: "▶";
    font-size: 9px;
    transition: transform 0.15s;
    color: var(--secondary-text-color);
    flex-shrink: 0;
  }
  .tool-call[open] > summary::before { transform: rotate(90deg); }
  .tool-call .body {
    padding: 0 14px 10px 14px;
    font-size: 11px;
    color: var(--secondary-text-color);
    font-family: var(--code-font-family, monospace);
  }
  .tool-call .body pre {
    background: rgba(0,0,0,0.25);
    padding: 8px;
    border-radius: 4px;
    margin: 4px 0;
    overflow-x: auto;
    max-height: 240px;
    overflow-y: auto;
    color: var(--primary-text-color);
  }

  /* ===== Pending changes ===== */
  .pending-card {
    align-self: stretch;
    border: 1px solid var(--warning-color, #ff9800);
    border-radius: 8px;
    padding: 16px;
    background: var(--card-background-color);
  }
  .pending-card h3 {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--warning-color, #ff9800);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .pending-card p { margin: 0 0 12px; font-size: 13px; }
  .pending-card .diff {
    background: var(--code-editor-background-color, #1e1e1e);
    color: #d4d4d4;
    padding: 12px;
    border-radius: 6px;
    font-family: var(--code-font-family, monospace);
    font-size: 12px;
    max-height: 320px;
    overflow: auto;
    margin-bottom: 12px;
    white-space: pre;
  }
  .pending-card .diff .add { color: #4ec9b0; }
  .pending-card .diff .del { color: #f48771; }
  .pending-card .diff .hunk { color: #569cd6; }
  .pending-card .service-payload {
    background: var(--code-editor-background-color, #1e1e1e);
    color: #d4d4d4;
    padding: 10px;
    border-radius: 6px;
    font-family: var(--code-font-family, monospace);
    font-size: 12px;
    margin-bottom: 12px;
    white-space: pre-wrap;
  }
  .pending-card .actions { display: flex; gap: 8px; }
  .pending-card .reject {
    background: transparent;
    color: var(--primary-text-color);
    border: 1px solid var(--divider-color);
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
  }
  .pending-card.superseded {
    border-color: var(--divider-color);
    opacity: 0.6;
  }
  .pending-card.superseded h3 {
    color: var(--secondary-text-color);
  }
  .pending-card.superseded .actions { display: none; }
  .pending-card .superseded-label {
    font-size: 12px;
    color: var(--secondary-text-color);
    font-style: italic;
    margin-top: 4px;
  }

  /* ===== Composer ===== */
  .composer {
    border-top: 1px solid var(--divider-color);
    padding: 16px 24px;
    display: flex;
    gap: 8px;
  }
  .composer textarea {
    flex: 1;
    border: 1px solid var(--divider-color);
    border-radius: 8px;
    padding: 10px 12px;
    font-family: inherit;
    font-size: 14px;
    resize: none;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    min-height: 44px;
    max-height: 200px;
  }
  .composer button.send,
  .composer button.stop {
    background: var(--primary-color);
    color: white;
    border: none;
    border-radius: 8px;
    padding: 0 20px;
    cursor: pointer;
    min-width: 90px;
  }
  .composer button.stop { background: var(--error-color, #f44336); }
  .composer button:disabled { opacity: 0.5; cursor: not-allowed; }

  .typing-indicator {
    align-self: flex-start;
    font-size: 12px;
    color: var(--secondary-text-color);
    font-style: italic;
    padding: 4px 0;
  }

  .hint-banner {
    background: var(--warning-color, #ff9800);
    color: #000;
    padding: 8px 24px;
    font-size: 13px;
    line-height: 1.4;
    border-bottom: 1px solid var(--divider-color);
    white-space: pre-wrap;
  }
  .hint-banner code {
    background: rgba(0,0,0,0.15);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: var(--code-font-family, monospace);
  }

  /* ===== Mobile / narrow viewport ===== */
  @media (max-width: 720px) {
    :host { position: relative; overflow: hidden; }
    .menu-toggle { display: inline-block; }
    .sidebar {
      position: absolute;
      top: 0; bottom: 0; left: 0;
      transform: translateX(-100%);
      transition: transform 0.2s ease;
      box-shadow: 2px 0 12px rgba(0,0,0,0.3);
    }
    :host([data-sidebar-open]) .sidebar { transform: translateX(0); }
    :host([data-sidebar-open]) .sidebar-backdrop { display: block; }
    .chat-header { padding: 10px 14px; gap: 8px; }
    .chat-header h1 { font-size: 15px; }
    .model-picker { font-size: 12px; padding: 5px 6px; max-width: 130px; }
    .messages { padding: 14px; gap: 12px; }
    .message { max-width: 92%; padding: 10px 14px; }
    .tool-call { max-width: 100%; }
    .pending-card { padding: 12px; }
    .pending-card .actions { flex-wrap: wrap; }
    .composer { padding: 10px 14px; gap: 6px; }
    .composer textarea { font-size: 16px; /* prevent iOS zoom */ }
    .composer button.send,
    .composer button.stop { padding: 0 14px; min-width: 0; }
    .empty-state { padding: 0 12px; }
    .empty-state .examples { gap: 6px; }
    .empty-state .example { font-size: 13px; padding: 8px 10px; }
    .hint-banner { padding: 8px 14px; font-size: 12px; }
  }
`;

class ClaudeChatPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._sessions = [];
    this._activeSessionId = null;
    this._activeSession = null;
    this._models = [];
    this._selectedModel = null;
    this._isStreaming = false;
    this._streamingMessageEl = null;
    this._currentStreamingText = "";
    this._unsubStream = null;
    // Tool calls created during the current streaming turn — keyed by id.
    this._streamingToolCalls = {};
    this._diagnostics = null;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first) {
      this._render();
      this._loadModels();
      this._loadDiagnostics();
      this._loadSessions();
    }
  }
  get hass() { return this._hass; }

  set narrow(_v) {}
  set route(_v) {}
  set panel(_v) {}

  connectedCallback() { if (!this.shadowRoot.firstChild) this._render(); }

  // --- API helpers ---------------------------------------------------------

  async _send(type, payload = {}) {
    return this._hass.connection.sendMessagePromise({ type, ...payload });
  }

  async _loadModels() {
    try {
      const res = await this._send("claude_chat/list_models");
      this._models = res.models || [];
      this._selectedModel =
        localStorage.getItem("claude_chat:model") || res.default || this._models[0]?.id;
      this._renderHeader();
    } catch (err) {
      console.warn("Could not load models", err);
    }
  }

  async _loadDiagnostics() {
    try {
      this._diagnostics = await this._send("claude_chat/diagnostics");
      this._renderHintBanner();
    } catch (err) {
      console.warn("Could not load diagnostics", err);
    }
  }

  async _loadSessions() {
    try {
      const res = await this._send("claude_chat/list_sessions");
      this._sessions = res.sessions || [];
      if (!this._activeSessionId && this._sessions.length > 0) {
        await this._selectSession(this._sessions[0].id);
      } else {
        this._renderSidebar();
      }
    } catch (err) {
      this._showError("Could not load sessions: " + err.message);
    }
  }

  async _createSession() {
    const res = await this._send("claude_chat/create_session", { title: "New chat" });
    await this._loadSessions();
    await this._selectSession(res.id);
  }

  async _deleteSession(id, event) {
    event.stopPropagation();
    if (!confirm("Delete this chat?")) return;
    await this._send("claude_chat/delete_session", { session_id: id });
    if (this._activeSessionId === id) {
      this._activeSessionId = null;
      this._activeSession = null;
    }
    await this._loadSessions();
  }

  async _selectSession(id) {
    this._activeSessionId = id;
    const res = await this._send("claude_chat/get_session", { session_id: id });
    this._activeSession = res;
    this._renderSidebar();
    this._renderHeader();
    this._renderChat();
    this._restoreDraft();
    this._closeSidebar();
  }

  async _approveChange(changeId) {
    try {
      await this._send("claude_chat/approve_change", {
        session_id: this._activeSessionId,
        change_id: changeId,
      });
      await this._selectSession(this._activeSessionId);
    } catch (err) {
      alert("Failed to apply change: " + err.message);
    }
  }

  async _rejectChange(changeId) {
    await this._send("claude_chat/reject_change", {
      session_id: this._activeSessionId,
      change_id: changeId,
    });
    await this._selectSession(this._activeSessionId);
  }

  // --- streaming chat ------------------------------------------------------

  async _sendMessage(textOverride) {
    const textarea = this.shadowRoot.querySelector("textarea");
    const text = (textOverride ?? textarea.value).trim();
    if (!text || this._isStreaming) return;
    if (!this._activeSessionId) await this._createSession();

    if (!textOverride) textarea.value = "";
    this._clearDraft();
    this._isStreaming = true;
    this._currentStreamingText = "";
    this._streamingToolCalls = {};

    this._activeSession.messages.push({
      role: "user",
      content: [{ type: "text", text }],
      created_at: Date.now() / 1000,
    });
    this._renderChat();
    this._setComposerStreaming(true);
    this._appendTypingIndicator();

    try {
      this._unsubStream = await this._hass.connection.subscribeMessage(
        (event) => this._onStreamEvent(event),
        {
          type: "claude_chat/send_message",
          session_id: this._activeSessionId,
          text,
          model: this._selectedModel,
        }
      );
    } catch (err) {
      this._showError("Send failed: " + err.message);
      this._finishStreaming();
    }
  }

  _stop() {
    if (!this._isStreaming) return;
    if (this._unsubStream) {
      try { this._unsubStream(); } catch (_) {}
      this._unsubStream = null;
    }
    this._finishStreaming();
    // Re-fetch the session so the server-side state (which kept appending) shows up.
    if (this._activeSessionId) this._selectSession(this._activeSessionId);
  }

  _onStreamEvent(event) {
    switch (event.type) {
      case "text_delta":
        this._appendStreamingText(event.text);
        break;
      case "tool_use_start":
        this._removeTypingIndicator();
        this._appendToolCall(event.name, event.id);
        break;
      case "tool_result":
        this._completeToolCall(event.id, event.result);
        this._appendTypingIndicator();
        break;
      case "turn_complete":
        break;
      case "done":
        this._activeSession = event.session;
        this._finishStreaming();
        this._renderChat();
        this._renderSidebar();
        this._renderHeader();
        break;
      case "error":
        this._showError(event.error);
        this._finishStreaming();
        break;
    }
  }

  _finishStreaming() {
    this._isStreaming = false;
    this._streamingMessageEl = null;
    this._streamingToolCalls = {};
    this._removeTypingIndicator();
    this._setComposerStreaming(false);
    if (this._unsubStream) {
      try { this._unsubStream(); } catch (_) {}
      this._unsubStream = null;
    }
  }

  _setComposerStreaming(streaming) {
    const composer = this.shadowRoot.querySelector(".composer");
    if (!composer) return;
    composer.querySelector(".send").style.display = streaming ? "none" : "";
    composer.querySelector(".stop").style.display = streaming ? "" : "none";
    const ta = composer.querySelector("textarea");
    ta.disabled = streaming;
    if (!streaming) ta.focus();
  }

  _appendStreamingText(text) {
    this._removeTypingIndicator();
    if (!this._streamingMessageEl) {
      const messages = this.shadowRoot.querySelector(".messages");
      const row = document.createElement("div");
      row.className = "message-row assistant";
      this._streamingMessageEl = document.createElement("div");
      this._streamingMessageEl.className = "message assistant";
      row.appendChild(this._streamingMessageEl);
      messages.appendChild(row);
    }
    this._currentStreamingText += text;
    // Render markdown live as it streams.
    this._streamingMessageEl.innerHTML = renderMarkdown(this._currentStreamingText);
    this._scrollToBottom();
  }

  _appendToolCall(name, id) {
    const messages = this.shadowRoot.querySelector(".messages");
    const el = document.createElement("details");
    el.className = "tool-call";
    el.innerHTML = `
      <summary>🔧 ${escapeHtml(name)} <span class="status">…</span></summary>
      <div class="body"><em>running…</em></div>
    `;
    messages.appendChild(el);
    this._streamingToolCalls[id] = el;
    // Reset streaming bubble — text after a tool call goes into a new bubble.
    this._streamingMessageEl = null;
    this._currentStreamingText = "";
    this._scrollToBottom();
  }

  _completeToolCall(id, result) {
    const el = this._streamingToolCalls[id];
    if (!el) return;
    const ok = !result?.error;
    const status = el.querySelector(".status");
    if (status) {
      status.textContent = ok ? "✓" : "✗";
      status.style.color = ok ? "var(--success-color, #4caf50)" : "var(--error-color, #f44336)";
    }
    const body = el.querySelector(".body");
    if (body) {
      body.innerHTML = `<pre>${escapeHtml(JSON.stringify(result, null, 2)).slice(0, 8000)}</pre>`;
    }
  }

  _appendTypingIndicator() {
    if (this.shadowRoot.querySelector(".typing-indicator")) return;
    const messages = this.shadowRoot.querySelector(".messages");
    const el = document.createElement("div");
    el.className = "typing-indicator";
    el.textContent = "Claude is thinking…";
    messages.appendChild(el);
    this._scrollToBottom();
  }

  _removeTypingIndicator() {
    const el = this.shadowRoot.querySelector(".typing-indicator");
    if (el) el.remove();
  }

  _scrollToBottom() {
    const messages = this.shadowRoot.querySelector(".messages");
    if (messages) messages.scrollTop = messages.scrollHeight;
  }

  _showError(text) {
    const messages = this.shadowRoot.querySelector(".messages");
    if (!messages) return;
    const el = document.createElement("div");
    el.className = "message assistant";
    el.style.borderLeft = "3px solid var(--error-color, #f44336)";
    el.textContent = "⚠ " + text;
    messages.appendChild(el);
    this._scrollToBottom();
  }

  // --- rendering -----------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="sidebar-backdrop"></div>
      <aside class="sidebar">
        <header>
          <h2>Sessions</h2>
          <button class="primary" id="new-chat">+ New</button>
        </header>
        <div class="session-list" id="session-list"></div>
      </aside>
      <section class="main">
        <div class="chat-header">
          <button class="menu-toggle" aria-label="Toggle sessions">☰</button>
          <h1 id="chat-title">Claude Chat</h1>
          <select class="model-picker" id="model-picker"></select>
        </div>
        <div id="hint-banner-slot"></div>
        <div class="messages" id="messages"></div>
        <div class="composer">
          <textarea
            placeholder="Ask Claude to inspect or modify your Home Assistant…"
            rows="2"
          ></textarea>
          <button class="send">Send</button>
          <button class="stop" style="display:none">Stop</button>
        </div>
      </section>
    `;
    this.shadowRoot.querySelector("#new-chat").addEventListener("click", () => {
      this._createSession();
      this._closeSidebar();
    });
    this.shadowRoot.querySelector(".composer .send").addEventListener("click", () =>
      this._sendMessage()
    );
    this.shadowRoot.querySelector(".composer .stop").addEventListener("click", () =>
      this._stop()
    );
    const ta = this.shadowRoot.querySelector("textarea");
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._sendMessage();
      }
    });
    ta.addEventListener("input", () => this._saveDraft(ta.value));
    this.shadowRoot.querySelector("#model-picker").addEventListener("change", (e) => {
      this._selectedModel = e.target.value;
      localStorage.setItem("claude_chat:model", this._selectedModel);
    });
    this.shadowRoot.querySelector(".menu-toggle").addEventListener("click", () =>
      this._toggleSidebar()
    );
    this.shadowRoot.querySelector(".sidebar-backdrop").addEventListener("click", () =>
      this._closeSidebar()
    );
  }

  _toggleSidebar() {
    if (this.hasAttribute("data-sidebar-open")) this.removeAttribute("data-sidebar-open");
    else this.setAttribute("data-sidebar-open", "");
  }
  _closeSidebar() {
    this.removeAttribute("data-sidebar-open");
  }

  _draftKey(sessionId) {
    return `claude_chat:draft:${sessionId || "new"}`;
  }
  _saveDraft(text) {
    try {
      const key = this._draftKey(this._activeSessionId);
      if (text) localStorage.setItem(key, text);
      else localStorage.removeItem(key);
    } catch (_) {}
  }
  _restoreDraft() {
    try {
      const ta = this.shadowRoot.querySelector("textarea");
      if (!ta) return;
      const text = localStorage.getItem(this._draftKey(this._activeSessionId)) || "";
      ta.value = text;
    } catch (_) {}
  }
  _clearDraft() {
    try {
      localStorage.removeItem(this._draftKey(this._activeSessionId));
    } catch (_) {}
  }

  _renderHintBanner() {
    const slot = this.shadowRoot.querySelector("#hint-banner-slot");
    if (!slot) return;
    slot.innerHTML = "";
    const hint = this._diagnostics?.automation_create_hint;
    if (!hint) return;
    const banner = document.createElement("div");
    banner.className = "hint-banner";
    // Bold-format any lines that look like config (indented or contain :)
    const formatted = escapeHtml(hint).replace(
      /(automation: !include automations\.yaml)/g,
      "<code>$1</code>"
    );
    banner.innerHTML = `⚠ ${formatted}`;
    slot.appendChild(banner);
  }

  _renderHeader() {
    const picker = this.shadowRoot.querySelector("#model-picker");
    if (!picker) return;
    picker.innerHTML = "";
    for (const m of this._models) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.name;
      if (m.id === this._selectedModel) opt.selected = true;
      picker.appendChild(opt);
    }
    const title = this.shadowRoot.querySelector("#chat-title");
    if (title) title.textContent = this._activeSession?.title || "Claude Chat";
  }

  _renderSidebar() {
    const list = this.shadowRoot.querySelector("#session-list");
    if (!list) return;
    list.innerHTML = "";
    if (this._sessions.length === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "padding: 16px; color: var(--secondary-text-color); font-size: 13px; text-align: center;";
      empty.textContent = "No sessions yet. Click + New.";
      list.appendChild(empty);
      return;
    }
    for (const s of this._sessions) {
      const el = document.createElement("div");
      el.className = "session" + (s.id === this._activeSessionId ? " active" : "");
      el.innerHTML = `
        <div style="flex:1; min-width:0;">
          <div class="session-title">${escapeHtml(s.title)}</div>
          <div class="session-meta">${s.message_count} msg · ${formatDate(s.updated_at)}</div>
        </div>
        ${s.has_pending ? '<div class="session-pending-dot" title="Pending changes"></div>' : ""}
        <button class="session-delete" title="Delete">×</button>
      `;
      el.addEventListener("click", () => this._selectSession(s.id));
      el.querySelector(".session-delete").addEventListener("click", (e) =>
        this._deleteSession(s.id, e)
      );
      list.appendChild(el);
    }
  }

  _renderChat() {
    const messages = this.shadowRoot.querySelector("#messages");
    const title = this.shadowRoot.querySelector("#chat-title");
    if (!messages) return;
    messages.innerHTML = "";

    if (!this._activeSession) {
      title.textContent = "Claude Chat";
      const examples = [
        "What temperature sensors do I have?",
        "Add a card to the test dashboard showing my living room temperature",
        "Show me all my automations",
      ];
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = `
        <h2>Hi 👋</h2>
        <p>Inspect your Home Assistant or build dashboard widgets in plain English. Every change is staged for your approval.</p>
        <div class="examples"></div>
      `;
      const examplesEl = empty.querySelector(".examples");
      examples.forEach((text) => {
        const ex = document.createElement("button");
        ex.className = "example";
        ex.textContent = text;
        ex.addEventListener("click", () => this._sendMessage(text));
        examplesEl.appendChild(ex);
      });
      messages.appendChild(empty);
      return;
    }

    title.textContent = this._activeSession.title;

    // Build a quick lookup: pending_change_id → PendingChange.
    // Also track the latest pending change per target so older proposals
    // for the same resource (e.g. same automation re-edited) can render
    // as superseded.
    const pendingById = {};
    const latestByTarget = {};
    for (const c of this._activeSession.pending_changes || []) {
      pendingById[c.id] = c;
      latestByTarget[targetKeyFor(c)] = c.id;
    }
    for (const c of Object.values(pendingById)) {
      c._superseded = latestByTarget[targetKeyFor(c)] !== c.id;
    }

    for (const msg of this._activeSession.messages) {
      this._renderMessage(messages, msg, pendingById);
    }

    // Render any pending changes that weren't matched to a tool_use_id
    // (shouldn't normally happen, but render as fallback).
    for (const change of Object.values(pendingById)) {
      if (!change._rendered) this._renderPendingChange(messages, change);
    }

    this._scrollToBottom();
  }

  _renderMessage(container, msg, pendingById) {
    if (msg.role === "user") {
      const text = (msg.content || [])
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
      if (!text) return;
      const row = document.createElement("div");
      row.className = "message-row user";
      row.innerHTML = `<div class="message user"></div>`;
      row.querySelector(".message").textContent = text;
      container.appendChild(row);
      return;
    }

    if (msg.role === "assistant") {
      for (const block of msg.content || []) {
        if (block.type === "text") {
          if (!block.text) continue;
          const row = document.createElement("div");
          row.className = "message-row assistant";
          const bubble = document.createElement("div");
          bubble.className = "message assistant";
          bubble.innerHTML = renderMarkdown(block.text);
          row.appendChild(bubble);
          container.appendChild(row);
        } else if (block.type === "tool_use") {
          const el = document.createElement("details");
          el.className = "tool-call";
          el.innerHTML = `
            <summary>🔧 ${escapeHtml(block.name)} <span class="status">✓</span></summary>
            <div class="body"><pre>${escapeHtml(JSON.stringify(block.input, null, 2))}</pre></div>
          `;
          container.appendChild(el);
        }
      }
    } else if (msg.role === "user") {
      // also handles tool_result blocks (role=user)
    }

    // After every assistant message, render any pending change that was
    // produced by one of its tool_use calls.
    if (msg.role === "assistant") {
      for (const block of msg.content || []) {
        if (block.type !== "tool_use") continue;
        // We don't have direct propose→change mapping in the message data,
        // but propose_* tools produce exactly one pending_change per call.
        // We render unmatched pending changes after the assistant message
        // they likely came from (the latest one).
      }
      // Render any pending changes that haven't been rendered yet.
      for (const change of Object.values(pendingById)) {
        if (change._rendered) continue;
        this._renderPendingChange(container, change);
        change._rendered = true;
      }
    }
  }

  _renderPendingChange(container, change) {
    const el = document.createElement("div");
    el.className = "pending-card" + (change._superseded ? " superseded" : "");
    let body;
    const p = change.payload || {};
    if (change.kind === "service_call") {
      const target = p.target && Object.keys(p.target).length
        ? `\nTarget: ${JSON.stringify(p.target)}` : "";
      const data = p.service_data && Object.keys(p.service_data).length
        ? `\nData: ${JSON.stringify(p.service_data, null, 2)}` : "";
      body = `<div class="service-payload">${escapeHtml(`${p.domain}.${p.service}${target}${data}`)}</div>`;
    } else if (change.kind === "automation_create") {
      body = `<div class="service-payload">${escapeHtml(p.yaml || "")}</div>`;
    } else if (change.kind === "automation_update") {
      body = change.diff
        ? `<div class="diff">${formatDiff(change.diff)}</div>`
        : `<div class="service-payload">${escapeHtml(p.yaml || "")}</div>`;
    } else if (change.kind === "automation_delete") {
      body = `<div class="service-payload">Will remove automation:\n\n${escapeHtml(p.yaml || "")}</div>`;
    } else if (change.diff) {
      body = `<div class="diff">${formatDiff(change.diff)}</div>`;
    } else {
      body = "";
    }
    const supersededLabel = change._superseded
      ? '<div class="superseded-label">Superseded by a newer proposal below — act on that one instead.</div>'
      : "";
    el.innerHTML = `
      <h3>⚠ ${labelForKind(change.kind)}</h3>
      <p>${escapeHtml(change.summary)}</p>
      ${body}
      ${supersededLabel}
      <div class="actions">
        <button class="primary approve">Apply</button>
        <button class="reject">Reject</button>
      </div>
    `;
    if (!change._superseded) {
      el.querySelector(".approve").addEventListener("click", () =>
        this._approveChange(change.id)
      );
      el.querySelector(".reject").addEventListener("click", () =>
        this._rejectChange(change.id)
      );
    }
    container.appendChild(el);
  }
}

function targetKeyFor(change) {
  const p = change.payload || {};
  switch (change.kind) {
    case "dashboard_update":
      return `dashboard:${p.url_path || ""}`;
    case "automation_update":
    case "automation_delete":
      return `automation:${p.automation_id || ""}`;
    case "automation_create":
      return `automation_create:${p.config?.alias || change.id}`;
    case "service_call":
      return `service:${p.domain}.${p.service}:${JSON.stringify(p.target || {})}`;
    default:
      return change.id;
  }
}

// --- helpers ----------------------------------------------------------------

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDate(epochSec) {
  if (!epochSec) return "";
  const d = new Date(epochSec * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString();
}

function formatDiff(diff) {
  return diff
    .split("\n")
    .map((line) => {
      const safe = escapeHtml(line);
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@"))
        return `<span class="hunk">${safe}</span>`;
      if (line.startsWith("+")) return `<span class="add">${safe}</span>`;
      if (line.startsWith("-")) return `<span class="del">${safe}</span>`;
      return safe;
    })
    .join("\n");
}

function labelForKind(kind) {
  if (kind === "dashboard_update") return "Pending dashboard change";
  if (kind === "service_call") return "Pending service call";
  if (kind === "automation_create") return "Pending: create automation";
  if (kind === "automation_update") return "Pending: update automation";
  if (kind === "automation_delete") return "Pending: delete automation";
  return "Pending change: " + kind;
}

function renderMarkdown(text) {
  if (!text) return "";
  if (window.marked && typeof window.marked.parse === "function") {
    try {
      return window.marked.parse(text, { breaks: true, gfm: true });
    } catch (e) {
      console.warn("marked failed", e);
    }
  }
  return escapeHtml(text).replace(/\n/g, "<br>");
}

customElements.define("claude-chat-panel", ClaudeChatPanel);
