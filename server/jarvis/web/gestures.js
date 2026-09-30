// Gestos de mano (Nivel 4, sentidos): el reconocimiento corre en el navegador con MediaPipe (WebAssembly).
// El vídeo nunca sale del dispositivo ni llega al servidor: solo se ejecutan las acciones del HUD.
"use strict";

// Cuánto hay que mantener cada gesto para que cuente (ms) y, si se repite mientras se mantiene, cada cuánto.
const GESTURE_RULES = {
  Open_Palm: { hold: 800 },
  Closed_Fist: { hold: 450 },
  Thumb_Up: { hold: 700 },
  Thumb_Down: { hold: 700 },
  Pointing_Up: { hold: 500, repeat: 600 },
  Victory: { hold: 500, repeat: 600 },
};
const GESTURE_MIN_SCORE = 0.6;
const GESTURE_GRACE_MS = 300; // parpadeos del detector que no deben reiniciar la cuenta

// Convierte lo que detecta cada fotograma en "gesto mantenido" y "disparo". Sin DOM: se puede probar en Node.
class GestureTracker {
  constructor(rules = GESTURE_RULES) {
    this.rules = rules;
    this.reset();
  }

  reset() {
    this.current = null; // gesto que se está manteniendo
    this.since = 0;
    this.lostAt = null;
    this.fired = false;
    this.lastFire = 0;
  }

  // name: gesto visto en este fotograma (o null). Devuelve {gesture, progress 0..1, fire}.
  update(name, t) {
    if (name && !this.rules[name]) name = null;
    if (name !== this.current) {
      this.lostAt ??= t;
      if (t - this.lostAt > GESTURE_GRACE_MS || this.current === null) {
        this.current = name;
        this.since = t;
        this.lostAt = null;
        this.fired = false;
      }
    } else {
      this.lostAt = null;
    }
    const rule = this.current && this.rules[this.current];
    if (!rule) return { gesture: null, progress: 0, fire: false };
    const progress = Math.min(1, (t - this.since) / rule.hold);
    let fire = false;
    if (progress >= 1) {
      if (!this.fired) fire = true;
      else if (rule.repeat && t - this.lastFire >= rule.repeat) fire = true;
    }
    if (fire) {
      this.fired = true;
      this.lastFire = t;
    }
    return { gesture: this.current, progress: this.fired && !rule.repeat ? 1 : progress, fire };
  }
}

// --- reconocimiento (MediaPipe) ----------------------------------------------------------------

const GESTURE_MODEL = "vendor/mediapipe/gesture_recognizer.task";
const GESTURE_WASM = "vendor/mediapipe/wasm/vision_wasm_internal";
const GESTURE_FPS = 15;

class GestureEngine {
  // onFrame({gesture, progress, fire, seen}) se llama en cada análisis; onError(mensaje) si algo falla.
  constructor(video, onFrame, onError) {
    this.video = video;
    this.onFrame = onFrame;
    this.onError = onError;
    this.tracker = new GestureTracker();
    this.recognizer = null;
    this.stream = null;
    this.timer = null;
    this.lastVideoTime = -1;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
    });
    this.video.srcObject = this.stream;
    await this.video.play().catch(() => {});
    try {
      this.recognizer ??= await this.load();
    } catch (err) {
      this.stop();
      throw err;
    }
    this.tracker.reset();
    this.timer = setInterval(() => this.tick(), 1000 / GESTURE_FPS);
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  async load() {
    const { FilesetResolver, GestureRecognizer } = await import("./vendor/mediapipe/vision_bundle.mjs");
    const fileset = await FilesetResolver.forVisionTasks(GESTURE_WASM.replace(/\/[^/]+$/, ""));
    // Se fija el wasm con SIMD, que es el único que se incluye.
    fileset.wasmLoaderPath = `${GESTURE_WASM}.js`;
    fileset.wasmBinaryPath = `${GESTURE_WASM}.wasm`;
    const make = (delegate) =>
      GestureRecognizer.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: GESTURE_MODEL, delegate },
        runningMode: "VIDEO",
        numHands: 1,
      });
    try {
      return await make("GPU");
    } catch {
      return make("CPU");
    }
  }

  onVisibility = () => {
    if (document.hidden) this.tracker.reset();
  };

  tick() {
    const v = this.video;
    if (document.hidden || !this.recognizer || v.readyState < 2 || v.currentTime === this.lastVideoTime) return;
    this.lastVideoTime = v.currentTime;
    let name = null;
    let seen = false;
    try {
      const res = this.recognizer.recognizeForVideo(v, performance.now());
      seen = res.landmarks.length > 0;
      const top = res.gestures[0]?.[0];
      if (top && top.score >= GESTURE_MIN_SCORE) name = top.categoryName;
    } catch (err) {
      this.onError?.("el reconocedor de gestos se ha parado");
      this.stop();
      return;
    }
    this.onFrame({ ...this.tracker.update(name, performance.now()), seen });
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.video.srcObject = null;
  }
}

if (typeof module !== "undefined") module.exports = { GestureTracker, GESTURE_RULES };
