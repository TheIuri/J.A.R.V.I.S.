// HUD de JARVIS: esfera animada, push-to-talk y panel de sesión.
// Dos modos (lo dice /hud/config):
//  - "local": servido por jarvis_hud.py en el PC, que añade el token y ejecuta las acciones del PC.
//  - "server": servido por el NAS (p. ej. en el móvil via HTTPS de Tailscale). El token se pide
//    una vez y se guarda en este dispositivo; no hay acciones de PC.
"use strict";

const SESSION = "hud";
const TARGET_RATE = 16000; // lo que espera Whisper
const MIN_RECORD_MS = 300;

const COLORS = {
  idle: [255, 181, 71],
  listening: [76, 201, 240],
  thinking: [179, 136, 255],
  speaking: [255, 140, 66],
  error: [255, 107, 107],
};
const STATE_LABEL = {
  idle: "EN ESPERA",
  listening: "ESCUCHANDO",
  thinking: "PENSANDO",
  speaking: "HABLANDO",
  error: "ERROR",
};

const $ = (id) => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let state = "idle";
let mode = "local";
let pcApps = null;
const TOKEN_KEY = "jarvis_token";
let audioCtx = null;
let micAnalyser = null; // solo mide: nunca va a los altavoces
let outAnalyser = null; // voz de JARVIS -> altavoces
let micStream = null;
let recorder = null; // {node, source, chunks, startedAt}
let playing = Promise.resolve();
const stats = { start: Date.now(), count: 0, peak: 0 };

// --- API y token ---------------------------------------------------------------

function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* sin almacenamiento: habrá que escribirlo cada vez */
  }
}

async function api(path, init = {}) {
  const headers = new Headers(init.headers || {});
  const token = getToken();
  if (mode === "server" && token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401 && mode === "server") {
    setToken("");
    askToken("Token incorrecto o caducado.");
  }
  return resp;
}

function askToken(message = "") {
  $("login-msg").textContent = message;
  $("login").hidden = false;
  $("token").focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const token = $("token").value.trim();
  if (!token) return;
  setToken(token);
  $("token").value = "";
  // Comprobación inofensiva: reiniciar la conversación del HUD exige token.
  const resp = await api("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session: SESSION }),
  });
  if (resp.ok) $("login").hidden = true;
});

// --- estado -----------------------------------------------------------------

function setState(next, detail) {
  state = next;
  const el = $("state");
  el.textContent = detail ? `${STATE_LABEL[next]} · ${detail}` : STATE_LABEL[next];
  el.classList.toggle("error", next === "error");
  if (next === "error") el.style.color = "";
}

// --- audio ------------------------------------------------------------------

function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new AudioContext();
    micAnalyser = audioCtx.createAnalyser();
    outAnalyser = audioCtx.createAnalyser();
    micAnalyser.fftSize = outAnalyser.fftSize = 512;
    outAnalyser.connect(audioCtx.destination);
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

const levelBuf = new Uint8Array(512);
function audioLevel() {
  const analyser = state === "listening" ? micAnalyser : state === "speaking" ? outAnalyser : null;
  if (!analyser) return 0;
  analyser.getByteTimeDomainData(levelBuf);
  let sum = 0;
  for (const v of levelBuf) sum += ((v - 128) / 128) ** 2;
  return Math.min(1, Math.sqrt(sum / levelBuf.length) * 4);
}

async function startRecording() {
  if (recorder || state === "thinking" || state === "speaking") return;
  const ctx = ensureAudio();
  try {
    micStream ??= await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    setState("error", "sin acceso al micrófono");
    return;
  }
  const source = ctx.createMediaStreamSource(micStream);
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  node.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(micAnalyser);
  source.connect(node);
  node.connect(ctx.destination); // necesario para que onaudioprocess se dispare (sale en silencio)
  recorder = { node, source, chunks, startedAt: performance.now() };
  setState("listening");
}

async function stopRecording() {
  if (!recorder) return;
  const { node, source, chunks, startedAt } = recorder;
  recorder = null;
  source.disconnect();
  node.disconnect();
  if (performance.now() - startedAt < MIN_RECORD_MS) {
    setState("idle");
    return;
  }
  const wav = encodeWav(downsample(concat(chunks), audioCtx.sampleRate, TARGET_RATE), TARGET_RATE);
  const form = new FormData();
  form.append("audio", wav, "audio.wav");
  form.append("session", SESSION);
  if (pcApps) form.append("pc_apps", pcApps.join(","));
  await ask("/api/voice", { method: "POST", body: form });
}

