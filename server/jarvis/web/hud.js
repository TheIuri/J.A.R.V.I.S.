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
  idle: [41, 151, 255],
  listening: [100, 210, 255],
  thinking: [191, 90, 242],
  speaking: [255, 159, 10],
  error: [255, 69, 58],
};
const STATE_LABEL = {
  idle: "En espera",
  listening: "Escuchando",
  thinking: "Pensando",
  speaking: "Hablando",
  error: "Error",
};

const $ = (id) => document.getElementById(id);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

let state = "idle";
let mode = "local";
let pcApps = null;
let claudeModels = []; // modo Claude (membresía): en el HUD del PC o en el NAS
let onlyClaude = false; // HUD_MODELS=claude: en CEREBRO solo los de Claude
let defaultModel = ""; // DEFAULT_MODEL: el cerebro por defecto si este navegador no eligió otro
const MODEL_KEY = "jarvis_model";
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
  if (resp.ok) {
    $("login").hidden = true;
    loadModels(); // con token ya se pueden pedir los modelos
    loadAgents();
    loadProfiles();
    loadCalendar();
  }
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
  // Indicador de la barra superior.
  const live = $("live");
  live.style.setProperty("--live", `rgb(${COLORS[next].join(",")})`);
  live.classList.toggle("busy", busy);
  $("live-text").textContent = { idle: "En espera", listening: "Escuchando", thinking: "Pensando", speaking: "Hablando", error: "Error" }[next];
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
  await askVoice(wav);
}

// --- cámara (región VISUAL) -------------------------------------------------------------
// Solo mientras está encendida: cada pregunta lleva una foto de ese momento. No se guarda nada.

let camStream = null;

async function toggleCamera() {
  const btn = $("camera");
  const visual = REGIONS[REGION.visual];
  if (camStream) {
    camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
    $("cam").srcObject = null;
    $("cam").hidden = true;
    Object.assign(visual, { planned: true, role: "cámara apagada" });
  } else {
    try {
      // En el móvil, la cámara trasera; en el PC, la webcam.
      camStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } },
      });
    } catch {
      setState("error", "sin acceso a la cámara");
      return;
    }
    $("cam").srcObject = camStream;
    $("cam").hidden = false;
    await $("cam").play().catch(() => {});
    Object.assign(visual, { planned: false, role: "cámara encendida" });
  }
  btn.classList.toggle("on", !!camStream);
  btn.setAttribute("aria-pressed", String(!!camStream));
  btn.querySelector("span").textContent = camStream ? "Cámara encendida" : "Cámara";
}

function snapshot() {
  const video = $("cam");
  if (!camStream || !video.videoWidth) return null;
  const scale = Math.min(1, 960 / video.videoWidth);
  const c = document.createElement("canvas");
  c.width = Math.round(video.videoWidth * scale);
  c.height = Math.round(video.videoHeight * scale);
  c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.75).split(",")[1];
}

$("camera").addEventListener("click", toggleCamera);

// --- modelo -----------------------------------------------------------------------

// Color de cada proveedor en el selector y en el indicador.
const MODEL_COLORS = {
  "": [41, 151, 255],
  groq: [255, 122, 69],
  gemini: [122, 162, 255],
  openrouter: [179, 136, 255],
  ollama: [120, 220, 170],
  anthropic: [217, 119, 87],
  "claude-sonnet": [217, 119, 87],
  "claude-opus": [236, 146, 110],
  "claude-haiku": [244, 180, 140],
};
const PROVIDER_NAME = { groq: "Groq", gemini: "Gemini", cerebras: "Cerebras", mistral: "Mistral", opencode: "OpenCode", openrouter: "OpenRouter", ollama: "Ollama" };
let currentModel = "";

// "cerebras:gpt-oss-120b" -> "Cerebras · gpt-oss-120b": que se vea de un vistazo quien ha contestado.
function providerLabel(spec) {
  if (!spec) return "";
  const i = String(spec).indexOf(":");
  if (i < 0) return PROVIDER_NAME[spec] || spec;
  const prov = spec.slice(0, i);
  return `${PROVIDER_NAME[prov] || prov.charAt(0).toUpperCase() + prov.slice(1)} · ${spec.slice(i + 1)}`;
}

// Si ha contestado otro modelo que el elegido (limite, caida...), es un respaldo.
function isBackup(spec) {
  if (!spec || !currentModel) return false;
  // Con Claude elegido, si contesta otro (Groq...) es el respaldo por falta de cupo.
  if (currentModel.startsWith("claude-")) return !/^Claude /.test(spec);
  return spec !== currentModel && !spec.startsWith(`${currentModel}:`);
}

function showSource(spec) {
  const el = $("source");
  if (!el) return;
  el.hidden = !spec;
  if (!spec) return;
  const backup = isBackup(spec);
  el.textContent = `${providerLabel(spec)}${backup ? " · respaldo" : ""}`;
  el.title = backup ? "El modelo elegido no estaba disponible; ha contestado este" : "Modelo que ha contestado";
  el.classList.toggle("backup", backup);
  el.style.setProperty("--swatch", `rgb(${modelRgb(spec).join(",")})`);
}
let modelOptions = [];

function selectedModel() {
  return currentModel;
}

function isClaude() {
  return selectedModel().startsWith("claude-");
}

function modelRgb(id) {
  const fam = /^claude-(sonnet|opus|haiku)/.exec(String(id));
  if (fam) return MODEL_COLORS[`claude-${fam[1]}`];
  return MODEL_COLORS[id] || MODEL_COLORS[String(id).split(":")[0]] || [141, 151, 173];
}

async function loadModels() {
  const claudeOnly = onlyClaude && claudeModels.length > 0;
  const options = claudeOnly ? [] : [{ id: "", label: "Automático", detail: "La cadena de JARVIS, con respaldo", group: "JARVIS · servidor" }];
  if (!claudeOnly) try {
    const resp = await api("/api/models");
    if (resp.ok) {
      // Por proveedor: primero los de tu cadena y después el resto de su catálogo.
      const models = (await resp.json()).models;
      const providers = [...new Set(models.map((m) => m.provider || String(m.id).split(":")[0]))];
      for (const prov of providers) {
        const own = models.filter((m) => (m.provider || String(m.id).split(":")[0]) === prov);
        own.sort((a, b) => Number(b.chain !== false) - Number(a.chain !== false));
        for (const m of own) {
          const model = m.model || String(m.id).split(":").slice(1).join(":");
          options.push({
            id: m.id, label: model, group: PROVIDER_NAME[prov] || prov.charAt(0).toUpperCase() + prov.slice(1),
            detail: m.chain === false ? "" : "En tu cadena", provider: prov,
          });
        }
      }
    }
  } catch {
    /* servidor antiguo: solo automático */
  }
  claudeModels.forEach((m) => options.push({ id: m.id, label: m.label, detail: mode === "server" ? "Tu membresía, desde el NAS" : "Tu membresía, desde este PC", group: "Claude · membresía" }));
  modelOptions = options;
  let saved = null; // null = este navegador nunca eligió ("" es Automático elegido a propósito)
  try {
    saved = localStorage.getItem(MODEL_KEY);
  } catch {
    /* sin almacenamiento */
  }
  renderModelMenu();
  const known = (id) => id !== null && options.some((m) => m.id === id);
  applyModel(known(saved) ? saved : known(defaultModel) ? defaultModel : "", false);
}

function renderModelMenu() {
  const menu = $("model-menu");
  menu.textContent = "";
  let group = null;
  for (const m of modelOptions) {
    if (m.group !== group) {
      group = m.group;
      const h = document.createElement("div");
      h.className = "menu-group";
      h.textContent = group;
      menu.append(h);
    }
    const item = document.createElement("button");
    item.type = "button";
    item.className = "menu-item";
    item.setAttribute("role", "menuitemradio");
    item.dataset.id = m.id;
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.setProperty("--swatch", `rgb(${modelRgb(m.id).join(",")})`);
    const text = document.createElement("span");
    text.className = "mi-text";
    const name = document.createElement("span");
    name.textContent = m.label;
    const small = document.createElement("small");
    small.textContent = m.detail || "";
    text.append(name, small);
    const meter = document.createElement("span");
    meter.className = "mi-quota";
    meter.dataset.model = m.id;
    text.append(meter);
    item.append(sw, text);
    item.insertAdjacentHTML("beforeend", '<svg class="icon"><use href="#i-check"/></svg>');
    item.addEventListener("click", () => {
      applyModel(m.id, true);
      toggleModelMenu(false);
      $("model-btn").focus();
    });
    menu.append(item);
  }
}

function applyModel(id, announce) {
  const m = modelOptions.find((o) => o.id === id) || modelOptions[0];
  if (!m) return;
  currentModel = m.id;
  const rgb = `rgb(${modelRgb(m.id).join(",")})`;
  $("model-btn").style.setProperty("--swatch", rgb);
  $("model-swatch").style.setProperty("--swatch", rgb);
  $("model-label").textContent = m.provider ? `${m.group} · ${m.label}` : m.label;
  $("model-menu").querySelectorAll(".menu-item").forEach((el) => el.setAttribute("aria-checked", String(el.dataset.id === m.id)));
  try {
    localStorage.setItem(MODEL_KEY, m.id);
  } catch {
    /* sin almacenamiento */
  }
  if (announce) trace("Cerebro", `ahora piensa con ${m.label}`, modelRgb(m.id));
}

function toggleModelMenu(open) {
  const menu = $("model-menu");
  if (open) loadUsage();
  menu.hidden = !open;
  $("model-btn").setAttribute("aria-expanded", String(open));
  if (open) (menu.querySelector('[aria-checked="true"]') || menu.querySelector(".menu-item"))?.focus();
}

$("model-btn").addEventListener("click", () => toggleModelMenu($("model-menu").hidden));
$("model-menu").addEventListener("keydown", (e) => {
  const items = [...$("model-menu").querySelectorAll(".menu-item")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
  } else if (e.key === "Escape") {
    toggleModelMenu(false);
    $("model-btn").focus();
  }
});
addEventListener("pointerdown", (e) => {
  if (!e.target.closest(".model-switch") && !$("model-menu").hidden) toggleModelMenu(false);
});

// --- cuota de los modelos: cuánto queda de cada uno (capas gratuitas con límite) --------------------

const QUOTA_TEXT = { ok: "Con margen", low: "Queda poco", limit: "En su límite", error: "Con errores" };
const QUOTA_RANK = { ok: 0, error: 1, low: 2, limit: 3 };
let usage = null;

function fmtNum(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n);
}

function fmtWait(s) {
  if (s >= 2 * 86400) return `${Math.round(s / 86400)} días`;
  return s >= 3600 ? `${Math.round(s / 3600)} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`;
}

// Lo que queda, en una línea: "1.2k tokens/min · 850 peticiones/día" o el aviso de límite.
function quotaLine(m, short = false) {
  if (m.state === "limit") return m.blocked_for ? `En su límite · vuelve en ${fmtWait(m.blocked_for)}` : "En su límite";
  let meters = m.meters.filter((x) => x.limit);
  if (short && meters.length > 1) {
    // Solo el más apurado, para que quepa en una línea del menú.
    meters = [meters.reduce((a, b) => ((b.remaining ?? b.limit) / b.limit < (a.remaining ?? a.limit) / a.limit ? b : a))];
  }
  const parts = meters.map((x) =>
    `${fmtNum(x.remaining ?? x.limit)} ${x.kind === "tokens" ? "tokens" : "peticiones"}${x.window ? `/${x.window}` : ""}`);
  return parts.length ? `Quedan ${parts.join(" · ")}` : `Hoy: ${m.requests} peticiones · ${fmtNum(m.tokens)} tokens`;
}

