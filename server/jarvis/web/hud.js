// HUD de JARVIS: cerebro animado (flujo de pensamiento en directo), push-to-talk y panel de sesión.
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
let wakeEnabled = false; // "Hey Jarvis" en el PC (modo local con openwakeword)
let wakeActive = false; // se oyó "Hey Jarvis" y se espera la frase
let busySent = false;
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
  if (next === "idle" && queuedNotices.length) setTimeout(flushNotices, 400);
  const busy = next === "listening" || next === "thinking" || next === "speaking";
  if (wakeEnabled && busy !== busySent) {
    busySent = busy; // mientras piensa o habla, el PC no escucha "Hey Jarvis" (su propia voz)
    fetch("/hud/busy", { method: "POST", body: JSON.stringify({ busy }) }).catch(() => {});
  }
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
  flowReset();
  let body;
  try {
    let resp = await api(`${path}/stream`, init);
    if (resp.status === 404) resp = await api(path, init); // servidor antiguo: sin directo
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    body = resp.headers.get("Content-Type")?.includes("ndjson") ? await readFlow(resp) : await resp.json();
  } catch (err) {
    regionState.forEach((r) => (r.pending = false));
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
  $("flow").classList.add("past");
}

// Lee la respuesta línea a línea (NDJSON): cada línea es un paso del turno.
async function readFlow(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      const event = JSON.parse(line);
      if (event.type === "done") return event;
      if (event.type === "error") throw new Error(event.detail);
      onFlow(event);
    }
    if (done) throw new Error("la conexión se cortó a mitad de respuesta");
  }
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

// --- avisos proactivos (recordatorios, agenda, TrueNAS...) ----------------------------

const queuedNotices = [];

async function noticesLoop(after = -1) {
  let next = after;
  let delay = 0;
  if (mode === "server" && !getToken()) return setTimeout(() => noticesLoop(after), 3000); // aún sin token
  try {
    const resp = await api(`/api/notifications?after=${after}&wait=${after < 0 ? 0 : 25}`);
    if (!resp.ok) throw new Error(resp.status);
    const body = await resp.json();
    if (!body.enabled) return; // avisos desactivados en el servidor
    if (after >= 0) body.notices.forEach((n) => queuedNotices.push(n));
    next = body.last;
    flushNotices();
  } catch {
    delay = 5000; // sin conexión o sin token todavía: se reintenta
  }
  setTimeout(() => noticesLoop(next), delay);
}

// Los avisos esperan a que JARVIS no esté escuchando, pensando ni hablando.
function flushNotices() {
  if (state !== "idle" && state !== "error") return;
  while (queuedNotices.length) {
    const n = queuedNotices.shift();
    $("subtitle").textContent = n.text;
    fire(REGION.thalamus, n.text);
    const turn = document.createElement("div");
    turn.className = `turn notice ${n.level}`;
    const a = document.createElement("div");
    a.className = "a";
    a.textContent = n.text;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `AVISO · ${n.source} · ${n.created.slice(11, 16)}`;
    turn.append(a, meta);
    $("log").append(turn);
    $("log").scrollTop = $("log").scrollHeight;
    // Solo habla la pestaña visible (si tienes el HUD abierto en el PC y en el móvil, no suenan los dos).
    if (n.speak && n.audio_wav_b64 && document.visibilityState === "visible") playWav(n.audio_wav_b64);
  }
}

async function pollEvents() {
  // El proxy del PC contesta en cuanto hay un aviso (o a los 20 s sin nada): reacción inmediata.
  let delay = 0;
  try {
    const { events } = await (await fetch("/hud/events")).json();
    events.forEach(onPcEvent);
  } catch {
    delay = 2000; // el HUD local se ha cerrado; se reintenta
  }
  setTimeout(pollEvents, delay);
}