function concat(chunks) {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

function downsample(samples, from, to) {
  if (from === to) return samples;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    // media del bloque: filtro paso bajo sencillo para evitar aliasing
    const start = Math.floor(i * ratio);
    const end = Math.min(samples.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += samples[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([buffer], { type: "audio/wav" });
}

function playWav(b64) {
  // Encola: una respuesta y un aviso de temporizador nunca se pisan.
  playing = playing.then(async () => {
    const ctx = ensureAudio();
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const buffer = await ctx.decodeAudioData(bytes.buffer);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(outAnalyser);
    setState("speaking");
    await new Promise((resolve) => {
      source.onended = resolve;
      source.start();
    });
    if (state === "speaking") setState("idle");
  }).catch(() => setState("idle"));
  return playing;
}

// --- conversación ------------------------------------------------------------

async function ask(path, init) {
  setState("thinking");
  let body;
  try {
    const resp = await api(path, init);
    body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  } catch (err) {
    setState("error", String(err.message || err).slice(0, 120));
    return;
  }
  if (!body.transcript) {
    $("subtitle").textContent = "No te he oído bien, prueba otra vez.";
    setState("idle");
    return;
  }
  showTurn(body);
  if (body.audio_wav_b64) await playWav(body.audio_wav_b64);
  else setState("idle");
}

function showTurn(body) {
  $("you").textContent = body.transcript;
  $("subtitle").textContent = body.reply;

  const t = body.timings_ms || {};
  stats.count += 1;
  stats.peak = Math.max(stats.peak, t.total || 0);
  $("s-count").textContent = stats.count;
  $("s-latency").textContent = t.total != null ? `${t.total} MS` : "—";
  $("s-peak").textContent = `${stats.peak} MS`;
  $("s-stages").textContent = Object.entries(t)
    .filter(([k]) => k !== "total")
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ") || "—";
  $("s-model").textContent = body.provider || "—";
  $("s-tools").textContent = (body.tools_used || []).join(", ") || "—";

  const turn = document.createElement("div");
  turn.className = "turn";
  const u = document.createElement("div");
  u.className = "u";
  u.textContent = `› ${body.transcript}`;
  const a = document.createElement("div");
  a.className = "a";
  a.textContent = body.reply;
  const meta = document.createElement("div");
  meta.className = "meta";
  const actions = (body.pc_results || []).map((r) => `[PC] ${r.result}`);
  meta.textContent = [`${t.total ?? "?"} ms`, ...(body.tools_used || []), ...actions].join(" · ");
  turn.append(u, a, meta);
  $("log").append(turn);
  $("log").scrollTop = $("log").scrollHeight;
}

async function sendText(text) {
  const payload = { text, session: SESSION, pc_apps: pcApps };
  await ask("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// --- memoria y sesión --------------------------------------------------------

async function showMemories() {
  const list = $("memory-list");
  list.textContent = "Cargando…";
  try {
    const resp = await api("/api/memories");
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail);
    list.textContent = body.memories.length ? "" : "Sin recuerdos todavía.";
    for (const m of body.memories) {
      const row = document.createElement("div");
      row.className = "mem";
      const text = document.createElement("span");
      text.textContent = `[${m.id}] ${m.content}`;
      text.title = `${m.type} · ${m.updated_at}`;
      const del = document.createElement("button");
      del.textContent = "OLVIDAR";
      del.onclick = async () => {
        await api(`/api/memories/${m.id}`, { method: "DELETE" });
        showMemories();
      };
      row.append(text, del);
      list.append(row);
    }
  } catch (err) {
    list.textContent = `No disponible: ${err.message}`;
  }
}

async function resetConversation() {
  await api("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session: SESSION }),
  });
  $("log").textContent = "";
  $("subtitle").textContent = "";
  $("you").textContent = "";
}

async function pollEvents() {
  try {
    const { events } = await (await fetch("/hud/events")).json();
    for (const ev of events) {
      if (ev.type !== "announce") continue;
      $("subtitle").textContent = ev.text;
      if (ev.audio_wav_b64) playWav(ev.audio_wav_b64);
    }
  } catch {
    /* el HUD local se ha cerrado; se reintenta */
  }
  setTimeout(pollEvents, 2000);
}

// --- esfera ------------------------------------------------------------------

const canvas = $("orb");
const g = canvas.getContext("2d");
const particles = Array.from({ length: 280 }, () => ({
  r: 0.55 + Math.random() * 0.4, // radio relativo de su órbita
  a: Math.random() * Math.PI * 2,
  speed: (0.1 + Math.random() * 0.35) * (Math.random() < 0.5 ? -1 : 1),
  tilt: (Math.random() - 0.5) * 0.9,
  size: 0.6 + Math.random() * 1.6,
}));
let color = [...COLORS.idle];
let level = 0;
let last = performance.now();

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const { width } = canvas.getBoundingClientRect();
  canvas.width = canvas.height = Math.round(width * dpr);
}
addEventListener("resize", resize);

function rgba([r, gr, b], alpha) {
  return `rgba(${r | 0},${gr | 0},${b | 0},${alpha})`;
}

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000) * (reducedMotion ? 0.3 : 1);
  last = now;
  const target = COLORS[state];
  color = color.map((c, i) => c + (target[i] - c) * Math.min(1, dt * 4));
  const lvl = audioLevel();
  level += (lvl - level) * Math.min(1, dt * 12);
  const thinking = state === "thinking" ? 0.5 + 0.5 * Math.sin(now / 180) : 0;

  if (state !== "error") $("state").style.color = rgba(color, 1);

  const W = canvas.width;
  const c = W / 2;
  const R = W * 0.36;
  g.clearRect(0, 0, W, W);

  // halo
  const halo = g.createRadialGradient(c, c, 0, c, c, R * (1.25 + level * 0.3));
  halo.addColorStop(0, rgba(color, 0.22 + level * 0.25 + thinking * 0.1));
  halo.addColorStop(1, rgba(color, 0));
  g.fillStyle = halo;
  g.fillRect(0, 0, W, W);

  // anillo de marcas
  const ticks = 120;
  const spin = now / 9000;
  for (let i = 0; i < ticks; i++) {
    const ang = (i / ticks) * Math.PI * 2 + spin;
    const long = i % 5 === 0;
    const r1 = R * 1.08;
    const r2 = r1 + (long ? R * 0.07 : R * 0.035) * (1 + level * 1.5);
    g.strokeStyle = rgba(color, long ? 0.75 : 0.35);
    g.lineWidth = W * (long ? 0.0028 : 0.0018);
    g.beginPath();
    g.moveTo(c + Math.cos(ang) * r1, c + Math.sin(ang) * r1);
    g.lineTo(c + Math.cos(ang) * r2, c + Math.sin(ang) * r2);
    g.stroke();
  }

  // arco que barre (más rápido al pensar)
  const sweep = now / (state === "thinking" ? 350 : 1400);
  g.strokeStyle = rgba(color, 0.8);
  g.lineWidth = W * 0.003;
  g.beginPath();
  g.ellipse(c, c, R * 0.98, R * 0.35, sweep * 0.3, sweep, sweep + Math.PI * 0.6);
  g.stroke();

  // partículas en órbitas inclinadas
  for (const p of particles) {
    p.a += p.speed * dt * (1 + level * 2 + thinking);
    const rr = R * p.r * (1 + level * 0.12);
    const x = Math.cos(p.a) * rr;
    const y = Math.sin(p.a) * rr * (0.35 + Math.abs(p.tilt));
    const depth = (Math.sin(p.a) + 1) / 2; // delante/detrás
    const px = c + x * Math.cos(p.tilt) - y * Math.sin(p.tilt);
    const py = c + x * Math.sin(p.tilt) + y * Math.cos(p.tilt);
    g.fillStyle = rgba(color, 0.25 + depth * 0.65);
    g.beginPath();
    g.arc(px, py, p.size * (W / 600) * (0.6 + depth * 0.6), 0, Math.PI * 2);
    g.fill();
  }

  // núcleo
  const coreR = R * (0.2 + level * 0.12 + thinking * 0.03);
  const core = g.createRadialGradient(c, c, 0, c, c, coreR * 2.2);
  core.addColorStop(0, "rgba(255,255,255,0.95)");
  core.addColorStop(0.25, rgba(color, 0.9));
  core.addColorStop(1, rgba(color, 0));
  g.fillStyle = core;
  g.beginPath();
  g.arc(c, c, coreR * 2.2, 0, Math.PI * 2);
  g.fill();

  requestAnimationFrame(frame);
}