// Fracción que queda del contador más apurado (para la barra).
function quotaLeft(m) {
  if (m.state === "limit") return 0;
  const fr = m.meters.filter((x) => x.limit).map((x) => (x.remaining ?? x.limit) / x.limit);
  return fr.length ? Math.min(...fr) : null;
}

function quotaBar(left, state) {
  const bar = document.createElement("span");
  bar.className = `qbar ${state}`;
  const fill = document.createElement("i");
  fill.style.width = `${Math.round((left ?? 1) * 100)}%`;
  bar.append(fill);
  return bar;
}

// --- cuota de Claude (membresía): ventanas de 5 h y semanal, y lo gastado hoy ---------------------
const CLAUDE_WINDOW = { five_hour: "Sesión de 5 h", seven_day: "Semana", seven_day_opus: "Semana · Opus",
  seven_day_sonnet: "Semana · Sonnet", seven_day_overage_included: "Semana (con extra)", overage: "Uso extra" };

function claudeWindows() {
  const lim = usage?.claude?.limits;
  if (!lim || !lim.status) return [];
  const out = [];
  if (lim.type) out.push({ key: lim.type, used: lim.used, resets_at: lim.resets_at });
  for (const [key, w] of Object.entries(lim.windows || {})) if (key !== lim.type) out.push({ key, ...w });
  return out;
}

function claudeQuota(modelId) {
  const c = usage?.claude;
  if (!c) return null;
  const lim = c.limits || {};
  const state = lim.status === "rejected" ? "limit" : lim.status === "allowed_warning" ? "low" : "ok";
  const wins = claudeWindows().filter((w) => w.used != null);
  const worst = wins.length ? wins.reduce((a, b) => (b.used > a.used ? b : a)) : null;
  const st = (c.models || []).find((m) => m.id === modelId);
  const today = st ? `Hoy ${st.turns} pregunta${st.turns === 1 ? "" : "s"} · ${fmtNum(st.input_tokens + st.output_tokens)} tokens` : "Hoy sin usar";
  const wait = worst?.resets_at ? Math.max(0, Math.round(worst.resets_at - Date.now() / 1000)) : 0;
  const line = worst
    ? `${CLAUDE_WINDOW[worst.key] || worst.key}: ${Math.round(worst.used * 100)}% usado${wait ? ` · se renueva en ${fmtWait(wait)}` : ""}`
    : state === "limit" ? `En su límite${wait ? ` · vuelve en ${fmtWait(wait)}` : ""}` : today;
  return { state, left: worst ? 1 - worst.used : state === "limit" ? 0 : null, line, today, note: line };
}

function renderClaudeUsage(list) {
  const c = usage?.claude;
  if (!c) return;
  const q = claudeQuota(currentModel);
  const li = document.createElement("li");
  li.className = q.state;
  li.innerHTML = '<div class="q-head"><span class="q-name">Claude · membresía</span><span class="q-role">tu plan</span><span class="q-state"></span></div>';
  li.querySelector(".q-state").className = `q-state ${q.state}`;
  li.querySelector(".q-state").textContent = QUOTA_TEXT[q.state];
  for (const w of claudeWindows()) {
    if (w.used == null) continue;
    const row = document.createElement("div");
    row.className = "q-meter";
    const label = document.createElement("span");
    label.textContent = CLAUDE_WINDOW[w.key] || w.key;
    const num = document.createElement("span");
    num.className = "num";
    const wait = w.resets_at ? Math.max(0, Math.round(w.resets_at - Date.now() / 1000)) : 0;
    num.textContent = `${Math.round(w.used * 100)}% usado${wait ? ` · ${fmtWait(wait)}` : ""}`;
    row.append(label, quotaBar(w.used, q.state), num); // barra = lo usado, como en la app de Claude
    li.append(row);
  }
  for (const m of c.models || []) {
    const foot = document.createElement("p");
    foot.className = "q-foot";
    foot.textContent = [`${m.label}: ${m.turns} pregunta${m.turns === 1 ? "" : "s"}`, `${fmtNum(m.input_tokens)} entrada · ${fmtNum(m.output_tokens)} salida`,
      m.cache_tokens ? `${fmtNum(m.cache_tokens)} en caché` : "", m.web_searches ? `${m.web_searches} búsquedas` : "",
      m.errors ? `${m.errors} fallos` : ""].filter(Boolean).join(" · ");
    li.append(foot);
  }
  if (!claudeWindows().length) {
    const foot = document.createElement("p");
    foot.className = "q-foot";
    foot.textContent = "Los límites de tu plan aparecen tras la primera pregunta a Claude.";
    li.append(foot);
  }
  list.append(li);
}

// Estado del cerebro elegido: en automático manda el primero de la cadena que no esté en su límite.
function activeQuota() {
  if (!usage) return null;
  const byName = new Map(usage.models.map((m) => [m.name, m]));
  const chain = usage.chain.map((n) => byName.get(n)).filter(Boolean);
  if (isClaude()) return claudeQuota(currentModel);
  if (!chain.length) return null;
  if (currentModel) {
    const own = usage.models.filter((m) => m.name === currentModel || m.provider === currentModel);
    return own.sort((a, b) => QUOTA_RANK[b.state] - QUOTA_RANK[a.state])[0] || null;
  }
  const first = chain[0];
  if (first.state !== "limit") return first;
  const backup = chain.find((m) => m.state !== "limit");
  return backup ? { ...backup, state: "low", note: `${first.name} en su límite; responde ${backup.name}` } : first;
}

function renderUsage() {
  if (!usage) return;
  const dot = $("quota-dot");
  const active = activeQuota();
  dot.hidden = !active;
  if (active) {
    dot.className = `quota-dot ${active.state}`;
    dot.title = active.note || `${QUOTA_TEXT[active.state]} · ${quotaLine(active)}`;
  }
  // En el menú: la cuota de cada proveedor bajo su nombre.
  document.querySelectorAll("#model-menu .mi-quota").forEach((el) => {
    const models = usage.models.filter((m) => m.name === el.dataset.model);
    el.textContent = "";
    if (el.dataset.model.startsWith("claude-")) {
      const q = claudeQuota(el.dataset.model);
      if (!q) return;
      el.className = `mi-quota ${q.state}`;
      el.append(document.createTextNode(q.today)); // el límite es del plan: se ve en el punto y en Sesión
      return;
    }
    if (!models.length) return;
    const worst = models.reduce((a, b) => (QUOTA_RANK[b.state] > QUOTA_RANK[a.state] ? b : a));
    el.className = `mi-quota ${worst.state}`;
    el.append(quotaBar(quotaLeft(worst), worst.state), document.createTextNode(quotaLine(worst, true)));
  });
  // En la pestaña Sesión: todos, conversación y agentes.
  const list = $("quota-list");
  list.textContent = "";
  renderClaudeUsage(list);
  for (const m of usage.models) {
    const li = document.createElement("li");
    li.className = m.state;
    const head = document.createElement("div");
    head.className = "q-head";
    const name = document.createElement("span");
    name.className = "q-name";
    name.textContent = m.name;
    const role = document.createElement("span");
    role.className = "q-role";
    role.textContent = usage.chain.includes(m.name) ? "conversación" : "agentes";
    const st = document.createElement("span");
    st.className = `q-state ${m.state}`;
    st.textContent = QUOTA_TEXT[m.state];
    head.append(name, role, st);
    li.append(head);
    for (const x of m.meters.filter((x) => x.limit)) {
      const row = document.createElement("div");
      row.className = "q-meter";
      const label = document.createElement("span");
      label.textContent = `${x.kind === "tokens" ? "Tokens" : "Peticiones"}${x.window ? ` / ${x.window}` : ""}`;
      const num = document.createElement("span");
      num.className = "num";
      num.textContent = `${fmtNum(x.remaining ?? x.limit)} de ${fmtNum(x.limit)}${x.reset_in ? ` · ${fmtWait(x.reset_in)}` : ""}`;
      row.append(label, quotaBar((x.remaining ?? x.limit) / x.limit, m.state), num);
      li.append(row);
    }
    const foot = document.createElement("p");
    foot.className = "q-foot";
    foot.textContent = [
      `Hoy ${m.requests} peticiones · ${fmtNum(m.tokens)} tokens`,
      m.limited ? `${m.limited} veces en límite` : "",
      m.state === "limit" && m.blocked_for ? `vuelve en ${fmtWait(m.blocked_for)}` : "",
    ].filter(Boolean).join(" · ");
    li.append(foot);
    list.append(li);
  }
  if (!list.children.length) list.innerHTML = '<p class="empty">Aún no se ha usado ningún modelo desde que arrancó el servidor.</p>';
}

async function loadUsage() {
  if (mode === "server" && !getToken()) return;
  try {
    const resp = await api("/api/usage");
    if (!resp.ok) return;
    usage = await resp.json();
    renderUsage();
  } catch {
    /* servidor antiguo o sin conexión */
  }
}
setInterval(loadUsage, 20000);

// --- vistas: Inicio (cerebro y respuesta) y las secciones a pantalla amplia --------------------

const VIEWS = ["home", "agents", "leads", "trace", "session", "settings"];
let view = "home";