function onPcEvent(ev) {
  switch (ev.type) {
    case "announce":
      $("subtitle").textContent = ev.text;
      if (ev.audio_wav_b64) playWav(ev.audio_wav_b64);
      break;
    case "wake":
      if (state !== "idle" && state !== "error") break;
      wakeActive = true;
      chime();
      flowReset();
      setState("listening", "te escucho");
      fire(REGION.auditory, "«Hey Jarvis»");
      break;
    case "utterance": {
      if (!wakeActive) break;
      wakeActive = false;
      const bytes = Uint8Array.from(atob(ev.audio_wav_b64), (c) => c.charCodeAt(0));
      const form = new FormData();
      form.append("audio", new Blob([bytes], { type: "audio/wav" }), "audio.wav");
      form.append("session", SESSION);
      if (pcApps) form.append("pc_apps", pcApps.join(","));
      ask("/api/voice", { method: "POST", body: form });
      break;
    }
    case "wake_cancel":
      if (!wakeActive) break;
      wakeActive = false;
      setState("idle");
      break;
  }
}

// Pitido corto al oír "Hey Jarvis".
function chime() {
  const ctx = ensureAudio();
  const now = ctx.currentTime;
  [880, 1320].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0, now + i * 0.09);
    gain.gain.linearRampToValueAtTime(0.12, now + i * 0.09 + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.09 + 0.16);
    osc.connect(gain).connect(outAnalyser);
    osc.start(now + i * 0.09);
    osc.stop(now + i * 0.09 + 0.18);
  });
}

// --- cerebro: flujo de pensamiento en directo -----------------------------------------
// Un cerebro 3D de partículas. Cada paso del turno enciende su región y un impulso viaja
// desde la región anterior: oído -> hipocampo (memoria) -> prefrontal (razona) -> tools -> lenguaje -> voz.

const REGIONS = [
  { id: "prefrontal", name: "PREFRONTAL", role: "razonamiento", pos: [0.8, 0.2, 0], color: [255, 77, 141] },
  { id: "motor", name: "CÓRTEX MOTOR", role: "acciones", pos: [0.2, 0.66, 0], color: [255, 96, 96] },
  { id: "association", name: "ASOCIACIÓN", role: "consultas", pos: [-0.42, 0.5, 0], color: [179, 136, 255] },
  { id: "auditory", name: "AUDITIVO", role: "oído", pos: [0.05, -0.2, 0.5], color: [76, 201, 240] },
  { id: "hippocampus", name: "HIPOCAMPO", role: "memoria", pos: [-0.2, -0.12, -0.25], color: [94, 227, 161] },
  { id: "language", name: "LENGUAJE", role: "respuesta", pos: [0.52, -0.24, 0.3], color: [255, 181, 71] },
  { id: "cerebellum", name: "CEREBELO", role: "voz", pos: [-0.6, -0.52, 0], color: [77, 124, 255] },
  { id: "visual", name: "VISUAL", role: "cámara · nivel 4", pos: [-0.92, 0.08, 0], color: [45, 212, 191], planned: true },
  { id: "thalamus", name: "TÁLAMO", role: "avisos", pos: [-0.05, 0.12, 0], color: [210, 230, 255] },
];
const REGION = Object.fromEntries(REGIONS.map((r, i) => [r.id, i]));

const TOOL_LABEL = {
  get_datetime: "hora",
  get_weather: "tiempo",
  memory_save: "guardar recuerdo",
  memory_search: "buscar recuerdo",
  memory_update: "corregir recuerdo",
  memory_forget: "olvidar recuerdo",
  obsidian_search: "Obsidian · buscar",
  obsidian_read: "Obsidian · leer",
  obsidian_create_note: "Obsidian · nota nueva",
  obsidian_append: "Obsidian · añadir",
  obsidian_daily_note: "Obsidian · diario",
  pc_open_app: "PC · abrir app",
  pc_open_url: "PC · abrir web",
  pc_volume: "PC · volumen",
  pc_media: "PC · música",
  pc_timer: "PC · temporizador",
  truenas_status: "TrueNAS",
  truenas_app_restart: "TrueNAS · reiniciar app",
  web_search: "buscar en internet",
  wikipedia: "Wikipedia",
  news: "noticias",
  convert: "conversión",
  calendar_agenda: "agenda",
  reminder_set: "nuevo recordatorio",
  reminder_list: "recordatorios",
  reminder_cancel: "cancelar recordatorio",
  wake_on_lan: "encender equipo",
  home_status: "casa · estado",
  home_control: "casa · control",
  spotify_play: "Spotify · poner",
  spotify_control: "Spotify · control",
  spotify_now_playing: "Spotify · qué suena",
};
// Acciones (cambian algo fuera) -> córtex motor; el resto son consultas -> asociación.
const MOTOR_TOOLS = new Set(["truenas_app_restart", "wake_on_lan", "home_control", "spotify_play", "spotify_control"]);

