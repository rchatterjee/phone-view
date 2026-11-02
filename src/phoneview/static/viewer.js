'use strict';
// PhoneView viewer: decodes the phone's screen (WebCodecs H.264/HEVC or JPEG),
// forwards pointer/keyboard input, and builds its controls from the backend's
// capabilities. No framework, no build step.

// ---- pure helpers (unit-tested in tests/test_viewer_js.cjs) --------------------

const COORD_MAX = 65535;
const MSG_VIDEO = 1, MSG_JPEG = 2;

function normalise(clientX, clientY, rect) {
  const fx = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const fy = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
  return {x: Math.round(fx * COORD_MAX), y: Math.round(fy * COORD_MAX)};
}

function parseBinary(buffer) {
  const view = new DataView(buffer);
  const type = view.getUint8(0);
  if (type === MSG_VIDEO) {
    return {type: 'video', key: (view.getUint8(1) & 1) === 1,
            pts: Number(view.getBigUint64(2)), data: new Uint8Array(buffer, 10)};
  }
  if (type === MSG_JPEG) return {type: 'jpeg', data: new Uint8Array(buffer, 1)};
  return {type: 'unknown'};
}

const KEYBOARD_MAP = {
  Enter: 'enter', Backspace: 'delete', Delete: 'forward_delete', Tab: 'tab', Escape: 'escape',
  ArrowUp: 'arrow_up', ArrowDown: 'arrow_down', ArrowLeft: 'arrow_left', ArrowRight: 'arrow_right',
  Home: 'move_home', End: 'move_end', PageUp: 'page_up', PageDown: 'page_down',
};

function keyForEvent(e, keys) {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  const mapped = KEYBOARD_MAP[e.key];
  if (mapped === 'escape' && !keys.includes('escape') && keys.includes('back')) return 'back';
  return mapped && keys.includes(mapped) ? mapped : null;
}

function isPrintable(e) {
  return e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
}