function selectView(name) {
  if (!VIEWS.includes(name)) name = "home";
  view = name;
  document.body.dataset.view = name;
  for (const btn of document.querySelectorAll("[data-view]")) {
    if (btn.dataset.view === name) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  $("workspace").hidden = name === "home";
  for (const v of VIEWS) if (v !== "home") $(`pane-${v}`).hidden = v !== name;
  if (name === "trace") setBadge("trace", (unseenTrace = 0));
  if (name !== "home") $("workspace").scrollTop = 0;
}

function setBadge(name, n) {
  const b = $(`badge-${name}`);
  b.hidden = !n;
  b.textContent = n > 99 ? "99+" : String(n);
}

for (const btn of document.querySelectorAll("[data-view]")) {
  // El botón de Ajustes vuelve a Inicio si ya estás en Ajustes.
  btn.addEventListener("click", () => selectView(btn.dataset.view === view && view !== "home" ? "home" : btn.dataset.view));
}
addEventListener("keydown", (e) => {
  if (e.key === "Escape" && view !== "home" && $("model-menu").hidden) selectView("home");
});

// --- preferencias de este dispositivo (Ajustes) -------------------------------------------------

const PREFS_KEY = "jarvis_prefs";
const prefs = { voice: true, highlight: true, labels: true, log: true, size: "normal" };
try {
  Object.assign(prefs, JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"));
} catch {
  /* sin almacenamiento: valores por defecto */
}

function applyPrefs() {
  document.body.classList.toggle("no-labels", !prefs.labels);
  document.body.classList.toggle("no-log", !prefs.log);
  document.body.classList.toggle("big-answers", prefs.size === "big");
  for (const k of ["voice", "highlight", "labels", "log"]) $(`pref-${k}`).checked = !!prefs[k];
  for (const b of document.querySelectorAll("[data-size]")) b.setAttribute("aria-pressed", String(b.dataset.size === prefs.size));
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* sin almacenamiento: vale para esta visita */
  }
  applyPrefs();
}

for (const k of ["voice", "highlight", "labels", "log"]) {
  $(`pref-${k}`).addEventListener("change", (e) => {
    prefs[k] = e.target.checked;
    savePrefs();
    if (k === "highlight") rerenderHighlights();
  });
}
for (const b of document.querySelectorAll("[data-size]")) {
  b.addEventListener("click", () => {
    prefs.size = b.dataset.size;
    savePrefs();
  });
}
$("logout").addEventListener("click", () => {
  setToken("");
  askToken("Sesión cerrada en este dispositivo.");
});
applyPrefs();

// --- respuesta visual: los datos importantes, remarcados (sin gastar tokens) ---------------------

const MONTHS_RE = "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre";
const DAYS_RE = "lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo";
const UNITS_RE = "km/h|kms?|kilómetros|metros|cm|mm|kg|kilos|gramos|g|litros|l|ml|GB|TB|MB|GHz|MHz|kWh|kW|W|ms|segundos|minutos|min|horas|h|días|dias|semanas|meses|años|kcal";
// Por orden de prioridad: en la misma posición gana el primero.
const FACT_RULES = [
  ["money", "Precio", "(?:[$€]\\s?\\d[\\d.,]*|\\d[\\d.,]*\\s?(?:€|euros?|\\$|dólares|USD|EUR))(?![\\p{L}])"],
  ["pct", "Porcentaje", "-?\\d+(?:[.,]\\d+)?\\s?(?:%|por ciento)"],
  ["temp", "Temperatura", "-?\\d+(?:[.,]\\d+)?\\s?(?:°\\s?C|ºC|°|º|grados)"],
  ["time", "Hora", "(?<![\\d:])(?:[01]?\\d|2[0-3]):[0-5]\\d(?![\\d:])|\\ba las? \\d{1,2}(?: y (?:media|cuarto)| menos cuarto)?(?: de la (?:mañana|tarde|noche))?(?![\\d:])"],
  ["date", "Fecha", `\\b\\d{1,2} de (?:${MONTHS_RE})(?: de \\d{4})?|(?<![\\p{L}])(?:${DAYS_RE})(?:,? \\d{1,2} de (?:${MONTHS_RE})(?: de \\d{4})?|,? \\d{1,2}(?![\\d:]))?(?![\\p{L}])|(?<![\\p{L}])(?:pasado mañana|mañana|hoy|esta (?:tarde|noche))(?![\\p{L}])`],
  ["measure", "Cifra", `\\d+(?:[.,]\\d+)?\\s?(?:${UNITS_RE})(?![\\p{L}])`],
  ["num", "Cifra", "(?<![\\p{L}\\d])\\d{1,3}(?:\\.\\d{3})+(?:,\\d+)?|(?<![\\p{L}\\d.,])\\d+(?:,\\d+)?(?![\\d])"],
  ["name", "", "«[^»]{1,80}»|\"[^\"]{2,80}\""],
  ["ok", "", "(?<![\\p{L}])(?:hecho|listo|creado|guardado|apuntado|encendido|conectado|activado|confirmado|funcionando)(?![\\p{L}])"],
  ["bad", "", "(?<![\\p{L}])(?:error|fallo|caído|apagado|desconectado|sin conexión|no he podido|no puedo)(?![\\p{L}])"],
];
const FACT_RE = new RegExp(FACT_RULES.map(([k, , src]) => `(?<${k}>${src})`).join("|"), "giu");
const FACT_LABEL = Object.fromEntries(FACT_RULES.map(([k, label]) => [k, label]));
const STOP = new Set(("de del la las el los lo un una unos unas y o a al en con por para que es son será serán está están hay " +
  "sobre hasta desde entre sus su tu tus mi mis se te le les me muy más menos unos cerca casi ahora hoy ya tiene tienes " +
  "tengo va van sería hace hacen quedan queda sale salen cuesta cuestan vale valen total aproximadamente unas").split(" "));

// Troceado de un texto: [{text, kind}] (kind vacío = texto normal).
function factPieces(text) {
  const out = [];
  let at = 0;
  for (const m of String(text).matchAll(FACT_RE)) {
    const kind = Object.keys(m.groups).find((k) => m.groups[k] !== undefined);
    if (m.index > at) out.push({ text: text.slice(at, m.index), kind: "" });
    out.push({ text: m[0], kind, index: m.index });
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push({ text: text.slice(at), kind: "" });
  return out;
}

function capital(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

// Etiqueta de un dato, sacada de sus palabras vecinas: «12,5 TB libres» -> Libres, «la máxima será de 24 grados»
// -> Máxima, «la Bambu Lab A1 cuesta 349 €» -> Bambu Lab A1. Si no hay nada claro, el tipo (Hora, Fecha...).
function factLabel(text, piece, from) {
  if (piece.kind === "num" || piece.kind === "measure") {
    const next = text.slice(piece.index + piece.text.length).match(/^\s+([\p{L}]{3,})/u);
    if (next && !STOP.has(next[1].toLowerCase())) {
      piece.counted = true; // «3 recordatorios»: un número que cuenta algo vale tanto como una hora o un precio
      return capital(next[1]);
    }
  }
  const before = text.slice(from, piece.index).split(/[.;:!?\n]/).pop();
  const words = before.match(/[\p{L}\p{N}]+/gu) || [];
  while (words.length && STOP.has(words.at(-1).toLowerCase())) words.pop();
  const label = [];
  while (words.length && label.length < 3 && !STOP.has(words.at(-1).toLowerCase())) label.unshift(words.pop());
  const kept = label.filter((w) => w.length > 1 || /\d/.test(w));
  if (kept.length && (piece.kind !== "time" && piece.kind !== "date" || before.trim().split(/\s+/).length <= 3)) {
    return capital(kept.join(" "));
  }
  return FACT_LABEL[piece.kind] || "Dato";
}

// Los datos de la respuesta para las fichas grandes: hasta 4, sin repetir.
function factsOf(text) {
  const seen = new Set();
  const facts = [];
  let from = 0;
  for (const p of factPieces(text)) {
    if (!p.kind) continue;
    const end = p.index + p.text.length;
    if (FACT_LABEL[p.kind]) {
      const key = p.text.toLowerCase().replace(/\s+/g, " ");
      if (!seen.has(key)) {
        seen.add(key);
        let k = factLabel(text, p, from);
        if (facts.some((f) => f.k === k)) k = FACT_LABEL[p.kind] || "Dato"; // «3 recordatorios para mañana»
        facts.push({ k, v: p.text, kind: p.kind, counted: p.counted });
      }
    }
    from = end;
  }
  // Los números sueltos solo si no hay nada más concreto.
  const strong = facts.filter((f) => f.kind !== "num" || f.counted);
  return (strong.length ? strong : facts).slice(0, 4);
}

// Pinta el texto con los datos remarcados (siempre como texto: nunca HTML del modelo).
function renderRich(el, text) {
  el.textContent = "";
  el.dataset.raw = text;
  if (!prefs.highlight) {
    el.textContent = text;
    return;
  }
  for (const p of factPieces(text)) {
    if (!p.kind) {
      el.append(p.text);
      continue;
    }
    const mark = document.createElement("mark");
    mark.className = `hl hl-${p.kind}`;
    mark.textContent = p.text;
    el.append(mark);
  }
}

function rerenderHighlights() {
  for (const el of document.querySelectorAll("[data-raw]")) renderRich(el, el.dataset.raw);
}

function fillFacts(items, title = "") {
  const dl = $("answer-facts");
  dl.textContent = "";
  dl.hidden = !items.length || !prefs.highlight;
  dl.dataset.n = String(items.length);
  for (const item of items) {
    const box = document.createElement("div");
    box.className = `fact${item.kind ? ` f-${item.kind}` : ""}`;
    const dt = document.createElement("dt");
    dt.textContent = item.k;
    const dd = document.createElement("dd");
    dd.textContent = item.v;
    box.append(dt, dd);
    dl.append(box);
  }
  $("answer-title").hidden = !title || dl.hidden;
  $("answer-title").textContent = title;
}

let answerAt = 0;
let answerTools = [];
let answerVisual = false; // la respuesta tiene tarjeta visual (tiempo...): sus datos exactos mandan sobre el texto

// --- tarjeta del tiempo: iconos dibujados y datos exactos de la herramienta (no del texto del modelo) -----

function weatherKind(code) {
  if (code <= 1) return "clear";
  if (code === 2) return "partly";
  if (code === 3) return "cloudy";
  if (code === 45 || code === 48) return "fog";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "storm";
  return "rain";
}

const WX_CLOUD = '<path class="wx-cloud" d="M19 50h27a10 10 0 0 0 1.5-19.9A14 14 0 0 0 20.2 33 8.6 8.6 0 0 0 19 50z"/>';
const WX_SUN_RAYS = [0, 45, 90, 135, 180, 225, 270, 315]
  .map((a) => `<line x1="0" y1="-17" x2="0" y2="-22" transform="rotate(${a})"/>`).join("");

function weatherIcon(code, isDay = true) {
  const kind = weatherKind(code);
  const sun = (x, y, s) => isDay
    ? `<g class="wx-sun" transform="translate(${x} ${y}) scale(${s})"><circle r="11"/><g class="wx-rays">${WX_SUN_RAYS}</g></g>`
    : `<g class="wx-moon" transform="translate(${x} ${y}) scale(${s})"><path d="M6 -13a14 14 0 1 0 7 20 11 11 0 0 1-7-20z"/></g>`;
  const up = (svg) => `<g transform="translate(0 -7)">${svg}</g>`;
  let body;
  if (kind === "clear") body = sun(32, 32, 1);
  else if (kind === "partly") body = sun(24, 24, 0.72) + WX_CLOUD;
  else if (kind === "cloudy") body = WX_CLOUD;
  else if (kind === "fog") body = up(WX_CLOUD) + '<g class="wx-fog"><line x1="14" y1="52" x2="50" y2="52"/><line x1="20" y1="58" x2="44" y2="58"/></g>';
  else if (kind === "snow") body = up(WX_CLOUD) + '<g class="wx-snow"><circle cx="22" cy="52" r="2.4"/><circle cx="32" cy="57" r="2.4"/><circle cx="42" cy="52" r="2.4"/></g>';
  else if (kind === "storm") body = up(WX_CLOUD) + '<path class="wx-bolt" d="M34 42l-8 11h7l-4 9 11-13h-7l4-7z"/>';
  else body = up(WX_CLOUD) + '<g class="wxi-drops"><line x1="22" y1="49" x2="19" y2="57"/><line x1="32" y1="49" x2="29" y2="59"/><line x1="42" y1="49" x2="39" y2="57"/></g>';
  return `<svg class="wx wxi-${kind}" viewBox="0 0 64 64" aria-hidden="true">${body}</svg>`;
}

function showWeather(card) {
  const box = $("answer-visual");
  const now = card.now || {};
  const days = (card.days || []).filter((d) => d.min != null && d.max != null);
  const lo = Math.min(...days.map((d) => d.min));
  const hi = Math.max(...days.map((d) => d.max));
  const span = Math.max(1, hi - lo);
  const el = document.createElement("section");
  el.className = "wx-card";
  el.setAttribute("aria-label", `El tiempo en ${card.title}`);
  // Solo números y textos fijos de la herramienta; aun así, todo lo de fuera va como texto.
  el.innerHTML = `
    <div class="wx-now">
      ${weatherIcon(now.code, now.day !== false)}
      <div class="wx-temp num"></div>
      <div class="wx-what"><b></b><span></span><small></small></div>
    </div>
    <ol class="wx-days"></ol>`;
  el.querySelector(".wx-temp").textContent = now.temp != null ? `${now.temp}°` : "—";
  el.querySelector(".wx-what b").textContent = now.text ? now.text.charAt(0).toUpperCase() + now.text.slice(1) : "";
  el.querySelector(".wx-what span").textContent = card.title || "";
  el.querySelector(".wx-what small").textContent = [now.feels != null ? `Sensación ${now.feels}°` : "",
    now.wind != null ? `Viento ${now.wind} km/h` : ""].filter(Boolean).join(" · ");
  const list = el.querySelector(".wx-days");
  for (const d of days) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="wx-day"></span>${weatherIcon(d.code)}<span class="wx-max num"></span>
      <span class="wx-bar"><i></i></span><span class="wx-min num"></span><span class="wx-pop num"></span>`;
    li.title = d.text;
    li.querySelector(".wx-day").textContent = d.label.replace(/^(\p{L}{3})\p{L}+ (\d+)$/u, "$1 $2"); // Jueves 1 -> Jue 1
    li.querySelector(".wx-max").textContent = `${d.max}°`;
    li.querySelector(".wx-min").textContent = `${d.min}°`;
    const bar = li.querySelector(".wx-bar i");
    bar.style.bottom = `${((d.min - lo) / span) * 100}%`;
    bar.style.height = `${Math.max(10, ((d.max - d.min) / span) * 100)}%`;
    const rain = li.querySelector(".wx-pop");
    rain.textContent = d.rain != null && d.rain >= 20 ? `${d.rain}%` : "";
    rain.classList.toggle("wet", (d.rain || 0) >= 50);
    list.append(li);
  }
  list.dataset.n = String(days.length);
  addVisual(el);
}

// Añade una tarjeta visual a la respuesta (puede haber varias: el resumen de buenos días trae tiempo y agenda).
function addVisual(el) {
  const box = $("answer-visual");
  box.append(el);
  box.hidden = false;
  answerVisual = true;
  $("answer-empty").hidden = true;
  $("answer-card").hidden = false;
  fillFacts([]); // los datos ya están en las tarjetas, exactos
}

function node(tag, cls = "", text = "") {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
}

function httpsUrl(url) {
  return /^https:\/\//.test(url || "") ? url : "";
}

// Iconos de un solo trazo para las tarjetas (mismo estilo que el resto del HUD).
const VIS_ICON = {
  calendar: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5v10a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5zM4 10h16M8.5 3v4M15.5 3v4"/></svg>',
  bell: '<svg class="icon" viewBox="0 0 24 24"><path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15zM10 20.5a2.2 2.2 0 0 0 4 0"/></svg>',
  swap: '<svg class="icon" viewBox="0 0 24 24"><path d="M5 8.5h13l-3.5-3.5M19 15.5H6l3.5 3.5"/></svg>',
  music: '<svg class="icon" viewBox="0 0 24 24"><path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/></svg>',
};

function visHead(icon, title, sub = "") {
  const h = node("header", "vis-head");
  h.insertAdjacentHTML("afterbegin", VIS_ICON[icon] || "");
  h.append(node("b", "", title));
  if (sub) h.append(node("span", "", sub));
  return h;
}

function showMusic(c) {
  const el = node(c.url ? "a" : "section", "vis-card vis-music");
  if (c.url) {
    el.href = c.url;
    el.target = "_blank";
    el.rel = "noopener noreferrer";
  }
  const art = node("div", "vis-art");
  if (httpsUrl(c.image)) {
    const img = node("img");
    img.src = c.image;
    img.alt = `Portada de ${c.album || c.title}`;
    img.referrerPolicy = "no-referrer";
    img.onerror = () => img.remove();
    art.append(img);
  }
  art.insertAdjacentHTML("beforeend", VIS_ICON.music);
  const body = node("div", "vis-music-body");
  body.append(node("span", `vis-state${c.state === "sonando" ? " on" : ""}`, c.state === "sonando" ? "Sonando en Spotify" : "En pausa"),
    node("b", "", c.title), node("span", "", c.artist));
  if (c.album) body.append(node("small", "", c.album));
  el.append(art, body);
  addVisual(el);
}

function showAgenda(c) {
  const el = node("section", "vis-card vis-agenda");
  const events = c.events || [];
  el.append(visHead("calendar", `Agenda · ${c.title}`, events.length ? `${events.length} ${events.length === 1 ? "evento" : "eventos"}` : ""));
  if (!events.length) {
    el.append(node("p", "vis-empty", "Nada en la agenda."));
    return addVisual(el);
  }
  const list = node("ol", "vis-events");
  let day = null;
  for (const e of events) {
    if (e.day !== day && (c.title.startsWith("Próximos") || events.some((x) => x.day !== events[0].day))) {
      day = e.day;
      list.append(node("li", "vis-day", e.day));
    }
    const li = node("li", "vis-event");
    const when = node("span", "vis-when num", e.time || "Todo el día");
    if (e.end) when.append(node("small", "", e.end));
    const what = node("div", "vis-what");
    what.append(node("b", "", e.title));
    if (e.location) what.append(node("span", "", e.location));
    li.append(when, what);
    list.append(li);
  }
  el.append(list);
  addVisual(el);
}

function showReminders(c) {
  const el = node("section", "vis-card vis-reminders");
  const items = c.items || [];
  el.append(visHead("bell", "Recordatorios", items.length ? `${items.length} pendientes` : ""));
  const list = node("ul", "vis-list");
  for (const r of items) {
    const li = node("li", r.new ? "new" : "");
    li.append(node("span", "vis-when", r.when), node("b", "", r.text));
    list.append(li);
  }
  if (!items.length) list.append(node("li", "vis-empty", "No hay recordatorios pendientes."));
  el.append(list);
  addVisual(el);
}

function showConvert(c) {
  const el = node("section", "vis-card vis-convert");
  el.append(node("span", "vis-from num", c.from));
  el.insertAdjacentHTML("beforeend", VIS_ICON.swap);
  el.append(node("b", "vis-to num", c.to));
  if (c.note) el.append(node("small", "", c.note));
  addVisual(el);
}

function showClock(c) {
  const el = node("section", "vis-card vis-clock");
  el.append(node("b", "num", c.time), node("span", "", c.date));
  addVisual(el);
}

function showWiki(c) {
  const url = httpsUrl(c.url);
  const el = node(url ? "a" : "section", "vis-card vis-wiki");
  if (url) {
    el.href = url;
    el.target = "_blank";
    el.rel = "noopener noreferrer";
  }
  if (httpsUrl(c.image)) {
    const img = node("img");
    img.src = c.image;
    img.alt = c.title;
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => img.remove();
    el.append(img);
  }
  const body = node("div", "vis-wiki-body");
  body.append(node("span", "vis-src", "Wikipedia"), node("b", "", c.title), node("p", "", c.text));
  el.append(body);
  addVisual(el);
}

const VISUALS = { weather: showWeather, music: showMusic, agenda: showAgenda, reminders: showReminders,
  convert: showConvert, clock: showClock, wiki: showWiki };

function clearVisual() {
  answerVisual = false;
  $("answer-visual").hidden = true;
  $("answer-visual").textContent = "";
}

// Tarjetas que llegan de las herramientas: las del tiempo van a la respuesta; el resto, a Resultados.
function routeCards(cards) {
  const rest = [];
  for (const c of cards || []) {
    if (VISUALS[c.kind]) VISUALS[c.kind](c);
    else rest.push(c);
  }
  if (rest.length) showCards(rest);
}

// Muestra una respuesta (o un aviso) en el panel de la derecha.
function showAnswer({ q, text, label = "" }) {
  answerAt = Date.now();
  $("answer-empty").hidden = true;
  $("answer-card").hidden = false;
  $("answer-card").classList.toggle("is-notice", !!label);
  if (label) clearVisual(); // un aviso no es la respuesta del tiempo
  if (q !== undefined) $("answer-q").textContent = q ? `› ${q}` : label;
  const same = $("answer-text").dataset.raw === (text || "");
  renderRich($("answer-text"), text || "");
  if (!answerVisual) fillFacts(factsOf(text || ""));
  if (same) return; // la misma respuesta que ya llegó en directo: sin repetir la entrada
  const card = $("answer-card");
  card.classList.remove("fresh");
  void card.offsetWidth; // reinicia la animación de entrada
  card.classList.add("fresh");
}

function showQuestion(q) {
  $("answer-empty").hidden = true;
  $("answer-card").hidden = false;
  $("answer-card").classList.remove("is-notice");
  $("answer-q").textContent = `› ${q}`;
  $("answer-text").textContent = "";
  $("answer-text").dataset.raw = "";
  fillFacts([]);
  clearVisual();
  setAnswerTools([]);
}

function setAnswerTools(tools, extra = []) {
  answerTools = tools;
  const meta = $("answer-meta");
  meta.querySelectorAll(".tool-chip").forEach((c) => c.remove());
  for (const t of [...new Set(tools)].map((n) => TOOL_LABEL[n] || n).concat(extra)) {
    const chip = document.createElement("span");
    chip.className = "tool-chip";
    chip.textContent = t;
    meta.append(chip);
  }
}

// --- traza: todo lo que pasa, en una línea de tiempo --------------------------------------

const MAX_TRACE = 150;
let unseenTrace = 0;

function trace(actor, what, rgb) {
  const li = document.createElement("li");
  if (rgb) li.style.setProperty("--c", `rgb(${rgb.join(",")})`);
  const time = document.createElement("time");
  time.textContent = new Date().toLocaleTimeString("es-ES");
  const ev = document.createElement("div");
  ev.className = "ev";
  const a = document.createElement("span");
  a.className = "actor";
  a.textContent = actor;
  const w = document.createElement("span");
  w.className = "what";
  w.textContent = what ? ` ${what}` : "";
  ev.append(a, w);
  li.append(time, ev);
  const list = $("trace");
  list.prepend(li);
  while (list.children.length > MAX_TRACE) list.lastChild.remove();
  if (view !== "trace") setBadge("trace", ++unseenTrace);
  else unseenTrace = 0;
}

// Voz: normal (el NAS transcribe y piensa) o modo Claude (el NAS transcribe y Claude piensa en el PC).
async function askVoice(wav) {
  const form = new FormData();
  form.append("audio", wav, "audio.wav");
  form.append("session", SESSION);
  if (!isClaude()) {
    if (pcApps) form.append("pc_apps", pcApps.join(","));
    if (selectedModel()) form.append("model", selectedModel());
    const photo = snapshot();
    if (photo) form.append("image", photo);
    return ask("/api/voice", { method: "POST", body: form });
  }
  setState("thinking", "transcribiendo");
  let text = "";
  try {
    const resp = await api("/api/transcribe", { method: "POST", body: form });
    text = (await resp.json()).text || "";
  } catch (err) {
    return setState("error", String(err.message || err).slice(0, 120));
  }
  if (!text) {
    showAnswer({ q: "", text: "No te he oído bien, prueba otra vez.", label: "Micrófono" });
    return setState("idle");
  }
  return askClaude(text);
}

function askClaude(text) {
  return ask("/claude/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, session: SESSION, model: selectedModel() }),
  });
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
    showAnswer({ q: "", text: "No te he oído bien, prueba otra vez.", label: "Micrófono" });
    setState("idle");
    return;
  }
  if (body.cards?.length && $("cards").hidden && !answerVisual) routeCards(body.cards); // servidor sin directo
  showTurn(body);
  if (body.audio_wav_b64 && prefs.voice) await playWav(body.audio_wav_b64);
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
  showAnswer({ q: body.transcript, text: body.reply });
  showSource(body.provider);
  setAnswerTools(body.tools_used || [], (body.pc_results || []).map((r) => `PC · ${r.result}`));

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
  $("s-model").textContent = providerLabel(body.provider) || "—";
  $("s-tools").textContent = (body.tools_used || []).join(", ") || "—";

  const turn = document.createElement("div");
  turn.className = "turn";
  const u = document.createElement("div");
  u.className = "u";
  u.textContent = `› ${body.transcript}`;
  const a = document.createElement("div");
  a.className = "a";
  renderRich(a, body.reply);
  const meta = document.createElement("div");
  meta.className = "meta";
  const actions = (body.pc_results || []).map((r) => `[PC] ${r.result}`);
  meta.textContent = [providerLabel(body.provider), `${t.total ?? "?"} ms`, ...(body.tools_used || []), ...actions].join(" · ");
  turn.append(u, a, meta);
  $("log").append(turn);
  $("log").scrollTop = $("log").scrollHeight;
}

async function sendText(text) {
  if (isClaude()) return askClaude(text);
  const payload = { text, session: SESSION, pc_apps: pcApps, model: selectedModel() || null, image: snapshot() };
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
  $("answer-card").hidden = true;
  $("answer-empty").hidden = false;
  clearVisual();
  hideCards();
  showSource("");
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
    showAnswer({ q: "", text: n.text, label: `Aviso · ${n.source}` });
    setAnswerTools([]);
    fire(REGION.thalamus, n.text);
    trace(`Aviso · ${n.source}`, n.text, n.level === "critical" ? [255, 69, 58] : REGIONS[REGION.thalamus].color);
    const turn = document.createElement("div");
    turn.className = `turn notice ${n.level}`;
    const a = document.createElement("div");
    a.className = "a";
    renderRich(a, n.text);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `Aviso · ${n.source} · ${n.created.slice(11, 16)}`;
    turn.append(a, meta);
    $("log").append(turn);
    $("log").scrollTop = $("log").scrollHeight;
    // Solo habla la pestaña visible (si tienes el HUD abierto en el PC y en el móvil, no suenan los dos).
    if (prefs.voice && n.speak && n.audio_wav_b64 && document.visibilityState === "visible") playWav(n.audio_wav_b64);
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
      showAnswer({ q: "", text: ev.text, label: "Aviso · PC" });
      if (ev.audio_wav_b64 && prefs.voice) playWav(ev.audio_wav_b64);
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
      askVoice(new Blob([bytes], { type: "audio/wav" }));
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
  { id: "prefrontal", name: "Prefrontal", role: "razonamiento", pos: [0.8, 0.2, 0], color: [255, 55, 95] },
  { id: "motor", name: "Córtex motor", role: "acciones", pos: [0.2, 0.66, 0], color: [255, 159, 10] },
  { id: "association", name: "Asociación", role: "consultas", pos: [-0.42, 0.5, 0], color: [191, 90, 242] },
  { id: "auditory", name: "Auditivo", role: "oído", pos: [0.05, -0.2, 0.5], color: [100, 210, 255] },
  { id: "hippocampus", name: "Hipocampo", role: "memoria", pos: [-0.2, -0.12, -0.25], color: [48, 209, 88] },
  { id: "language", name: "Lenguaje", role: "respuesta", pos: [0.52, -0.24, 0.3], color: [255, 214, 10] },
  { id: "cerebellum", name: "Cerebelo", role: "voz", pos: [-0.6, -0.52, 0], color: [10, 132, 255] },
  { id: "visual", name: "Visual", role: "cámara apagada", pos: [-0.92, 0.08, 0], color: [102, 212, 207], planned: true },
  { id: "thalamus", name: "Tálamo", role: "avisos", pos: [-0.05, 0.12, 0], color: [220, 235, 255] },
];
window.JARVIS_REGIONS = REGIONS; // brain3d.js (WebGL) lee de aquí la forma y los colores
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
  calendar_add: "agenda · nuevo evento",
  agent_run: "encargar a un agente",
  agent_status: "estado del agente",
  delegate_claude: "encargar a Claude",
  security_audit: "auditoría de seguridad",
  camera_look: "mirar por la cámara",
  home_camera: "cámara de casa",
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
const MOTOR_TOOLS = new Set(["calendar_add", "truenas_app_restart", "wake_on_lan", "home_control", "spotify_play", "spotify_control"]);

function toolRegion(name) {
  if (name.startsWith("reminder_")) return REGION.thalamus;
  if (name === "camera_look" || name === "home_camera") return REGION.visual;
  if (name.startsWith("agent_") || name.startsWith("delegate_") || name === "security_audit") return REGION.prefrontal; // delegar
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

// --- ventana de resultados ------------------------------------------------------------

const CARD_LABEL = { web: "Web", wiki: "Wikipedia", news: "Noticias" };

function hideCards() {
  $("cards").hidden = true;
  $("cards-list").textContent = "";
}

function showCards(cards) {
  const list = $("cards-list");
  for (const c of cards) {
    // Contenido de terceros: siempre como texto, y enlaces solo http(s).
    const safeUrl = /^https?:\/\//.test(c.url || "") ? c.url : "";
    const el = document.createElement(safeUrl ? "a" : "div");
    el.className = `card ${c.kind || ""}`;
    if (safeUrl) {
      el.href = safeUrl;
      el.target = "_blank";
      el.rel = "noopener noreferrer";
    }
    if (c.image) {
      const img = document.createElement("img");
      img.className = "photo";
      img.src = c.image;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.onerror = () => img.remove();
      el.append(img);
    }
    const src = document.createElement("div");
    src.className = "src";
    if (c.icon) {
      const icon = document.createElement("img");
      icon.src = c.icon;
      icon.alt = "";
      icon.referrerPolicy = "no-referrer";
      icon.onerror = () => icon.remove();
      src.append(icon);
    }
    src.append(document.createTextNode(c.source || CARD_LABEL[c.kind] || ""));
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = c.title || "";
    el.append(src, title);
    if (c.text) {
      const d = document.createElement("div");
      d.className = "d";
      d.textContent = c.text;
      el.append(d);
    }
    list.append(el);
  }
  const n = list.children.length;
  $("cards-title").textContent = `Resultados · ${n}`;
  $("cards").hidden = n === 0;
}

$("cards-close").addEventListener("click", hideCards);

// Resultados de un agente (investigador, compras...): una tarjeta por opción, la recomendada primero.
// Vigilancia de alucinaciones: datos que el agente dio pero no aparecen en lo que leyó.
function unverifiedNote(items) {
  const p = document.createElement("p");
  p.className = "unverified";
  p.textContent = `Sin verificar: ${items.join(" · ")}`;
  p.title = "No aparece en ninguna fuente que leyó el agente. Compruébalo antes de usarlo.";
  return p;
}

function showOptions(label, cards, note, rgb) {
  const list = $("cards-list");
  list.textContent = "";
  for (const c of cards) {
    const el = document.createElement("article");
    el.className = `card option${c.best ? " best" : ""}`;
    if (rgb) el.style.setProperty("--c", `rgb(${rgb.join(",")})`);
    const head = document.createElement("div");
    head.className = "opt-head";
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = c.title;
    head.append(title);
    if (c.price) {
      const price = document.createElement("span");
      price.className = "price num";
      price.textContent = c.price;
      head.append(price);
    }
    if (c.best) {
      const badge = document.createElement("span");
      badge.className = "badge-best";
      badge.textContent = "Recomendado";
      el.append(badge);
    }
    el.append(head);
    if (c.unverified?.length) el.append(unverifiedNote(c.unverified));
    if (c.data) {
      const d = document.createElement("div");
      d.className = "d";
      d.textContent = c.data;
      el.append(d);
    }
    for (const [cls, text] of [["pro", c.pros], ["con", c.cons]]) {
      if (!text) continue;
      const line = document.createElement("div");
      line.className = `pc ${cls}`;
      line.textContent = text;
      el.append(line);
    }
    if (/^https?:\/\//.test(c.url || "")) {
      const a = document.createElement("a");
      a.className = "pill-btn";
      a.href = c.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.insertAdjacentHTML("afterbegin", '<svg class="icon"><use href="#i-link"/></svg>');
      const host = document.createElement("span");
      host.textContent = new URL(c.url).hostname.replace(/^www\./, "");
      a.append(host);
      el.append(a);
    }
    list.append(el);
  }
  if (note) {
    const foot = document.createElement("p");
    foot.className = "cards-note";
    foot.textContent = `Informe completo en Obsidian: ${note.split("/").pop().replace(/\.md$/, "")}`;
    list.append(foot);
  }
  $("cards-title").textContent = `${label} · ${cards.length} ${cards.length === 1 ? "opción" : "opciones"}`;
  $("cards").hidden = false;
  selectView("home");
  $("cards").scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
}

function flowReset() {
  hideCards();
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
      showQuestion(ev.text);
      fire(REGION.auditory, ev.text);
      trace("Tú", ev.text, REGIONS[REGION.auditory].color);
      break;
    case "memory": {
      const items = ev.items || [];
      const what = items.length ? `${items.length} recuerdo${items.length > 1 ? "s" : ""} · ${items[0].text}` : "sin recuerdos relevantes";
      fire(REGION.hippocampus, what);
      trace("Memoria", what, REGIONS[REGION.hippocampus].color);
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
      setAnswerTools([...answerTools, ev.name]);
      trace(label.charAt(0).toUpperCase() + label.slice(1), argsSummary(ev.args), REGIONS[idx].color);
      break;
    }
    case "tool_result": {
      const idx = toolRegion(ev.name);
      const label = TOOL_LABEL[ev.name] || ev.name;
      Object.assign(regionState[idx], { pending: false, fail: !ev.ok });
      fire(idx, `${ev.ok ? "hecho" : "falló"} · ${label} · ${ev.ms} ms · ${ev.text}`);
      trace(ev.ok ? `${label} · ${ev.ms} ms` : `${label} · falló`, ev.text, ev.ok ? REGIONS[idx].color : [255, 69, 58]);
      break;
    }
    case "cards":
      routeCards(ev.cards);
      break;
    case "reply":
      showAnswer({ text: ev.text });
      fire(REGION.language, ev.text);
      trace(`JARVIS${ev.provider ? ` · ${providerLabel(ev.provider)}${isBackup(ev.provider) ? " (respaldo)" : ""}` : ""}`, ev.text, REGIONS[REGION.language].color);
      showSource(ev.provider);
      if (ev.provider) $("s-model").textContent = providerLabel(ev.provider);
      setTimeout(loadUsage, 500);
      break;
    case "speaking":
      setState("thinking", "poniendo voz");
      fire(REGION.cerebellum, "sintetizando voz");
      break;
  }
}

// --- agentes: panel, trazabilidad en directo y satélites en el cerebro ------------------------

const AGENT_COLORS = {
  investigador: [76, 201, 240],
  tecnico: [255, 110, 90],
  organizador: [48, 209, 88],
  escritor: [255, 200, 90],
  compras: [190, 140, 255],
  captador: [100, 210, 255],
  claude: [217, 119, 87],
  auditor: [255, 77, 120],
};
const agentRgb = (id) => AGENT_COLORS[id] || [210, 230, 255];
const satellites = new Map(); // clave -> satélite dibujado alrededor del cerebro
const externalJobs = new Map(); // tareas de Claude Code en el PC (no están en /api/agents)
let agentsTimer = null;

function satKey(ev) {
  return ev.job ? `${ev.agent}:${ev.job}` : ev.agent;
}

function satellite(ev) {
  const key = satKey(ev);
  let sat = satellites.get(key);
  if (!sat) {
    const used = new Set([...satellites.values()].map((x) => x.slot));
    let slot = 0;
    while (used.has(slot)) slot++;
    const el = document.createElement("div");
    el.className = "sat";
    const rgb = agentRgb(ev.agent);
    el.style.setProperty("--c", `rgb(${rgb.join(",")})`);
    const b = document.createElement("b");
    const span = document.createElement("span");
    const small = document.createElement("small");
    const text = document.createElement("div");
    text.append(b, span, small);
    el.append(text);
    $("flow").append(el);
    const ang = slot * 1.15 + 0.4;
    sat = {
      key, slot, rgb, el, b, span, small, state: "working", born: performance.now(),
      pos: [Math.cos(ang) * 1.55, 0.5 + 0.22 * Math.sin(ang * 2), Math.sin(ang) * 1.05], proj: [0, 0, 1, 1],
    };
    satellites.set(key, sat);
  }
  return sat;
}

function fmtDay(iso) {
  return iso ? String(iso).slice(0, 10).split("-").reverse().join("/") : "";
}

function retireSatellite(sat, ms) {
  setTimeout(() => sat.el.classList.add("leaving"), ms);
  setTimeout(() => {
    sat.el.remove();
    satellites.delete(sat.key);
  }, ms + 1300);
}

function onActivity(ev) {
  if (ev.type === "insight") return showInsight(ev);
  const rgb = agentRgb(ev.agent);
  const label = ev.label || ev.agent;
  const sat = satellite(ev);
  sat.b.textContent = label;
  switch (ev.type) {
    case "agent_start":
      sat.state = "working";
      sat.span.textContent = ev.task || "trabajando";
      sat.small.textContent = ev.models || ev.model || "";
      pulses.push({ from: REGION.prefrontal, toSat: sat.key, t: 0, dur: reducedMotion ? 0.01 : 0.9, rgb });
      fire(REGION.prefrontal, `encarga ${/^el /i.test(label) ? "al " + label.slice(3) : "a " + label}`.toLowerCase());
      trace(label, `empieza: ${ev.task || ""}`, rgb);
      if (!ev.job) externalJobs.set(ev.agent, { agent: ev.agent, label, task: ev.task, state: "trabajando", steps: 0, model: "Claude", started: ev.ts });
      break;
    case "agent_tool": {
      const idx = toolRegion(ev.tool || "");
      const tl = TOOL_LABEL[ev.tool] || ev.tool;
      sat.span.textContent = tl;
      if (ev.model) sat.small.textContent = providerLabel(ev.model);
      pulses.push({ fromSat: sat.key, to: idx, t: 0, dur: reducedMotion ? 0.01 : 0.7, rgb });
      regionState[idx].act = Math.max(regionState[idx].act, 0.8);
      trace(label, tl, rgb);
      const ext = externalJobs.get(ev.agent);
      if (!ev.job && ext) ext.steps++;
      break;
    }
    case "agent_verify":
      trace(label, ev.unverified?.length
        ? `comprobados ${ev.checked} datos · ${ev.unverified.length} sin verificar: ${ev.unverified.join(", ")}`
        : `comprobados ${ev.checked} datos: todos salen en las fuentes`, ev.unverified?.length ? [255, 214, 10] : [48, 209, 88]);
      break;
    case "agent_done":
      sat.state = "done";
      sat.el.classList.add("done");
      sat.span.textContent = ev.summary || "terminado";
      if (ev.model) sat.small.textContent = providerLabel(ev.model);
      pulses.push({ fromSat: sat.key, to: REGION.thalamus, t: 0, dur: reducedMotion ? 0.01 : 0.9, rgb: [48, 209, 88] });
      trace(label, `terminado${ev.model ? ` con ${providerLabel(ev.model)}` : ""}: ${ev.summary || ""}`, [48, 209, 88]);
      if (!ev.job && externalJobs.has(ev.agent)) Object.assign(externalJobs.get(ev.agent), { state: "terminado", summary: ev.summary, note: ev.note, cards: ev.cards });
      if (ev.cards?.length) showOptions(label, ev.cards, ev.note, rgb);
      retireSatellite(sat, 6000);
      break;
    case "agent_reused":
      sat.state = "done";
      sat.el.classList.add("done");
      sat.span.textContent = "reutiliza un informe";
      sat.small.textContent = `del ${fmtDay(ev.date)} · 0 tokens`;
      pulses.push({ from: REGION.hippocampus, toSat: sat.key, t: 0, dur: reducedMotion ? 0.01 : 0.9, rgb });
      trace(label, `reutiliza el informe del ${fmtDay(ev.date)} (0 tokens): ${ev.summary || ""}`, rgb);
      if (ev.cards?.length) showOptions(label, ev.cards, ev.note, rgb);
      retireSatellite(sat, 5000);
      break;
    case "leads":
      sat.span.textContent = ev.new ? `${ev.new} leads nuevos` : "sin leads nuevos";
      trace(label, ev.new ? `ha encontrado ${ev.new} leads nuevos: ${(ev.names || []).join(", ")}` : "no ha encontrado leads nuevos", rgb);
      pulses.push({ fromSat: sat.key, to: REGION.hippocampus, t: 0, dur: reducedMotion ? 0.01 : 0.9, rgb });
      loadLeads();
      break;
    case "agent_error":
      sat.state = "error";
      sat.el.classList.add("error");
      sat.span.textContent = ev.detail || ev.task || "error";
      trace(label, `no ha podido terminar: ${ev.detail || ev.task || ""}`, [255, 69, 58]);
      if (!ev.job && externalJobs.has(ev.agent)) Object.assign(externalJobs.get(ev.agent), { state: "error", summary: ev.task });
      retireSatellite(sat, 6000);
      break;
  }
  clearTimeout(agentsTimer);
  agentsTimer = setTimeout(loadAgents, 400);
}

async function activityLoop(after = -1) {
  let next = after;
  let delay = 0;
  if (mode === "server" && !getToken()) return setTimeout(() => activityLoop(after), 3000);
  try {
    const resp = await api(`/api/activity?after=${after}&wait=${after < 0 ? 0 : 25}`);
    if (resp.status === 404) return; // servidor antiguo
    if (!resp.ok) throw new Error(resp.status);
    const body = await resp.json();
    if (after >= 0) body.events.forEach(onActivity);
    next = body.last;
  } catch {
    delay = 5000;
  }
  setTimeout(() => activityLoop(next), delay);
}

function elapsed(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
}

// Nombre visible de un modelo: "Claude Sonnet 5" o "Groq · llama-3.3-70b".
function modelName(id) {
  return (claudeModels.find((m) => m.id === id) || {}).label || providerLabel(id);
}

// Modelo de cada agente: el elegido va primero y su cadena queda de respaldo.
function agentModelPicker(a) {
  const label = document.createElement("label");
  label.className = "agent-model";
  const span = document.createElement("span");
  span.textContent = "Modelo";
  const sel = document.createElement("select");
  sel.setAttribute("aria-label", `Modelo de ${a.label}`);
  sel.append(new Option(`Su cadena (${a.models})`, ""));
  const groups = new Map();
  for (const id of a.choices) {
    const prov = id.split(":")[0];
    const name = id.startsWith("claude-") ? "Claude · membresía" : PROVIDER_NAME[prov] || prov;
    if (!groups.has(name)) groups.set(name, document.createElement("optgroup"));
    groups.get(name).label = name;
    groups.get(name).append(new Option(modelName(id), id));
  }
  groups.forEach((g) => sel.append(g));
  sel.value = a.prefer || "";
  sel.addEventListener("change", async () => {
    sel.disabled = true;
    try {
      const resp = await api("/api/agents/model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: a.id, model: sel.value }),
      });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `HTTP ${resp.status}`);
      trace(a.label, sel.value ? `ahora trabaja con ${modelName(sel.value)}` : "vuelve a su cadena de modelos", agentRgb(a.id));
    } catch (err) {
      trace(a.label, `no se pudo cambiar el modelo: ${err.message || err}`, [255, 69, 58]);
    }
    loadAgents();
  });
  label.append(span, sel);
  return label;
}

// Encargo directo: el agente arranca sin gastar cupo del LLM de la conversación.
let agentInfo = [];
function fillAgentForm(agents) {
  agentInfo = agents;
  const form = $("agent-form");
  form.hidden = !agents.length;
  const pick = $("agent-pick");
  const prev = pick.value;
  pick.textContent = "";
  for (const a of agents) {
    const name = a.label.replace(/^(El|La) /, "").replace(/^./, (c) => c.toUpperCase());
    pick.append(new Option(`${name}${a.prefer ? ` · ${modelName(a.prefer)}` : ""}`, a.id));
  }
  if (agents.some((a) => a.id === prev)) pick.value = prev;
  agentHint();
}

function agentHint() {
  const a = agentInfo.find((x) => x.id === $("agent-pick").value);
  $("agent-hint").textContent = !a ? "" : a.profiles?.length
    ? `Nombra el producto en el encargo para usar su perfil: ${a.profiles.join(", ")}.`
    : a.description;
}

$("agent-pick").addEventListener("change", agentHint);
$("agent-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const task = $("agent-task").value.trim();
  if (!task) return;
  const btn = $("agent-go");
  btn.disabled = true;
  $("agent-msg").textContent = "";
  try {
    const resp = await api("/api/agents/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent: $("agent-pick").value, task, refresh: $("agent-refresh").checked }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    $("agent-msg").textContent = body.reused ? "Ya había un informe parecido: te lo muestro (0 tokens)." : "En marcha. Te aviso al terminar.";
    if (!body.reused) $("agent-task").value = "";
    $("agent-refresh").checked = false;
    loadAgents();
  } catch (err) {
    $("agent-msg").textContent = `No se pudo: ${err.message || err}`;
  } finally {
    btn.disabled = false;
  }
});

// Agentes personalizados: formulario de alta (el servidor pone los límites).
function fillCustomForm(agents) {
  $("custom-new").hidden = !agents.length;
  const sel = $("custom-model");
  const prev = sel.value;
  sel.textContent = "";
  sel.append(new Option("Modelo: la cadena de los agentes", ""));
  for (const m of claudeModels) sel.append(new Option(`Modelo: ${m.label}`, m.id));
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

$("custom-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("custom-msg").textContent = "";
  try {
    const resp = await api("/api/agents/custom", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("custom-name").value, description: $("custom-desc").value,
        instructions: $("custom-instr").value, model: $("custom-model").value }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    $("custom-msg").textContent = `Creado «${body.agent.name}». Ya puedes hacerle encargos.`;
    for (const id of ["custom-name", "custom-desc", "custom-instr"]) $(id).value = "";
    loadAgents();
  } catch (err) {
    $("custom-msg").textContent = `No se pudo: ${err.message || err}`;
  }
});

async function loadAgents() {
  if (mode === "server" && !getToken()) return;
  let data = { agents: [], jobs: [] };
  try {
    const resp = await api("/api/agents");
    if (resp.ok) data = await resp.json();
  } catch {
    /* sin conexión: se deja lo que había */
  }
  fillAgentForm(data.agents);
  fillCustomForm(data.agents);
  const agents = $("agents-list");
  agents.textContent = "";
  for (const a of data.agents) {
    const li = document.createElement("li");
    li.style.setProperty("--c", `rgb(${agentRgb(a.id).join(",")})`);
    const dot = document.createElement("span");
    dot.className = "dot";
    const body = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = a.label.replace(/^(El|La) /, "").replace(/^./, (c) => c.toUpperCase());
    const p = document.createElement("p");
    p.textContent = a.description;
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = a.prefer ? `${modelName(a.prefer)} · su cadena de respaldo` : a.models;
    body.append(strong, p, chip);
    if (a.choices?.length) body.append(agentModelPicker(a));
    if (a.custom) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn-quiet";
      del.textContent = "Quitar agente";
      del.addEventListener("click", async () => {
        if (!confirm(`¿Quitar el agente «${a.label}»? Sus informes siguen en Obsidian.`)) return;
        const resp = await api(`/api/agents/custom/${encodeURIComponent(a.id)}`, { method: "DELETE" });
        if (resp.ok) trace(a.label, "agente quitado", agentRgb(a.id));
        loadAgents();
      });
      body.append(del);
    }
    if (a.profiles) {
      // El captador: para qué productos o negocios sabe buscar clientes (LEADS_PROFILE_<NOMBRE>).
      const prof = document.createElement("p");
      prof.className = "profiles";
      prof.textContent = a.profiles.length
        ? `Busca clientes para: ${a.profiles.join(", ")}. Nómbralo en el encargo («busca clientes para ${a.profiles.at(-1)}»).`
        : "Sin perfiles: añade LEADS_PROFILE para que sepa qué ofreces.";
      body.append(prof);
    }
    li.append(dot, body);
    agents.append(li);
  }
  if (claudeModels.length && mode !== "server") { // encargos y auditorias con Claude: solo en el PC
    for (const [id, label, desc] of [["claude", "Claude", "tareas complejas con tu membresía"], ["auditor", "Auditor de seguridad", "revisa el servidor o el código con Claude"]]) {
      const li = document.createElement("li");
      li.style.setProperty("--c", `rgb(${agentRgb(id).join(",")})`);
      li.innerHTML = '<span class="dot"></span><div><strong></strong><p></p><span class="chip">Claude · membresía</span></div>';
      li.querySelector("strong").textContent = label;
      li.querySelector("p").textContent = desc;
      agents.append(li);
    }
  }
  if (!agents.children.length) agents.innerHTML = '<p class="empty">Sin agentes disponibles en este servidor.</p>';

  const jobs = [...data.jobs.map((j) => ({ ...j, label: (data.agents.find((a) => a.id === j.agent) || {}).label || j.agent })),
    ...externalJobs.values()].sort((x, y) => (y.started || "").localeCompare(x.started || ""));
  const list = $("jobs-list");
  list.textContent = "";
  for (const j of jobs.slice(0, 8)) {
    const li = document.createElement("li");
    li.className = j.state === "trabajando" ? "working" : j.state === "terminado" ? "done" : "error";
    li.style.setProperty("--c", `rgb(${agentRgb(j.agent).join(",")})`);
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = `${j.label} · ${j.state}`;
    const task = document.createElement("p");
    task.className = "task";
    task.textContent = j.task;
    const meta = document.createElement("div");
    meta.className = "meta";
    for (const part of [providerLabel(j.model), j.steps ? `${j.steps} pasos` : "", j.started ? `hace ${elapsed(j.started)}` : ""].filter(Boolean)) {
      const m = document.createElement("span");
      m.textContent = part;
      meta.append(m);
    }
    li.append(who, task, meta);
    if (j.summary && j.state !== "trabajando") {
      const sum = document.createElement("p");
      sum.className = "summary";
      sum.textContent = j.summary;
      li.append(sum);
    }
    if (j.cards?.length && j.state === "terminado") {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "pill-btn";
      open.textContent = `Ver resultados · ${j.cards.length}`;
      open.addEventListener("click", () => showOptions(j.label, j.cards, j.note, agentRgb(j.agent)));
      li.append(open);
    }
    list.append(li);
  }
  if (!jobs.length) list.innerHTML = '<p class="empty">Nadie trabajando ahora. Pídele algo largo a un agente: «investiga…», «compárame…», «audita…».</p>';
  setBadge("agents", jobs.filter((j) => j.state === "trabajando").length);
}
setInterval(loadAgents, 30000);

// --- leads: posibles clientes que encuentra el captador ------------------------------------

const LEAD_STATUS = {
  nuevo: { label: "Nuevo", plural: "Nuevos", rgb: [41, 151, 255] },
  contactado: { label: "Contactado", plural: "Contactados", rgb: [255, 159, 10] },
  interesado: { label: "Interesado", plural: "Interesados", rgb: [48, 209, 88] },
  descartado: { label: "Descartado", plural: "Descartados", rgb: [142, 142, 147] },
};
let leads = [];
let leadFilter = "";

function safeUrl(url) {
  return /^https?:\/\/[^\s<>"']+$/i.test(url || "") ? url : "";
}

function copyText(text, button) {
  const done = () => {
    const old = button.lastChild.textContent;
    button.lastChild.textContent = "Copiado";
    setTimeout(() => (button.lastChild.textContent = old), 1400);
  };
  navigator.clipboard?.writeText(text).then(done, () => {});
}

function iconButton(icon, text, onClick, cls = "pill-btn") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.insertAdjacentHTML("afterbegin", `<svg class="icon"><use href="#i-${icon}"/></svg>`);
  const span = document.createElement("span");
  span.textContent = text;
  b.append(span);
  b.addEventListener("click", onClick);
  return b;
}

// Perfiles del captador: lo que ofreces a cada tipo de cliente. Se guardan en el NAS.
async function saveProfile(name, text) {
  const resp = await api("/api/leads/profiles", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, text }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  renderProfiles(body.profiles || []);
  loadAgents(); // el encargo directo muestra los perfiles del captador
  return body.name;
}

function renderProfiles(profiles) {
  const list = $("profiles-list");
  list.textContent = "";
  for (const p of profiles) {
    const box = document.createElement("div");
    box.className = "profile";
    const head = document.createElement("header");
    const name = document.createElement("strong");
    name.textContent = p.name;
    const src = document.createElement("small");
    src.textContent = p.source === "nas" ? "guardado desde el HUD" : "de la configuración de TrueNAS";
    head.append(name, src);
    const text = document.createElement("textarea");
    text.rows = 4;
    text.maxLength = 2000;
    text.value = p.text;
    text.setAttribute("aria-label", `Perfil ${p.name}`);
    const row = document.createElement("div");
    row.className = "row";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn-primary";
    save.textContent = "Guardar";
    const msg = document.createElement("span");
    msg.className = "agent-hint";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await saveProfile(p.name, text.value);
      } catch (err) {
        msg.textContent = `No se pudo: ${err.message || err}`;
        save.disabled = false;
      }
    });
    row.append(save);
    if (p.source === "nas") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn-quiet";
      del.textContent = "Quitar";
      del.title = "Borra lo guardado desde el HUD; si existe en TrueNAS, vuelve a ese";
      del.addEventListener("click", () => saveProfile(p.name, "").catch((err) => (msg.textContent = `No se pudo: ${err.message || err}`)));
      row.append(del);
    }
    row.append(msg);
    box.append(head, text, row);
    list.append(box);
  }
}

async function loadProfiles() {
  if (mode === "server" && !getToken()) return;
  try {
    const resp = await api("/api/leads/profiles");
    $("profiles").hidden = !resp.ok;
    if (resp.ok) renderProfiles((await resp.json()).profiles || []);
  } catch {
    /* servidor antiguo */
  }
}

$("profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("profile-msg").textContent = "";
  try {
    const name = await saveProfile($("profile-name").value, $("profile-text").value);
    $("profile-msg").textContent = `Guardado como «${name}».`;
    $("profile-name").value = "";
    $("profile-text").value = "";
  } catch (err) {
    $("profile-msg").textContent = `No se pudo: ${err.message || err}`;
  }
});

// Calendario: dónde crea eventos JARVIS (Ajustes → Calendario).
async function loadCalendar() {
  if (mode === "server" && !getToken()) return;
  let data;
  try {
    const resp = await api("/api/calendar/status");
    if (!resp.ok) return;
    data = await resp.json();
  } catch {
    return;
  }
  $("calendar-box").hidden = false;
  $("calendar-state").textContent = data.google
    ? "Google Calendar: conectado. Lee tu agenda al momento y crea eventos (siempre te pide confirmación antes)."
    : "Sin calendario conectado. Para Google Calendar, sigue «Crear eventos» en el README.";
}

async function loadLeads() {
  if (mode === "server" && !getToken()) return;
  try {
    const resp = await api("/api/leads");
    if (resp.status === 404) {
      leads = null;
    } else if (resp.ok) {
      leads = (await resp.json()).leads;
    }
  } catch {
    /* sin conexión: se deja lo que había */
  }
  renderLeads();
}

async function setLeadStatus(lead, status) {
  try {
    const resp = await api("/api/leads/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: lead.id, status }),
    });
    if (!resp.ok) throw new Error(resp.status);
    lead.status = status;
    trace("Leads", `${lead.name}: ${LEAD_STATUS[status].label.toLowerCase()}`, LEAD_STATUS[status].rgb);
  } catch {
    trace("Leads", `no he podido cambiar ${lead.name}`, [255, 69, 58]);
  }
  renderLeads();
}

function renderLeads() {
  const list = $("leads-list");
  const filter = $("lead-filter");
  list.textContent = "";
  filter.textContent = "";
  if (leads === null) {
    $("lead-stats").textContent = "";
    list.innerHTML = '<p class="empty">Los leads necesitan los agentes activados en el servidor (AGENTS_ENABLED).</p>';
    setBadge("leads", 0);
    return;
  }
  const counts = Object.fromEntries(Object.keys(LEAD_STATUS).map((k) => [k, leads.filter((l) => l.status === k).length]));
  $("lead-stats").textContent = leads.length
    ? `${leads.length} en total · ${counts.nuevo} por contactar · ${counts.interesado} interesados`
    : "";
  for (const [key, text] of [["", "Todos"], ...Object.entries(LEAD_STATUS).map(([k, v]) => [k, v.plural])]) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip-btn";
    chip.setAttribute("aria-pressed", String(leadFilter === key));
    chip.textContent = key ? `${text} ${counts[key]}` : text;
    chip.addEventListener("click", () => {
      leadFilter = key;
      renderLeads();
    });
    filter.append(chip);
  }
  const shown = leads.filter((l) => !leadFilter || l.status === leadFilter);
  for (const lead of shown) {
    const st = LEAD_STATUS[lead.status] || LEAD_STATUS.nuevo;
    const li = document.createElement("li");
    li.className = `lead ${lead.status}`;
    li.style.setProperty("--c", `rgb(${st.rgb.join(",")})`);
    const head = document.createElement("div");
    head.className = "lead-head";
    const name = document.createElement("strong");
    name.textContent = lead.name;
    const pill = document.createElement("span");
    pill.className = "status";
    pill.textContent = st.label;
    head.append(name, pill);
    const kind = document.createElement("p");
    kind.className = "lead-kind";
    kind.textContent = [lead.kind, lead.area].filter(Boolean).join(" · ");
    li.append(head, kind);
    if (lead.fit) {
      const fit = document.createElement("p");
      fit.className = "lead-fit";
      fit.textContent = lead.fit;
      li.append(fit);
    }
    if (lead.unverified?.length) li.append(unverifiedNote(lead.unverified));
    const contact = document.createElement("div");
    contact.className = "lead-contact";
    const url = safeUrl(lead.web);
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.className = "pill-btn";
      a.insertAdjacentHTML("afterbegin", '<svg class="icon"><use href="#i-link"/></svg>');
      const host = document.createElement("span");
      host.textContent = new URL(url).hostname.replace(/^www\./, "");
      a.append(host);
      contact.append(a);
    }
    if (lead.contact) contact.append(iconButton("copy", lead.contact, (e) => copyText(lead.contact, e.currentTarget)));
    if (contact.children.length) li.append(contact);
    if (lead.message) {
      const msg = document.createElement("blockquote");
      msg.textContent = lead.message;
      li.append(msg, iconButton("copy", "Copiar mensaje", (e) => copyText(lead.message, e.currentTarget), "pill-btn quiet"));
    }
    const actions = document.createElement("div");
    actions.className = "lead-actions";
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", `Estado de ${lead.name}`);
    for (const [key, v] of Object.entries(LEAD_STATUS)) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = v.label;
      b.setAttribute("aria-pressed", String(lead.status === key));
      b.style.setProperty("--c", `rgb(${v.rgb.join(",")})`);
      b.addEventListener("click", () => lead.status !== key && setLeadStatus(lead, key));
      actions.append(b);
    }
    const meta = document.createElement("p");
    meta.className = "lead-meta";
    meta.textContent = [lead.found ? `Encontrado ${lead.found.slice(0, 10).split("-").reverse().join("/")}` : "", lead.note].filter(Boolean).join(" · ");
    li.append(actions, meta);
    list.append(li);
  }
  if (!shown.length) {
    list.innerHTML = leads.length
      ? '<p class="empty">Ninguno con este estado.</p>'
      : '<p class="empty">Aún no hay leads. Pídeselo al captador: «busca clientes para mi taller de impresión 3D en Sabadell».</p>';
  }
  setBadge("leads", counts.nuevo);
}

// --- fichas: los datos clave de cada respuesta, flotando junto al cerebro -----------------------

// La ficha llega unos segundos después de la respuesta: si es de la respuesta que se ve, sustituye a los
// datos sacados a mano (el modelo pone mejores etiquetas). Si no hay respuesta en pantalla, se enseña sola.
function showInsight(card, quiet = false) {
  const idx = card.tools?.length ? toolRegion(card.tools[0]) : REGION.language;
  const rgb = REGIONS[idx].color;
  const items = (card.items || []).map((i) => ({ k: i.k, v: i.v }));
  const current = !$("answer-card").hidden && Date.now() - answerAt < 90000;
  if ((current && !answerVisual) || $("answer-card").hidden) {
    if (!current) {
      $("answer-empty").hidden = true;
      $("answer-card").hidden = false;
      $("answer-q").textContent = `Última ficha · ${(card.ts || "").slice(11, 16)}`;
      $("answer-text").textContent = "";
      $("answer-text").dataset.raw = "";
    }
    fillFacts(items, card.title);
  }
  if (quiet) return;
  regionState[idx].act = Math.max(regionState[idx].act, 0.9);
  pulses.push({ from: REGION.language, to: idx, t: 0, dur: reducedMotion ? 0.01 : 0.6, rgb });
  trace("Ficha", `${card.title}: ${items.map((i) => `${i.k} ${i.v}`).join(" · ")}`, rgb);
}

async function loadInsights() {
  if (mode === "server" && !getToken()) return;
  try {
    const resp = await api("/api/insights");
    if (!resp.ok) return;
    const { insights } = await resp.json();
    insights.slice(-1).forEach((c) => showInsight(c, true));
  } catch {
    /* servidor antiguo */
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
    const text = REGIONS[i].planned ? "Planificado" : r.pending ? "Ejecutando" : r.act > 0.35 ? "Activo" : "En reposo";
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
new ResizeObserver(resize).observe(canvas);

function rgba([r, gr, b], alpha) {
  return `rgba(${r | 0},${gr | 0},${b | 0},${Math.max(0, Math.min(1, alpha))})`;
}

// Respaldo sin WebGL: el cerebro de partículas en 2D.
function draw2d(now, dt, W, H) {
  let centers;
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

  // agentes: satélites en órbita, unidos al prefrontal por un haz
  centers = REGIONS.map((r) => project(r.pos, [0, 0, 0, 0]));
  const pf = centers[REGION.prefrontal];
  g.lineCap = "round";
  for (const sat of satellites.values()) {
    const bob = Math.sin(now / 900 + sat.slot) * 0.04;
    project([sat.pos[0], sat.pos[1] + bob, sat.pos[2]], sat.proj);
    const [x, y, f, depth] = sat.proj;
    const rgb = sat.state === "done" ? [48, 209, 88] : sat.state === "error" ? [255, 69, 58] : sat.rgb;
    const alpha = 0.35 + 0.65 * depth;
    const mx = (pf[0] + x) / 2 + ((pf[0] + x) / 2 - cx) * 0.35;
    const my = (pf[1] + y) / 2 + ((pf[1] + y) / 2 - cy) * 0.35 - S * 0.1;
    g.setLineDash([px * 5, px * 7]);
    g.lineDashOffset = -now / 25;
    g.strokeStyle = rgba(rgb, 0.45 * alpha);
    g.lineWidth = Math.max(1, px * 1.3);
    g.beginPath();
    g.moveTo(pf[0], pf[1]);
    g.quadraticCurveTo(mx, my, x, y);
    g.stroke();
    g.setLineDash([]);
    const pulse = sat.state === "working" ? 0.5 + 0.5 * Math.sin(now / 260) : 0.6;
    const r = px * (7 + pulse * 3) * f;
    const glow = g.createRadialGradient(x, y, 0, x, y, r * 3);
    glow.addColorStop(0, rgba(rgb, 0.55 * alpha));
    glow.addColorStop(1, rgba(rgb, 0));
    g.fillStyle = glow;
    g.beginPath();
    g.arc(x, y, r * 3, 0, Math.PI * 2);
    g.fill();
    g.fillStyle = rgba([255, 255, 255], 0.9 * alpha);
    g.beginPath();
    g.arc(x, y, r * 0.45, 0, Math.PI * 2);
    g.fill();
    g.strokeStyle = rgba(rgb, 0.9 * alpha);
    g.lineWidth = Math.max(1, px * 1.5);
    g.beginPath();
    g.arc(x, y, r, 0, Math.PI * 2);
    g.stroke();
  }

  // impulsos entre regiones y agentes
  for (let k = pulses.length - 1; k >= 0; k--) {
    const p = pulses[k];
    p.t += dt / p.dur;
    const fromSat = p.fromSat && satellites.get(p.fromSat);
    const toSat = p.toSat && satellites.get(p.toSat);
    if ((p.fromSat && !fromSat) || (p.toSat && !toSat)) {
      pulses.splice(k, 1);
      continue;
    }
    const a = fromSat ? fromSat.proj : centers[p.from];
    const b = toSat ? toSat.proj : centers[p.to];
    const mx = (a[0] + b[0]) / 2 + (((a[0] + b[0]) / 2 - cx) * 0.4);
    const my = (a[1] + b[1]) / 2 + (((a[1] + b[1]) / 2 - cy) * 0.4) - S * 0.12;
    const at = (t) => {
      const u = 1 - t;
      return [u * u * a[0] + 2 * u * t * mx + t * t * b[0], u * u * a[1] + 2 * u * t * my + t * t * b[1]];
    };
    const col = p.rgb || REGIONS[p.to].color;
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

  return centers;
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
  let centers;
  if (window.brain3d) {
    // WebGL: el cerebro de verdad en 3D lo pinta brain3d.js en #gl; este lienzo solo recibe el dedo.
    if (!canvas.dataset.clear) {
      g.clearRect(0, 0, W, H);
      canvas.dataset.clear = "1";
      document.body.classList.add("gl");
    }
    centers = window.brain3d.render({ now, dt, color, level, burst, regionState, pulses, satellites, W, H });
  } else {
    centers = draw2d(now, dt, W, H);
  }

  // etiquetas de región
  const dpr = W / canvas.getBoundingClientRect().width || 1;
  // Etiquetas siempre dentro del escenario (en el móvil las de los bordes se salían).
  const maxX = canvas.offsetLeft + canvas.clientWidth - 8;
  const place = (el, x, y) => {
    const left = Math.min(canvas.offsetLeft + x / dpr + 14, maxX - el.offsetWidth);
    el.style.transform = `translate(${Math.max(canvas.offsetLeft + 4, left)}px, ${canvas.offsetTop + y / dpr - 12}px)`;
  };
  for (const sat of satellites.values()) place(sat.el, sat.proj[0], sat.proj[1]);
  labels.forEach(({ el, span, i }) => {
    const r = regionState[i];
    const [x, y] = centers[i];
    place(el, x, y);
    el.classList.toggle("on", r.act > 0.35 || r.pending);
    el.classList.toggle("fail", r.fail);
    el.classList.toggle("back", centers[i][3] < 0.4); // región en la cara oculta del modelo
    if (span.textContent !== (r.detail || REGIONS[i].role)) span.textContent = r.detail || REGIONS[i].role;
  });

  requestAnimationFrame(frame);
}

// --- entradas ----------------------------------------------------------------

// La barra espaciadora habla solo si no estás escribiendo ni sobre un control (en ellos, el espacio es suyo).
function spaceIsForTyping(e) {
  const el = e.target instanceof Element ? e.target : document.activeElement;
  return Boolean(el?.closest?.("input, textarea, select, button, summary, [contenteditable], [role='menuitemradio'], [role='tab']"));
}

addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || spaceIsForTyping(e)) return;
  e.preventDefault();
  startRecording();
});
addEventListener("keyup", (e) => {
  if (e.code !== "Space" || spaceIsForTyping(e)) return;
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
    ({ mode, pc_apps: pcApps, wake: wakeEnabled = false, claude_models: claudeModels = [], only_claude: onlyClaude = false, default_model: defaultModel = "" } = await (
      await fetch("/hud/config")
    ).json());
  } catch {
    pcApps = null;
  }
  noticesLoop();
  activityLoop();
  loadModels();
  loadAgents();
  loadLeads();
  loadProfiles();
  loadCalendar();
  loadInsights();
  loadUsage();
  selectView(location.hash.slice(1) || "home");
  if (mode === "server") {
    $("hint").textContent = matchMedia("(pointer: coarse)").matches
      ? "Mantén pulsado el cerebro para hablar"
      : "Mantén pulsado el cerebro o la barra espaciadora";
    if (!getToken()) askToken();
  } else {
    pollEvents(); // avisos del PC: temporizadores y "Hey Jarvis" (solo en modo local)
    if (wakeEnabled) {
      const hint = "Di «Hey Jarvis», o mantén pulsado el cerebro o la barra espaciadora";
      // El navegador no deja sonar nada hasta el primer clic o tecla en la página.
      if (ensureAudio().state === "suspended") {
        $("hint").textContent = "Haz clic en la página una vez para activar el sonido";
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