function toolRegion(name) {
  if (name.startsWith("reminder_")) return REGION.thalamus;
  if (name.startsWith("memory_")) return REGION.hippocampus;
  if (name.startsWith("pc_") || /^obsidian_(create|append|daily)/.test(name) || MOTOR_TOOLS.has(name)) return REGION.motor;
  return REGION.association;
}

// Forma: cerebro (elipsoide), cerebelo y tronco. Más densidad cerca de la superficie (córtex).
function insideBrain(x, y, z) {
  const cerebrum = x * x + ((y - 0.05) / 0.7) ** 2 + (z / 0.62) ** 2;
  if (cerebrum <= 1 && y > -0.48 && !(x < -0.35 && y < -0.3)) {
    if (Math.abs(z) < 0.045 && y > -0.2) return null; // cisura entre los dos hemisferios
    return { q: Math.sqrt(cerebrum), region: null };
  }
  if (((x + 0.58) / 0.34) ** 2 + ((y + 0.52) / 0.2) ** 2 + (z / 0.42) ** 2 <= 1) return { q: 0.8, region: REGION.cerebellum };
  if (((x + 0.22) / 0.1) ** 2 + ((y + 0.72) / 0.3) ** 2 + (z / 0.1) ** 2 <= 1) return { q: 0.5, region: REGION.cerebellum };
  return null;
}

function nearestRegion(x, y, z) {
  let best = 0;
  let bestD = Infinity;
  REGIONS.forEach((r, i) => {
    if (i === REGION.cerebellum) return;
    const d = (x - r.pos[0]) ** 2 + (y - r.pos[1]) ** 2 * 1.2 + (z - r.pos[2]) ** 2 * 0.5;
    if (d < bestD) [best, bestD] = [i, d];
  });
  return best;
}

const lowPower = matchMedia("(pointer: coarse)").matches;
const neurons = [];
const edges = REGIONS.map(() => []); // por región: un único trazo por color
const fibers = REGIONS.map(() => []);
(function buildBrain() {
  const target = lowPower ? 600 : 1100;
  while (neurons.length < target) {
    const x = Math.random() * 2 - 1;
    const y = Math.random() * 1.8 - 1.05;
    const z = Math.random() * 1.3 - 0.65;
    const hit = insideBrain(x, y, z);
    if (!hit || Math.random() > 0.2 + 0.8 * hit.q ** 3) continue;
    neurons.push({ p: [x, y, z], region: hit.region ?? nearestRegion(x, y, z), tw: Math.random() * 6.28, s: [0, 0, 1, 1] });
  }
  for (let i = 0; i < neurons.length; i++) {
    const a = neurons[i].p;
    const near = [];
    for (let j = i + 1; j < neurons.length; j++) {
      const b = neurons[j].p;
      const d = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
      if (d < 0.03) near.push([d, j]);
    }
    near.sort((m, n) => m[0] - n[0]);
    for (const [, j] of near.slice(0, 2)) edges[neurons[i].region].push([i, j]);
  }
  // Fibras largas que salen del córtex (el "pelo" luminoso del vídeo).
  const surface = neurons.filter((n) => Math.hypot(...n.p) > 0.8);
  for (let k = 0; k < (lowPower ? 40 : 80); k++) {
    const n = surface[(Math.random() * surface.length) | 0];
    const out = 1.35 + Math.random() * 0.55;
    const bend = () => (Math.random() - 0.5) * 0.5;
    fibers[n.region].push({
      a: n.p,
      c: [n.p[0] * 1.3 + bend(), n.p[1] * 1.3 + bend(), n.p[2] * 1.3 + bend()],
      b: [n.p[0] * out + bend(), n.p[1] * out + bend(), n.p[2] * out + bend()],
    });
  }
})();

const regionState = REGIONS.map(() => ({ act: 0, base: 0, pending: false, detail: "", fail: false }));
const pulses = [];
let lastRegion = null;
let burst = 0;
let spin = -0.6; // giro acumulado del modelo (una vuelta cada ~40 s)