function base64ToBytes(text) {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function decoderConfig(cfg) {
  const config = {codec: cfg.codec_string, optimizeForLatency: true};
  if (cfg.format === 'hvcc' && cfg.description) config.description = base64ToBytes(cfg.description);
  return config;  // no description: the bitstream is Annex-B with in-band parameter sets
}

// Pointer ids 0..9 for the server; mouse is always 0.
class PointerSlots {
  constructor() { this.map = new Map(); }
  claim(pointerId, isMouse, multitouch) {
    if (this.map.has(pointerId)) return this.map.get(pointerId);
    const used = new Set(this.map.values());
    if (!multitouch && used.size) return null;
    let slot = isMouse ? 0 : [...Array(10).keys()].find(i => !used.has(i));
    if (slot === undefined || used.has(slot)) return null;
    this.map.set(pointerId, slot);
    return slot;
  }
  get(pointerId) { return this.map.get(pointerId); }
  release(pointerId) { const s = this.map.get(pointerId); this.map.delete(pointerId); return s; }
  clear() { const all = [...this.map.entries()]; this.map.clear(); return all; }
}

const NAV_LABELS = [
  ['back', 'Back'], ['home', 'Home'], ['app_switch', 'Recent apps'], ['power', 'Power'],
  ['volume_down', 'Volume −'], ['volume_up', 'Volume +'], ['siri', 'Siri / Assistant'], ['menu', 'Menu'],
];
const COMMAND_LABELS = [
  ['expand_notifications', 'Notifications'], ['expand_settings', 'Quick settings'],
  ['collapse_panels', 'Close panels'], ['rotate', 'Rotate'], ['screen_off', 'Screen off'], ['screen_on', 'Screen on'],
];
const SETUP_LABELS = {
  developer_mode_check: 'Check Developer Mode', developer_mode_reveal: 'Reveal Developer Mode setting',
  mount_ddi: 'Retry disk image mount', start_wda: 'Start WebDriverAgent',
  use_screenshots: 'Use screenshots instead', retry_stream: 'Retry live video stream',
};

if (typeof module !== 'undefined') {
  module.exports = {normalise, parseBinary, keyForEvent, isPrintable, base64ToBytes, decoderConfig,
                    PointerSlots, COORD_MAX};
}

// ---- browser app ---------------------------------------------------------------

if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const canvas = $('screen'), ctx = canvas.getContext('2d');
  const placeholder = $('placeholder'), deviceEl = $('device'), errorEl = $('error'), noteEl = $('note');
  const feedback = $('feedback'), metricsEl = $('metrics'), controlMode = $('control-mode');
  const navEl = $('nav'), commandsEl = $('commands'), textForm = $('text-form'), textInput = $('text-input');
  const setupEl = $('setup'), setupStatus = $('setup-status'), devGuide = $('devmode-guide');
  const setupButtons = $('setup-buttons'), settingsBox = $('settings-box'), settingsForm = $('settings-form');
  const reconnectBtn = $('reconnect');

  let ws = null, retryTimer = null, generation = null;
  let caps = {keys: [], commands: [], setup: [], settings: []};
  let status = {};
  let decoder = null, needKey = true, videoCfg = null, decodeFailures = 0;
  let decodedFrames = 0, fpsTime = performance.now(), displayFps = 0;
  const slots = new PointerSlots();
  const pendingMoves = new Map();
  let moveScheduled = false, textBuffer = '', textTimer = null, lastWheel = 0, pinch = null;

  // -- connection ----------------------------------------------------------------
  function connect() {
    clearTimeout(retryTimer);
    reconnectBtn.hidden = true;
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { deviceEl.textContent = 'Connected. Looking for a phone…'; };
    ws.onmessage = e => (typeof e.data === 'string' ? onText(JSON.parse(e.data)) : onBinary(e.data));
    ws.onclose = e => {
      resetVideo(); slots.clear(); generation = null;
      deviceEl.textContent = e.code === 1008 ? (e.reason || 'Session refused') : 'Disconnected from PhoneView';
      reconnectBtn.hidden = false;
      const refused = e.code === 1008 && !/timed out/i.test(e.reason || '');
      if (!refused) retryTimer = setTimeout(connect, 2000);
    };
  }
  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  function sendEvent(obj) {
    if (generation === null) return;
    send({...obj, generation});
  }

  // -- incoming --------------------------------------------------------------------
  function onText(msg) {
    switch (msg.type) {
      case 'status': return onStatus(msg);
      case 'capabilities': caps = msg; return renderControls();
      case 'video_config': return configureVideo(msg);
      case 'input_result': if (msg.error) feedback.textContent = msg.error; return;
      case 'settings_result':
        feedback.textContent = msg.error || 'Settings applied';
        return;
      case 'setup_result':
        feedback.textContent = msg.error || msg.message || 'Done';
        return;
    }
  }

  function onStatus(msg) {
    status = msg;
    if (generation !== msg.generation) { slots.clear(); generation = msg.generation; }
    const d = msg.device;
    if (d) {
      const parts = [d.name, `${d.os} ${d.version || ''}`.trim()];
      if (msg.mode) parts.push({stream: 'live video', screenshot: 'screenshots', legacy: 'screenshots (iOS 15–16)'}[msg.mode] || msg.mode);
      if (msg.platform === 'android') parts.push(msg.streaming ? 'live video' : 'idle');
      if (msg.viewer && msg.viewer !== 'local browser') parts.push(`viewing as ${msg.viewer}`);
      deviceEl.textContent = parts.filter(Boolean).join(' · ');
    } else {
      deviceEl.textContent = msg.platform === 'none' ? 'No phone connected' : 'Waiting for the phone…';
    }
    errorEl.hidden = !msg.error; errorEl.textContent = msg.error || '';
    const note = msg.note || null;
    noteEl.hidden = !note; noteEl.textContent = note || '';
    controlMode.textContent = msg.control ? `Control: ${msg.control}` : (msg.wda || '');
    metricsEl.textContent = metricsText(msg);
    renderSetup();
  }

  function metricsText(msg) {
    const bits = [];
    if (canvas.width) bits.push(`${canvas.width}×${canvas.height}`);
    bits.push(`${displayFps.toFixed(1)} fps shown`);
    if (msg.mbps !== undefined) bits.push(`${msg.mbps} Mbps`);
    if (msg.capture_fps !== undefined) bits.push(`capture ${msg.capture_fps} fps`);
    if (msg.screenshot_ms) bits.push(`screenshot ${msg.screenshot_ms} ms`);
    return bits.join(' · ');
  }

  function onBinary(buffer) {
    const msg = parseBinary(buffer);
    if (msg.type === 'video') return decodeVideo(msg);
    if (msg.type === 'jpeg') return showJpeg(msg.data);
  }

  // -- video ------------------------------------------------------------------------
  function closeDecoder() {
    if (decoder) { try { decoder.close(); } catch (_) {} }
    decoder = null; videoCfg = null; needKey = true;
  }

  // Blank the screen only when the device stops sending video (not on a mere
  // reconfigure, e.g. rotation), so a drag in progress keeps a real rectangle.
  function resetVideo() {
    closeDecoder();
    canvas.classList.add('empty'); placeholder.hidden = false;
  }

  // Configured synchronously: the key frame follows the config immediately on the
  // wire, and an async support check would let it slip past an unready decoder.
  // An unsupported config surfaces through the error callback instead.
  function configureVideo(cfg) {
    if (!cfg.codec || cfg.codec === 'jpeg') return resetVideo();
    closeDecoder();
    videoCfg = cfg;
    if (!('VideoDecoder' in window)) {
      return videoUnsupported('This browser has no WebCodecs video decoder. Use a current Chrome, Edge, Safari or Firefox, opened at http://127.0.0.1 or localhost.');
    }
    let decodedSinceConfig = 0;
    const current = new VideoDecoder({
      output: frame => { decodedSinceConfig++; decodeFailures = 0; draw(frame, frame.displayWidth, frame.displayHeight); frame.close(); },
      error: err => {
        if (decoder !== current) return;
        decodeFailures++;
        if (decodedSinceConfig === 0 && decodeFailures >= 2) {
          closeDecoder();
          return videoUnsupported(`This browser cannot decode ${cfg.codec.toUpperCase()} (${cfg.codec_string}): ${err.message}`);
        }
        feedback.textContent = `Video decoder error: ${err.message}; recovering`;
        setTimeout(() => {
          if (videoCfg === cfg) { configureVideo(cfg); send({type: 'keyframe'}); }
        }, 200);
      },
    });
    decoder = current;
    try {
      decoder.configure(decoderConfig(cfg));
    } catch (err) {
      closeDecoder();
      return videoUnsupported(`This browser cannot decode ${cfg.codec.toUpperCase()} (${cfg.codec_string}).`);
    }
    needKey = true;  // the server sends a key frame after every config
  }

  function videoUnsupported(text) {
    feedback.textContent = text;
    if (caps.setup.includes('use_screenshots')) send({type: 'setup', action: 'use_screenshots'});
  }

  function decodeVideo(msg) {
    if (!decoder || decoder.state !== 'configured') return;
    if (needKey) {
      if (!msg.key) return;
      needKey = false;
    }
    if (decoder.decodeQueueSize > 20 && !msg.key) {  // decoder falling behind: skip to next key frame
      needKey = true; send({type: 'keyframe'}); return;
    }
    try {
      decoder.decode(new EncodedVideoChunk({type: msg.key ? 'key' : 'delta', timestamp: msg.pts, data: msg.data}));
    } catch (err) {
      needKey = true; send({type: 'keyframe'});
    }
  }

  async function showJpeg(bytes) {
    try {
      const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
      draw(bitmap, bitmap.width, bitmap.height);
      bitmap.close();
    } catch (_) {
      // A bad frame is skipped; the acknowledgement below still releases the next one.
    } finally {
      send({type: 'frame_ack'});
    }
  }

  function draw(image, width, height) {
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width; canvas.height = height;
    }
    ctx.drawImage(image, 0, 0, width, height);
    canvas.classList.remove('empty');
    placeholder.hidden = true;
    decodedFrames++;
  }

  setInterval(() => {
    const now = performance.now();
    displayFps = decodedFrames * 1000 / (now - fpsTime); decodedFrames = 0; fpsTime = now;
  }, 1000);

  // -- pointer input -----------------------------------------------------------------
  function pos(e) { return normalise(e.clientX, e.clientY, canvas.getBoundingClientRect()); }
  function mirror(p) { return {x: COORD_MAX - p.x, y: COORD_MAX - p.y}; }

  canvas.addEventListener('pointerdown', e => {
    if (!caps.touch || canvas.classList.contains('empty')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    e.preventDefault(); canvas.focus();
    const slot = slots.claim(e.pointerId, e.pointerType === 'mouse', caps.multitouch);
    if (slot === null) return;
    canvas.setPointerCapture(e.pointerId);
    const p = pos(e);
    sendEvent({type: 'touch', phase: 'down', id: slot, ...p});
    // Ctrl + mouse drag = two-finger pinch/rotate around the centre (Android).
    if (e.pointerType === 'mouse' && e.ctrlKey && caps.multitouch) {
      pinch = true; sendEvent({type: 'touch', phase: 'down', id: 1, ...mirror(p)});
    }
  });
  canvas.addEventListener('pointermove', e => {
    const slot = slots.get(e.pointerId);
    if (slot === undefined || !canvas.getBoundingClientRect().width) return;
    pendingMoves.set(slot, pos(e));
    if (!moveScheduled) { moveScheduled = true; requestAnimationFrame(flushMoves); }
  });
  function flushMoves() {
    moveScheduled = false;
    for (const [slot, p] of pendingMoves) {
      sendEvent({type: 'touch', phase: 'move', id: slot, ...p});
      if (pinch && slot === 0) sendEvent({type: 'touch', phase: 'move', id: 1, ...mirror(p)});
    }
    pendingMoves.clear();
  }
  function endPointer(e, phase) {
    const slot = slots.release(e.pointerId);
    if (slot === undefined) return;
    const last = pendingMoves.get(slot);
    pendingMoves.delete(slot);
    // A hidden canvas has no rectangle; lift where the finger last was instead of a corner.
    const p = canvas.getBoundingClientRect().width ? pos(e) : (last || {x: 0, y: 0});
    sendEvent({type: 'touch', phase, id: slot, ...p});
    if (pinch && slot === 0) { sendEvent({type: 'touch', phase, id: 1, ...mirror(p)}); pinch = null; }
  }
  canvas.addEventListener('pointerup', e => endPointer(e, 'up'));
  canvas.addEventListener('pointercancel', e => endPointer(e, 'cancel'));
  canvas.addEventListener('lostpointercapture', e => endPointer(e, 'cancel'));
  canvas.addEventListener('contextmenu', e => e.preventDefault());

  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const now = performance.now();
    const p = pos(e);
    if (caps.scroll) {
      if (now - lastWheel < 33) return;
      lastWheel = now;
      const unit = e.deltaMode === 1 ? 1 : 1 / 100;
      const clamp = v => Math.max(-16, Math.min(16, v));
      sendEvent({type: 'scroll', ...p, dx: clamp(-e.deltaX * unit), dy: clamp(-e.deltaY * unit)});
    } else if (caps.touch && slots.map.size === 0 && e.deltaY && now - lastWheel > 450) {
      lastWheel = now;
      swipe(p, Math.sign(e.deltaY));  // no native scroll: synthesise a short drag
    }
  }, {passive: false});

  function swipe(p, direction) {
    const y = Math.max(12000, Math.min(53535, p.y));
    const from = y + direction * 8000, to = y - direction * 8000;
    const steps = 6;
    sendEvent({type: 'touch', phase: 'down', id: 0, x: p.x, y: from});
    for (let i = 1; i <= steps; i++) {
      const yy = Math.round(from + (to - from) * i / steps);
      setTimeout(() => sendEvent({type: 'touch', phase: i === steps ? 'up' : 'move', id: 0, x: p.x, y: yy}), i * 30);
    }
  }

  // -- keyboard -------------------------------------------------------------------------
  function flushText() {
    clearTimeout(textTimer); textTimer = null;
    while (textBuffer) {
      sendEvent({type: 'text', text: textBuffer.slice(0, 300)});
      textBuffer = textBuffer.slice(300);
    }
  }
  canvas.addEventListener('keydown', e => {
    const key = keyForEvent(e, caps.keys);
    if (key) {
      e.preventDefault(); flushText();
      sendEvent({type: 'key', key, phase: 'press'});
    } else if (caps.text && isPrintable(e)) {
      e.preventDefault();
      textBuffer += e.key;
      if (!textTimer) textTimer = setTimeout(flushText, 150);
    }
  });
  canvas.addEventListener('paste', e => {
    if (!caps.text) return;
    e.preventDefault();
    textBuffer += (e.clipboardData.getData('text') || '').replace(/\r\n/g, '\n');
    flushText();
  });
  textForm.addEventListener('submit', e => {
    e.preventDefault();
    if (!textInput.value) return;
    textBuffer += textInput.value; textInput.value = '';
    flushText();
  });

  // -- controls from capabilities -----------------------------------------------------
  function button(label, onClick, title) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = label; if (title) b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }

  function renderControls() {
    navEl.replaceChildren(...NAV_LABELS.filter(([k]) => caps.keys.includes(k)).map(([k, label]) =>
      button(label, () => sendEvent({type: 'key', key: k, phase: 'press'}))));
    commandsEl.replaceChildren(...COMMAND_LABELS.filter(([k]) => caps.commands.includes(k)).map(([k, label]) =>
      button(label, () => sendEvent({type: 'command', name: k}))));
    textForm.hidden = !caps.text;
    canvas.style.cursor = caps.touch ? 'crosshair' : 'default';
    renderSettings();
    renderSetup();
  }

  function renderSettings() {
    settingsBox.hidden = !caps.settings.length;
    const fields = caps.settings.map(s => {
      const label = document.createElement('label');
      label.textContent = s.label;
      let input;
      if (s.options) {
        input = document.createElement('select');
        for (const o of s.options) {
          const opt = document.createElement('option');
          opt.value = String(o.value); opt.textContent = o.label;
          if (o.value === s.value) opt.selected = true;
          input.append(opt);
        }
      } else {
        input = document.createElement('input');
        Object.assign(input, {type: 'number', min: s.min, max: s.max, step: s.step, value: s.value, required: true});
      }
      input.name = s.name;
      label.append(input);
      return label;
    });
    const apply = document.createElement('button');
    apply.type = 'submit'; apply.textContent = 'Apply';
    settingsForm.replaceChildren(...fields, apply);
  }
  settingsForm.addEventListener('submit', e => {
    e.preventDefault();
    if (!settingsForm.reportValidity()) return;
    const values = {};
    for (const s of caps.settings) values[s.name] = Number(settingsForm.elements[s.name].value);
    feedback.textContent = 'Applying settings…';
    send({type: 'stream_settings', generation, settings: values});
  });

  function renderSetup() {
    const dm = status.developer_mode;
    const actions = caps.setup.filter(a => SETUP_LABELS[a]);
    const showDev = status.platform === 'ios' && dm && ['disabled', 'unknown'].includes(dm.state);
    const others = actions.filter(a => !a.startsWith('developer_mode'));
    setupEl.hidden = !(showDev || others.length);
    devGuide.hidden = !(showDev && dm.state === 'disabled');
    setupStatus.textContent = dm && status.platform === 'ios'
      ? `Developer Mode: ${dm.state}. ${dm.message || ''}` + (dm.reveal === 'revealed' ? ' The setting was revealed; reopen Settings.' : '')
      : '';
    const visible = actions.filter(a => showDev || !a.startsWith('developer_mode'));
    const key = visible.join(',');
    if (setupButtons.dataset.key !== key) {
      setupButtons.dataset.key = key;
      setupButtons.replaceChildren(...visible.map(a => button(SETUP_LABELS[a], () => {
        feedback.textContent = 'Working…';
        send({type: 'setup', action: a});
      })));
    }
  }

  reconnectBtn.addEventListener('click', () => { if (!ws || ws.readyState === WebSocket.CLOSED) connect(); });
  window.addEventListener('pagehide', () => { if (ws) ws.close(); });
  connect();
});