// --- entradas ----------------------------------------------------------------

addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || document.activeElement === $("text")) return;
  e.preventDefault();
  startRecording();
});
addEventListener("keyup", (e) => {
  if (e.code !== "Space" || document.activeElement === $("text")) return;
  e.preventDefault();
  stopRecording();
});
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  startRecording();
});
canvas.addEventListener("pointerup", stopRecording);
canvas.addEventListener("pointercancel", stopRecording);

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || state === "thinking") return;
  $("text").value = "";
  $("text").blur();
  ensureAudio(); // el clic/Enter cuenta como gesto: permite reproducir la respuesta
  sendText(text);
});
$("memories").addEventListener("click", showMemories);
$("reset").addEventListener("click", resetConversation);

function tick() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("es-ES");
  const s = Math.floor((Date.now() - stats.start) / 1000);
  $("s-time").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
setInterval(tick, 1000);

async function init() {
  resize();
  tick();
  requestAnimationFrame(frame);
  try {
    ({ mode, pc_apps: pcApps } = await (await fetch("/hud/config")).json());
  } catch {
    pcApps = null;
  }
  if (mode === "server") {
    $("hint").textContent = matchMedia("(pointer: coarse)").matches
      ? "MANTÉN PULSADO EL NÚCLEO PARA HABLAR"
      : "MANTÉN PULSADO EL NÚCLEO O LA BARRA ESPACIADORA";
    if (!getToken()) askToken();
  } else {
    pollEvents(); // avisos de temporizador del PC (solo en modo local)
  }
}
init();