function fire(idx, detail) {
  const r = regionState[idx];
  r.act = 1;
  if (detail !== undefined) r.detail = detail;
  burst = 0.6;
  if (lastRegion !== null && lastRegion !== idx) {
    pulses.push({ from: lastRegion, to: idx, t: 0, dur: reducedMotion ? 0.01 : 0.55 });
  }
  lastRegion = idx;
  const name = REGIONS[idx].name;
  const trail = $("trail");
  if (!trail.dataset.last || trail.dataset.last !== name) {
    trail.textContent = trail.textContent ? `${trail.textContent} → ${name}` : name;
    trail.dataset.last = name;
  }
}

function flowReset() {
  regionState.forEach((r) => Object.assign(r, { pending: false, detail: "", fail: false }));
  lastRegion = null;
  $("trail").textContent = "";
  $("trail").dataset.last = "";
  $("flow").classList.remove("past");
}

function argsSummary(args) {
  return Object.values(args || {})
    .filter((v) => v !== "" && v != null)
    .map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v)))
    .join(" · ");
}

function onFlow(ev) {
  switch (ev.type) {
    case "listening":
      setState("thinking", "transcribiendo");
      fire(REGION.auditory, "transcribiendo…");
      break;
    case "heard":
      if (!ev.text) break;
      $("you").textContent = ev.text;
      fire(REGION.auditory, ev.text);
      break;
    case "memory": {
      const items = ev.items || [];
      fire(REGION.hippocampus, items.length ? `${items.length} recuerdo${items.length > 1 ? "s" : ""} · ${items[0].text}` : "sin recuerdos relevantes");
      break;
    }
    case "thinking":
      setState("thinking", ev.round > 1 ? `razonando · ronda ${ev.round}` : "razonando");
      fire(REGION.prefrontal, ev.round > 1 ? `ronda ${ev.round}` : "razonando");
      break;
    case "tool": {
      const idx = toolRegion(ev.name);
      const label = TOOL_LABEL[ev.name] || ev.name;
      Object.assign(regionState[idx], { pending: true, fail: false });
      fire(idx, [label, argsSummary(ev.args)].filter(Boolean).join(" · "));
      setState("thinking", label);
      break;
    }
    case "tool_result": {
      const idx = toolRegion(ev.name);
      const label = TOOL_LABEL[ev.name] || ev.name;
      Object.assign(regionState[idx], { pending: false, fail: !ev.ok });
      fire(idx, `${ev.ok ? "✓" : "✗"} ${label} · ${ev.ms} ms · ${ev.text}`);
      break;
    }
    case "reply":
      $("subtitle").textContent = ev.text;
      fire(REGION.language, ev.text);
      break;
    case "speaking":
      setState("thinking", "poniendo voz");
      fire(REGION.cerebellum, "sintetizando voz");
      break;
  }
}

// Etiquetas de región (HTML sobre el lienzo) y panel de estado del córtex.
const labels = REGIONS.map((r, i) => {
  const el = document.createElement("div");
  el.className = "region";
  el.style.setProperty("--c", `rgb(${r.color.join(",")})`);
  const b = document.createElement("b");
  b.textContent = r.name;
  const span = document.createElement("span");
  el.append(b, span);
  $("flow").append(el);
  const row = document.createElement("li");
  row.style.setProperty("--c", `rgb(${r.color.join(",")})`);
  const name = document.createElement("span");
  name.textContent = r.name;
  const status = document.createElement("em");
  row.append(name, status);
  $("cortex").append(row);
  return { el, span, row, status, i };
});

function updateCortexPanel() {
  labels.forEach(({ row, status, i }) => {
    const r = regionState[i];
    const text = REGIONS[i].planned ? "PLANIFICADO" : r.pending ? "EJECUTANDO" : r.act > 0.35 ? "ACTIVO" : "EN REPOSO";
    status.textContent = text;
    row.className = REGIONS[i].planned ? "planned" : r.act > 0.35 || r.pending ? "live" : "";
  });
}
setInterval(updateCortexPanel, 250);

// --- lienzo -----------------------------------------------------------------------

const canvas = $("orb");
const g = canvas.getContext("2d");
let color = [...COLORS.idle];
let level = 0;
let last = performance.now();

function resize() {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const { width, height } = canvas.getBoundingClientRect();
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
}
addEventListener("resize", resize);

