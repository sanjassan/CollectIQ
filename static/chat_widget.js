/* CollectIQ floating card assistant.
 * Self-contained: injects its own styles + DOM. Renders a chat bubble at the
 * bottom-right of every page; users ask questions about any card and get
 * grounded answers (price drivers, selling points) from /api/chat.
 *
 * Optional per-page focus: set window.CIQ_CARD = {token_id, name} to make the
 * assistant answer about the card currently in view.
 */
(function () {
  if (window.__ciqChatLoaded) return;
  window.__ciqChatLoaded = true;

  var T = {
    title: "Card Assistant",
    intro: "Ask me about any card — price drivers, whether it's a good buy, or its selling points.",
    placeholder: "Ask about a card…",
    send: "Send",
    thinking: "Thinking…",
    offline: "Model offline — showing grounded facts",
  };

  var css =
    "#ciq-chat-btn{position:fixed;right:22px;bottom:22px;width:56px;height:56px;border-radius:50%;" +
    "background:linear-gradient(135deg,#1f6feb,#8957e5);color:#fff;border:none;cursor:pointer;" +
    "font-size:26px;box-shadow:0 6px 20px rgba(0,0,0,.45);z-index:99998;display:flex;align-items:center;justify-content:center;transition:transform .15s}" +
    "#ciq-chat-btn:hover{transform:scale(1.08)}" +
    "#ciq-chat-panel{position:fixed;right:22px;bottom:88px;width:370px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 120px);" +
    "background:#161b22;border:1px solid #30363d;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.55);z-index:99999;display:none;flex-direction:column;overflow:hidden;font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans TC',sans-serif}" +
    "#ciq-chat-panel.open{display:flex}" +
    "#ciq-chat-head{padding:13px 16px;background:#0d1117;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:9px}" +
    "#ciq-chat-head .dot{width:9px;height:9px;border-radius:50%;background:#3fb950;flex-shrink:0}" +
    "#ciq-chat-head .dot.off{background:#8b949e}" +
    "#ciq-chat-head b{font-size:14px;color:#e6edf3}" +
    "#ciq-chat-head small{color:#8b949e;font-size:11px;margin-left:auto}" +
    "#ciq-chat-head .x{background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;line-height:1;padding:2px 4px}" +
    "#ciq-chat-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}" +
    ".ciq-msg{font-size:13px;line-height:1.5;padding:9px 12px;border-radius:10px;max-width:88%;white-space:pre-wrap;word-wrap:break-word}" +
    ".ciq-msg.user{align-self:flex-end;background:#1f6feb;color:#fff;border-bottom-right-radius:3px}" +
    ".ciq-msg.bot{align-self:flex-start;background:#21262d;color:#e6edf3;border-bottom-left-radius:3px}" +
    ".ciq-msg.bot.off{border:1px solid #30363d}" +
    ".ciq-src{align-self:flex-start;display:flex;gap:8px;flex-wrap:wrap;max-width:100%}" +
    ".ciq-src a{display:flex;gap:7px;align-items:center;text-decoration:none;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:5px 8px;color:#c9d1d9;font-size:11px;max-width:150px}" +
    ".ciq-src a img{width:26px;height:26px;object-fit:cover;border-radius:4px;flex-shrink:0}" +
    ".ciq-src a span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    "#ciq-chat-form{display:flex;gap:8px;padding:11px;border-top:1px solid #30363d;background:#0d1117}" +
    "#ciq-chat-input{flex:1;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:9px 11px;font-size:13px;outline:none}" +
    "#ciq-chat-input:focus{border-color:#1f6feb}" +
    "#ciq-chat-send{background:#1f6feb;border:none;border-radius:8px;color:#fff;padding:0 15px;cursor:pointer;font-size:13px;font-weight:600}" +
    "#ciq-chat-send:disabled{opacity:.5;cursor:default}";

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var btn = document.createElement("button");
  btn.id = "ciq-chat-btn";
  btn.title = T.title;
  btn.innerHTML = "&#128172;"; // speech balloon
  document.body.appendChild(btn);

  var panel = document.createElement("div");
  panel.id = "ciq-chat-panel";
  panel.innerHTML =
    '<div id="ciq-chat-head"><span class="dot off" id="ciq-dot"></span><b>' + T.title +
    '</b><small id="ciq-status">…</small><button class="x" id="ciq-close">&times;</button></div>' +
    '<div id="ciq-chat-msgs"></div>' +
    '<form id="ciq-chat-form"><input id="ciq-chat-input" autocomplete="off" placeholder="' +
    T.placeholder + '"/><button id="ciq-chat-send" type="submit">' + T.send + "</button></form>";
  document.body.appendChild(panel);

  var msgs = panel.querySelector("#ciq-chat-msgs");
  var input = panel.querySelector("#ciq-chat-input");
  var form = panel.querySelector("#ciq-chat-form");
  var sendBtn = panel.querySelector("#ciq-chat-send");
  var dot = panel.querySelector("#ciq-dot");
  var statusEl = panel.querySelector("#ciq-status");
  var history = [];
  var greeted = false;

  function esc(s) {
    return (s || "").replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function addMsg(text, who, off) {
    var d = document.createElement("div");
    d.className = "ciq-msg " + who + (off ? " off" : "");
    d.textContent = text;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }

  function addSources(sources) {
    if (!sources || !sources.length) return;
    var wrap = document.createElement("div");
    wrap.className = "ciq-src";
    sources.forEach(function (s) {
      var a = document.createElement("a");
      a.href = s.url || "#";
      a.target = "_blank";
      a.rel = "noopener";
      a.innerHTML = (s.image_url ? '<img src="' + esc(s.image_url) + '"/>' : "") +
        "<span>" + esc((s.name || "card").slice(0, 40)) + "</span>";
      wrap.appendChild(a);
    });
    msgs.appendChild(wrap);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function refreshHealth() {
    fetch("/api/chat/health").then(function (r) { return r.json(); }).then(function (h) {
      dot.className = "dot" + (h.llm_online ? "" : " off");
      statusEl.textContent = (h.llm_online ? h.llm_model : "offline") +
        " · " + (h.cards_indexed || 0) + " cards";
    }).catch(function () { statusEl.textContent = ""; });
  }

  function openPanel() {
    panel.classList.add("open");
    if (!greeted) {
      greeted = true;
      var hint = window.CIQ_CARD && window.CIQ_CARD.name
        ? T.intro + "\n\nIn view: " + window.CIQ_CARD.name
        : T.intro;
      addMsg(hint, "bot");
      refreshHealth();
    }
    input.focus();
  }

  btn.addEventListener("click", function () {
    panel.classList.contains("open") ? panel.classList.remove("open") : openPanel();
  });
  panel.querySelector("#ciq-close").addEventListener("click", function () {
    panel.classList.remove("open");
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q) return;
    addMsg(q, "user");
    history.push({ role: "user", content: q });
    input.value = "";
    sendBtn.disabled = true;
    var pending = addMsg(T.thinking, "bot");

    var payload = { message: q, history: history.slice(0, -1) };
    if (window.CIQ_CARD) {
      if (window.CIQ_CARD.token_id) payload.token_id = window.CIQ_CARD.token_id;
      if (window.CIQ_CARD.name) payload.card_name = window.CIQ_CARD.name;
    }

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); }).then(function (res) {
      pending.remove();
      var d = addMsg(res.answer || "(no answer)", "bot", !res.llm_online);
      history.push({ role: "assistant", content: res.answer || "" });
      addSources(res.sources);
      dot.className = "dot" + (res.llm_online ? "" : " off");
    }).catch(function () {
      pending.remove();
      addMsg("Network error — please try again.", "bot", true);
    }).then(function () {
      sendBtn.disabled = false;
      input.focus();
    });
  });
})();
