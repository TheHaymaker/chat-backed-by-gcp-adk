/* agent-widget.js — embeddable chat widget (v0)
 *
 * Usage:
 *   <script type="module" src="/agent-widget.js"
 *           data-endpoint="http://localhost:8000" data-tenant="acme"></script>
 *
 * Design notes:
 *  - Shadow DOM isolation; theme via CSS custom properties (--ca-*) settable
 *    from the host page. System font stack: an embed never injects webfonts.
 *  - The agent's ui_blocks render through a fixed component registry; unknown
 *    block types degrade to text. All model text is HTML-escaped, then a tiny
 *    whitelist renderer applies **bold**, `code`, and https-links only.
 *  - Event bus: window.AgentWidget.on(name, fn) / .use(adapter); events also
 *    batch to POST /v1/events.
 */
(() => {
  const script = document.currentScript || document.querySelector('script[data-endpoint]');
  const ENDPOINT = (script?.dataset.endpoint || '').replace(/\/$/, '');
  const TENANT = script?.dataset.tenant || 'acme';

  /* ---------------- event bus + adapters ---------------- */
  const listeners = { '*': [] };
  const pending = [];
  let session = null;

  function emit(name, props = {}) {
    const evt = { name, props, ts: Date.now() / 1000 };
    (listeners[name] || []).forEach((fn) => fn(evt));
    listeners['*'].forEach((fn) => fn(evt));
    pending.push({ name, props });
    if (pending.length >= 10) flushEvents();
  }
  async function flushEvents() {
    if (!pending.length || !session) return;
    const batch = pending.splice(0, pending.length);
    try {
      await api('/v1/events', { events: batch });
    } catch (_) { /* analytics must never break chat */ }
  }
  setInterval(flushEvents, 5000);

  window.AgentWidget = {
    on(name, fn) { (listeners[name] ||= []).push(fn); return this; },
    use(adapter) { this.on('*', adapter); return this; },
  };
  /* built-in adapters */
  window.AgentWidget.GA4Adapter = () => (e) =>
    window.gtag && window.gtag('event', e.name, e.props);
  window.AgentWidget.DataLayerAdapter = () => (e) =>
    (window.dataLayer ||= []).push({ event: e.name, ...e.props });
  window.AgentWidget.WebhookAdapter = ({ url }) => (e) =>
    navigator.sendBeacon && navigator.sendBeacon(url, JSON.stringify(e));

  /* ---------------- API helpers ---------------- */
  async function api(path, body) {
    const res = await fetch(ENDPOINT + path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(session ? { Authorization: `Bearer ${session.session_token}` } : {}),
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${path} ${res.status}`);
    return res;
  }

  async function* sse(res) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') return;
          yield JSON.parse(data);
        }
      }
    }
  }

  /* ---------------- safe text rendering ---------------- */
  const esc = (s) => s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function renderMarkdown(md) {
    let h = esc(String(md));
    h = h.replace(/\[([^\]]{1,80})\]\((https:\/\/[^\s)]{1,500})\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    h = h.replace(/\*\*([^*]{1,200})\*\*/g, '<strong>$1</strong>');
    h = h.replace(/`([^`]{1,200})`/g, '<code>$1</code>');
    return h.replace(/\n/g, '<br>');
  }

  const fmtTime = (iso, tz) => new Date(iso).toLocaleTimeString([],
    { hour: '2-digit', minute: '2-digit', timeZone: tz || 'UTC' });
  const fmtDate = (iso, tz) => new Date(iso).toLocaleDateString([],
    { weekday: 'short', month: 'short', day: 'numeric', timeZone: tz || 'UTC' });

  /* ---------------- block registry ---------------- */
  const registry = {
    text(props) {
      const el = div('blk-text');
      el.innerHTML = renderMarkdown(props.markdown);
      return el;
    },
    quick_replies(props, ctx, blockId) {
      const el = div('blk-qr');
      for (const opt of props.options.slice(0, 5)) {
        const b = document.createElement('button');
        b.className = 'chip';
        b.textContent = opt.label;
        b.onclick = () => {
          emit('ui_block_interacted', { type: 'quick_replies', value: opt.value,
                                        action: opt.action || null });
          el.querySelectorAll('button').forEach((x) => (x.disabled = true));
          if (opt.action) ctx.interact(opt.action, blockId, opt.payload || {});
          else ctx.send(opt.value);
        };
        el.append(b);
      }
      return el;
    },
    form(props, ctx, blockId) {
      const el = div('blk-form');
      if (props.title) {
        const t = div('form-title');
        t.textContent = props.title;
        el.append(t);
      }
      const form = document.createElement('form');
      form.noValidate = true;
      const inputs = {};
      for (const f of props.fields.slice(0, 8)) {
        const row = div('form-row');
        const label = document.createElement('label');
        label.textContent = f.label + (f.required ? ' *' : '');
        label.htmlFor = `f_${f.name}`;
        const input = document.createElement(f.type === 'textarea' ? 'textarea' : 'input');
        if (f.type !== 'textarea') input.type = f.type;
        input.id = `f_${f.name}`;
        input.placeholder = f.placeholder || '';
        if (f.required) input.required = true;
        inputs[f.name] = { input, def: f };
        const err = div('form-err');
        row.append(label, input, err);
        form.append(row);
      }
      const submit = document.createElement('button');
      submit.type = 'submit';
      submit.className = 'form-submit';
      submit.textContent = props.submit_label || 'Submit';
      form.append(submit);
      form.onsubmit = (e) => {
        e.preventDefault();
        let ok = true;
        const values = {};
        for (const [name, { input, def }] of Object.entries(inputs)) {
          const v = input.value.trim();
          const err = input.parentElement.querySelector('.form-err');
          err.textContent = '';
          if (def.required && !v) { err.textContent = 'Required'; ok = false; }
          else if (v && def.type === 'email' && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) {
            err.textContent = 'Enter a valid email'; ok = false;
          }
          values[name] = v;
        }
        if (!ok) return;
        form.querySelectorAll('input,textarea,button').forEach((x) => (x.disabled = true));
        submit.textContent = 'Sending…';
        emit('ui_block_interacted', { type: 'form', form_id: props.form_id });
        ctx.interact('form_submitted', blockId,
          { form_id: props.form_id, values });
      };
      el.append(form);
      return el;
    },
    handoff(props) {
      const el = div('blk-handoff');
      if (props.reason) {
        const r = div('ho-reason');
        r.textContent = props.reason;
      }
      for (const ch of props.channels.slice(0, 4)) {
        const a = document.createElement('a');
        a.className = 'ho-chan';
        a.target = '_blank'; a.rel = 'noopener noreferrer';
        a.href = ch.kind === 'email' ? `mailto:${ch.value}`
               : ch.kind === 'phone' ? `tel:${ch.value}`
               : /^https:\/\//.test(ch.value) ? ch.value : '#';
        a.innerHTML = `<span class="ho-label">${esc(ch.label)}</span>` +
                      `<span class="ho-value">${esc(ch.value)}</span>`;
        a.onclick = () => emit('handoff_channel_clicked', { kind: ch.kind });
        el.append(a);
      }
      return el;
    },
    scheduler(props, ctx, blockId) {
      const el = div('blk-sched');
      const head = div('sched-head');
      head.textContent = `${fmtDate(props.slots[0].start, props.timezone)} · ${props.timezone || 'UTC'}`;
      const rail = div('sched-rail');
      for (const slot of props.slots) {
        const b = document.createElement('button');
        b.className = 'slot';
        b.textContent = fmtTime(slot.start, props.timezone);
        b.onclick = () => {
          rail.querySelectorAll('.slot').forEach((x) => (x.disabled = true));
          b.classList.add('picked');
          emit('ui_block_interacted', { type: 'scheduler', service_id: props.service_id });
          emit('booking_started', { service_id: props.service_id });
          ctx.interact('slot_selected', blockId, { slot_id: slot.slot_id });
        };
        rail.append(b);
      }
      el.append(head, rail);
      return el;
    },
    confirmation(props) {
      const el = div('blk-receipt');
      el.innerHTML =
        `<div class="receipt-title">Booked</div>` +
        `<div class="receipt-row"><span>When</span><b>${esc(fmtDate(props.start))}, ${esc(fmtTime(props.start))}</b></div>` +
        (props.summary ? `<div class="receipt-row"><span>What</span><b>${esc(props.summary)}</b></div>` : '') +
        `<div class="receipt-row"><span>Ref</span><b>${esc(props.booking_ref)}</b></div>`;
      return el;
    },
    faq_card(props) {
      const el = div('blk-faq');
      const q = document.createElement('button');
      q.className = 'faq-q';
      q.setAttribute('aria-expanded', 'true');
      q.innerHTML = `<span>${esc(props.question)}</span><span class="faq-caret">▾</span>`;
      const a = div('faq-a');
      a.innerHTML = renderMarkdown(props.answer_markdown) +
        (props.url ? `<div class="faq-more"><a href="${esc(props.url)}" target="_blank" rel="noopener noreferrer">Read more</a></div>` : '');
      q.onclick = () => {
        const open = a.style.display !== 'none';
        a.style.display = open ? 'none' : '';
        q.setAttribute('aria-expanded', String(!open));
        emit('ui_block_interacted', { type: 'faq_card', expanded: !open });
      };
      el.append(q, a);
      return el;
    },
    kb_answer(props) {
      const el = div('blk-kb');
      const body = div('kb-body');
      body.innerHTML = renderMarkdown(props.markdown);
      const src = div('kb-sources');
      src.innerHTML = '<span class="kb-label">Sources</span>';
      props.citations.forEach((c, i) => {
        const a = document.createElement('a');
        a.href = c.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        a.className = 'kb-cite';
        a.textContent = `${i + 1}. ${c.title}`;
        a.onclick = () => emit('ui_block_interacted',
          { type: 'kb_answer', citation: c.url });
        src.append(a);
      });
      el.append(body, src);
      return el;
    },
    search_results(props) {
      const el = div('blk-search');
      for (const r of props.results.slice(0, 8)) {
        const a = document.createElement('a');
        a.href = r.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        a.className = 'sr-item';
        a.innerHTML = `<span class="sr-title">${esc(r.title)}</span>` +
          (r.snippet ? `<span class="sr-snippet">${esc(r.snippet)}</span>` : '') +
          `<span class="sr-url">${esc(r.url.replace(/^https:\/\//, ''))}</span>`;
        a.onclick = () => emit('search_result_clicked', { url: r.url });
        el.append(a);
      }
      return el;
    },
  };
  const div = (cls) => Object.assign(document.createElement('div'), { className: cls });

  /* ---------------- the component ---------------- */
  class AgentChat extends HTMLElement {
    connectedCallback() {
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.innerHTML = TEMPLATE;
      this.$ = (s) => this.shadowRoot.querySelector(s);
      this.feed = this.$('.feed');
      this.$('.launcher').onclick = () => this.toggle();
      this.$('form.compose').addEventListener('submit', (e) => {
        e.preventDefault();
        const input = this.$('input');
        if (input.value.trim()) { this.send(input.value.trim()); input.value = ''; }
      });
      this.boot();
    }
    async boot() {
      try {
        const res = await api('/v1/session', { tenant: TENANT });
        session = await res.json();
      } catch (e) {
        this.note('Chat is unavailable right now. Reload to try again.');
      }
    }
    toggle() {
      const open = this.$('.panel').classList.toggle('open');
      if (open && !this.opened) { this.opened = true; emit('widget_opened', {}); }
    }
    note(text) {
      const el = div('msg agent');
      el.textContent = text;
      this.feed.append(el);
      this.feed.scrollTop = this.feed.scrollHeight;
    }
    bubble(role) {
      const el = div(`msg ${role}`);
      this.feed.append(el);
      this.feed.scrollTop = this.feed.scrollHeight;
      return el;
    }
    async send(text) {
      this.bubble('user').textContent = text;
      emit('message_sent', { length: text.length });
      await this.turn(api('/v1/chat', { message: text }));
    }
    async interact(action, blockId, payload) {
      await this.turn(api('/v1/interact', { action, block_id: blockId, payload }));
    }
    async turn(request) {
      const typing = this.bubble('agent typing');
      typing.textContent = '· · ·';
      try {
        for await (const env of sse(await request)) {
          typing.remove();
          if (env.message) this.bubble('agent').innerHTML = renderMarkdown(env.message);
          for (const block of env.ui_blocks || []) this.renderBlock(block);
          emit('response_rendered', { blocks: (env.ui_blocks || []).length });
        }
      } catch (e) {
        typing.remove();
        this.note("That didn't go through. Try again.");
      }
    }
    renderBlock(block) {
      const fn = registry[block.type];
      const ctx = { send: this.send.bind(this), interact: this.interact.bind(this) };
      let el;
      if (fn) {
        try { el = fn(block.props, ctx, block.id); }
        catch (_) { el = null; }
      }
      if (!el) {           /* unknown/failed type -> graceful text fallback */
        el = div('blk-text');
        el.textContent = block.props?.markdown || '';
        if (!el.textContent) return;
      }
      const wrap = this.bubble('agent block');
      wrap.append(el);
      emit('ui_block_rendered', { type: block.type });
    }
  }

  const TEMPLATE = `
  <style>
    :host{
      --ca-accent: var(--agent-accent, #315B4E);
      --ca-accent-ink: var(--agent-accent-ink, #FDFCF9);
      --ca-ink: var(--agent-ink, #1E2422);
      --ca-muted: var(--agent-muted, #6B7370);
      --ca-surface: var(--agent-surface, #FFFFFF);
      --ca-bg: var(--agent-bg, #F2F3F1);
      --ca-line: var(--agent-line, #DFE3E0);
      --ca-radius: var(--agent-radius, 14px);
      --ca-font: var(--agent-font, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif);
      all: initial; font-family: var(--ca-font); position: fixed;
      right: 20px; bottom: 20px; z-index: 2147483000;
    }
    @media (prefers-color-scheme: dark){ :host{
      --ca-ink:#E8EAE8; --ca-muted:#9AA29E; --ca-surface:#202523;
      --ca-bg:#171B19; --ca-line:#333A36;
    }}
    *{ box-sizing:border-box; font-family:inherit }
    button{ cursor:pointer }
    button:focus-visible, input:focus-visible, a:focus-visible{
      outline:2px solid var(--ca-accent); outline-offset:2px }
    .launcher{
      width:54px; height:54px; border-radius:50%; border:none;
      background:var(--ca-accent); color:var(--ca-accent-ink);
      font-size:22px; line-height:1; box-shadow:0 6px 20px rgba(0,0,0,.22);
    }
    .panel{
      display:none; position:absolute; right:0; bottom:66px;
      width:min(372px, calc(100vw - 32px)); height:min(560px, 78vh);
      background:var(--ca-bg); color:var(--ca-ink);
      border:1px solid var(--ca-line); border-radius:var(--ca-radius);
      box-shadow:0 18px 48px rgba(0,0,0,.24); overflow:hidden;
      flex-direction:column;
    }
    .panel.open{ display:flex }
    .head{
      padding:14px 16px; background:var(--ca-surface);
      border-bottom:1px solid var(--ca-line);
      font-weight:600; font-size:14px; letter-spacing:.01em;
    }
    .head small{ display:block; font-weight:400; color:var(--ca-muted); margin-top:2px }
    .feed{ flex:1; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:8px }
    .msg{ max-width:85%; padding:9px 12px; border-radius:var(--ca-radius);
      font-size:14px; line-height:1.45; overflow-wrap:anywhere }
    .msg.user{ align-self:flex-end; background:var(--ca-accent); color:var(--ca-accent-ink);
      border-bottom-right-radius:4px }
    .msg.agent{ align-self:flex-start; background:var(--ca-surface);
      border:1px solid var(--ca-line); border-bottom-left-radius:4px }
    .msg.typing{ color:var(--ca-muted); letter-spacing:3px }
    .msg.block{ padding:10px; width:85% }
    .msg code{ background:var(--ca-bg); padding:1px 5px; border-radius:5px; font-size:.92em }
    .msg a{ color:var(--ca-accent) }
    .chip{
      border:1px solid var(--ca-accent); color:var(--ca-accent); background:transparent;
      border-radius:999px; padding:6px 12px; margin:3px 4px 0 0; font-size:13px;
    }
    .chip:hover:not(:disabled){ background:var(--ca-accent); color:var(--ca-accent-ink) }
    .chip:disabled{ opacity:.45 }
    .sched-head{ font-size:12px; color:var(--ca-muted); margin-bottom:8px;
      text-transform:uppercase; letter-spacing:.06em }
    .sched-rail{ display:grid; grid-template-columns:repeat(auto-fill, minmax(76px,1fr)); gap:6px }
    .slot{
      font-variant-numeric:tabular-nums; font-size:13px; padding:8px 4px;
      border:1px solid var(--ca-line); border-radius:8px;
      background:var(--ca-bg); color:var(--ca-ink);
    }
    .slot:hover:not(:disabled){ border-color:var(--ca-accent) }
    .slot.picked{ background:var(--ca-accent); color:var(--ca-accent-ink);
      border-color:var(--ca-accent); opacity:1 }
    .slot:disabled:not(.picked){ opacity:.4 }
    .blk-receipt{
      border:1px solid var(--ca-line); border-top:2px dashed var(--ca-accent);
      border-radius:8px; padding:12px; background:var(--ca-bg);
    }
    .faq-q{
      display:flex; justify-content:space-between; gap:10px; width:100%;
      border:none; background:none; color:var(--ca-ink); text-align:left;
      font-size:14px; font-weight:600; padding:0 0 6px;
    }
    .faq-caret{ color:var(--ca-muted) }
    .faq-a{ font-size:14px; line-height:1.5; border-top:1px solid var(--ca-line); padding-top:8px }
    .faq-more{ margin-top:8px; font-size:13px }
    .faq-more a, .kb-cite{ color:var(--ca-accent) }
    .kb-body{ font-size:14px; line-height:1.5 }
    .kb-sources{ margin-top:10px; padding-top:8px; border-top:1px solid var(--ca-line);
      display:flex; flex-direction:column; gap:4px }
    .kb-label{ font-size:11px; font-weight:700; letter-spacing:.08em;
      text-transform:uppercase; color:var(--ca-muted) }
    .kb-cite{ font-size:13px; text-decoration:none }
    .kb-cite:hover{ text-decoration:underline }
    .blk-search{ display:flex; flex-direction:column; gap:8px }
    .sr-item{
      display:flex; flex-direction:column; gap:2px; text-decoration:none;
      border:1px solid var(--ca-line); border-radius:8px; padding:9px 11px;
      background:var(--ca-bg);
    }
    .sr-item:hover{ border-color:var(--ca-accent) }
    .sr-title{ font-size:14px; font-weight:600; color:var(--ca-accent) }
    .sr-snippet{ font-size:13px; color:var(--ca-ink); line-height:1.4 }
    .sr-url{ font-size:11px; color:var(--ca-muted); font-variant-numeric:tabular-nums }
    .form-title{ font-size:13px; font-weight:700; margin-bottom:8px }
    .form-row{ display:flex; flex-direction:column; gap:3px; margin-bottom:8px }
    .form-row label{ font-size:12px; color:var(--ca-muted) }
    .form-row input, .form-row textarea{
      border:1px solid var(--ca-line); border-radius:8px; padding:8px 10px;
      font-size:14px; background:var(--ca-bg); color:var(--ca-ink); resize:vertical;
    }
    .form-row textarea{ min-height:56px }
    .form-err{ font-size:11px; color:#B4483B; min-height:1px }
    .form-submit{
      border:none; background:var(--ca-accent); color:var(--ca-accent-ink);
      border-radius:999px; padding:8px 16px; font-size:14px; font-weight:600;
    }
    .form-submit:disabled{ opacity:.6 }
    .blk-handoff{ display:flex; flex-direction:column; gap:6px }
    .ho-chan{
      display:flex; justify-content:space-between; gap:10px; align-items:baseline;
      text-decoration:none; border:1px solid var(--ca-line); border-radius:8px;
      padding:9px 11px; background:var(--ca-bg);
    }
    .ho-chan:hover{ border-color:var(--ca-accent) }
    .ho-label{ font-size:13px; font-weight:600; color:var(--ca-accent) }
    .ho-value{ font-size:12px; color:var(--ca-muted); overflow-wrap:anywhere }
    .receipt-title{ font-size:12px; font-weight:700; letter-spacing:.1em;
      text-transform:uppercase; color:var(--ca-accent); margin-bottom:8px }
    .receipt-row{ display:flex; justify-content:space-between; gap:12px;
      font-size:13px; padding:3px 0 }
    .receipt-row span{ color:var(--ca-muted) }
    .receipt-row b{ font-variant-numeric:tabular-nums; text-align:right }
    form.compose{ display:flex; gap:8px; padding:12px;
      background:var(--ca-surface); border-top:1px solid var(--ca-line) }
    form.compose input{
      flex:1; border:1px solid var(--ca-line); border-radius:999px;
      padding:9px 14px; font-size:14px; background:var(--ca-bg); color:var(--ca-ink);
    }
    form.compose button{
      border:none; background:var(--ca-accent); color:var(--ca-accent-ink);
      border-radius:999px; padding:0 16px; font-size:14px; font-weight:600;
    }
    @media (prefers-reduced-motion: no-preference){
      .msg{ animation:rise .18s ease-out }
      @keyframes rise{ from{ transform:translateY(4px); opacity:0 } }
    }
  </style>
  <button class="launcher" aria-label="Open chat" aria-haspopup="dialog">✳</button>
  <div class="panel" role="dialog" aria-label="Chat assistant">
    <div class="head">Assistant<small>Ask a question or book a time</small></div>
    <div class="feed" aria-live="polite"></div>
    <form class="compose">
      <input type="text" placeholder="Message…" aria-label="Message" maxlength="4000">
      <button type="submit">Send</button>
    </form>
  </div>`;

  customElements.define('agent-chat', AgentChat);
  if (!document.querySelector('agent-chat')) {
    document.body.append(document.createElement('agent-chat'));
  }
})();