function rgba([r, gr, b], alpha) {
  return `rgba(${r | 0},${gr | 0},${b | 0},${Math.max(0, Math.min(1, alpha))})`;
}

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000) * (reducedMotion ? 0.3 : 1);
  last = now;
  const target = COLORS[state];
  color = color.map((c, i) => c + (target[i] - c) * Math.min(1, dt * 4));
  const lvl = audioLevel();
  level += (lvl - level) * Math.min(1, dt * 12);
  burst = Math.max(0, burst - dt * 2);
  if (state !== "error") $("state").style.color = rgba(color, 1);

  // Actividad de fondo según el estado (micro, pensando, hablando).
  const think = state === "thinking" ? 0.3 + 0.15 * Math.sin(now / 160) : 0;
  regionState.forEach((r, i) => {
    let base = REGIONS[i].planned ? 0.05 : 0.15;
    if (i === REGION.auditory && state === "listening") base = 0.35 + level * 0.8;
    if (i === REGION.prefrontal) base = Math.max(base, think);
    if ((i === REGION.cerebellum || i === REGION.language) && state === "speaking") base = 0.3 + level * 0.7;
    if (r.pending) base = Math.max(base, 0.6 + 0.3 * Math.sin(now / 90));
    r.act = Math.max(base, r.act - dt * 0.45);
  });

  const W = canvas.width;
  const H = canvas.height;
  const cx = W / 2;
  const cy = H * 0.5;
  const S = Math.min(W * (W < H * 1.6 ? 0.4 : 0.34), H * 0.46);
  g.clearRect(0, 0, W, H);

  // halo del estado
  const halo = g.createRadialGradient(cx, cy, 0, cx, cy, Math.min(W, H) / 2);
  halo.addColorStop(0, rgba(color, 0.1 + level * 0.15 + burst * 0.08));
  halo.addColorStop(1, rgba(color, 0));
  g.fillStyle = halo;
  g.fillRect(0, 0, W, H);

  // modelo 3D girando sobre su eje vertical, algo inclinado para ver la parte de arriba
  spin += dt * 0.16;
  const yaw = spin;
  const pitch = 0.22 + Math.sin(now / 9000) * 0.08;
  const cyw = Math.cos(yaw), syw = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const project = (p, out) => {
    const x = p[0] * cyw - p[2] * syw;
    const z1 = p[0] * syw + p[2] * cyw;
    const y = p[1] * cp - z1 * sp;
    const z = p[1] * sp + z1 * cp;
    const f = 3 / (3 + z);
    out[0] = cx + x * S * f;
    out[1] = cy - y * S * f;
    out[2] = f;
    out[3] = Math.max(0, Math.min(1, (f - 0.75) / 0.75)); // 0 = al fondo, 1 = delante
    return out;
  };
  for (const n of neurons) project(n.p, n.s);

  g.globalCompositeOperation = "lighter";
  const px = W / 900;

  // fibras
  const tmpA = [0, 0, 0, 0], tmpB = [0, 0, 0, 0], tmpC = [0, 0, 0, 0];
  fibers.forEach((list, i) => {
    const act = regionState[i].act;
    g.strokeStyle = rgba(REGIONS[i].color, 0.05 + act * 0.35);
    g.lineWidth = Math.max(0.6, px * (0.8 + act));
    g.beginPath();
    for (const f of list) {
      project(f.a, tmpA);
      project(f.c, tmpC);
      project(f.b, tmpB);
      g.moveTo(tmpA[0], tmpA[1]);
      g.quadraticCurveTo(tmpC[0], tmpC[1], tmpB[0], tmpB[1]);
    }
    g.stroke();
  });

  // sinapsis
  edges.forEach((list, i) => {
    const act = regionState[i].act;
    g.strokeStyle = rgba(REGIONS[i].color, 0.1 + act * 0.5);
    g.lineWidth = Math.max(0.5, px * 0.9);
    g.beginPath();
    for (const [a, b] of list) {
      g.moveTo(neurons[a].s[0], neurons[a].s[1]);
      g.lineTo(neurons[b].s[0], neurons[b].s[1]);
    }
    g.stroke();
  });

  // neuronas
  for (const n of neurons) {
    const act = regionState[n.region].act;
    const tw = 0.5 + 0.5 * Math.sin(now / 700 + n.tw);
    const depth = 0.35 + 0.65 * n.s[3];
    const size = Math.max(1, px * (1.4 + act * 1.6) * n.s[2]);
    g.fillStyle = rgba(REGIONS[n.region].color, (0.35 + tw * 0.3 + act * 0.6) * depth);
    g.fillRect(n.s[0] - size / 2, n.s[1] - size / 2, size, size);
    if (act > 0.3 && tw > 0.6 && n.s[3] > 0.3) {
      // brillo alrededor de las neuronas activas
      g.fillStyle = rgba(REGIONS[n.region].color, (act - 0.3) * 0.25);
      g.beginPath();
      g.arc(n.s[0], n.s[1], size * 2.2, 0, Math.PI * 2);
      g.fill();
    }
  }

  // impulsos entre regiones
  const centers = REGIONS.map((r) => project(r.pos, [0, 0, 0, 0]));
  for (let k = pulses.length - 1; k >= 0; k--) {
    const p = pulses[k];
    p.t += dt / p.dur;
    const a = centers[p.from], b = centers[p.to];
    const mx = (a[0] + b[0]) / 2 + (((a[0] + b[0]) / 2 - cx) * 0.4);
    const my = (a[1] + b[1]) / 2 + (((a[1] + b[1]) / 2 - cy) * 0.4) - S * 0.12;
    const at = (t) => {
      const u = 1 - t;
      return [u * u * a[0] + 2 * u * t * mx + t * t * b[0], u * u * a[1] + 2 * u * t * my + t * t * b[1]];
    };
    const col = REGIONS[p.to].color;
    for (let s = 0; s < 8; s++) {
      const t = Math.min(1, p.t) - s * 0.035;
      if (t < 0) break;
      const [x, y] = at(t);
      g.fillStyle = rgba(col, (1 - s / 8) * 0.9);
      g.beginPath();
      g.arc(x, y, px * (4 - s * 0.4), 0, Math.PI * 2);
      g.fill();
    }
    if (p.t >= 1.25) pulses.splice(k, 1);
  }
  g.globalCompositeOperation = "source-over";

  // etiquetas de región
  const dpr = W / canvas.getBoundingClientRect().width || 1;
  labels.forEach(({ el, span, i }) => {
    const r = regionState[i];
    const [x, y] = centers[i];
    el.style.transform = `translate(${canvas.offsetLeft + x / dpr + 12}px, ${canvas.offsetTop + y / dpr - 12}px)`;
    el.classList.toggle("on", r.act > 0.35 || r.pending);
    el.classList.toggle("fail", r.fail);
    el.classList.toggle("back", centers[i][3] < 0.4); // región en la cara oculta del modelo
    if (span.textContent !== (r.detail || REGIONS[i].role)) span.textContent = r.detail || REGIONS[i].role;
  });

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
    ({ mode, pc_apps: pcApps, wake: wakeEnabled = false } = await (await fetch("/hud/config")).json());
  } catch {
    pcApps = null;
  }
  noticesLoop();
  if (mode === "server") {
    $("hint").textContent = matchMedia("(pointer: coarse)").matches
      ? "MANTÉN PULSADO EL CEREBRO PARA HABLAR"
      : "MANTÉN PULSADO EL CEREBRO O LA BARRA ESPACIADORA";
    if (!getToken()) askToken();
  } else {
    pollEvents(); // avisos del PC: temporizadores y "Hey Jarvis" (solo en modo local)
    if (wakeEnabled) {
      const hint = "DI «HEY JARVIS», MANTÉN PULSADO EL CEREBRO O LA BARRA ESPACIADORA";
      // El navegador no deja sonar nada hasta el primer clic o tecla en la página.
      if (ensureAudio().state === "suspended") {
        $("hint").textContent = "HAZ CLIC EN LA PÁGINA UNA VEZ PARA ACTIVAR EL SONIDO";
        const unlock = () => {
          ensureAudio();
          $("hint").textContent = hint;
        };
        addEventListener("pointerdown", unlock, { once: true });
        addEventListener("keydown", unlock, { once: true });
      } else {
        $("hint").textContent = hint;
      }
    }
  }
}
init();
