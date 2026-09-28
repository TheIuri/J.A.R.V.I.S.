// Cerebro 3D de JARVIS (WebGL con Three.js). Un núcleo de corteza que brilla en blanco, racimos
// de neuronas de colores por región y dendritas que salen hacia fuera; por ellas viajan señales.
// hud.js lleva el estado (regiones, impulsos, agentes) y aquí solo se dibuja. Si WebGL no está,
// este módulo no se registra y hud.js pinta el cerebro en 2D como antes.

import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const lowPower = matchMedia("(pointer: coarse)").matches;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const MAX_REGIONS = 12;
const TRAIL = 10;
const MAX_PULSES = 48;
const MAX_SATS = 12;
const BEAM_SEGS = 36;

// Números pseudoaleatorios con semilla: el cerebro sale igual en cada visita.
let seed = 20260928;
const rand = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const jitter = (s) => (rand() - 0.5) * 2 * s;

// --- forma ------------------------------------------------------------------------------

function inside(x, y, z) {
  const q = x * x + ((y - 0.05) / 0.7) ** 2 + (z / 0.62) ** 2;
  if (q <= 1 && y > -0.48 && !(x < -0.35 && y < -0.3)) return Math.abs(z) < 0.04 && y > -0.2 ? 0 : Math.sqrt(q);
  if (((x + 0.58) / 0.34) ** 2 + ((y + 0.52) / 0.2) ** 2 + (z / 0.42) ** 2 <= 1) return 0.9;
  return 0;
}

// Surcos: la densidad de la corteza baja en unas líneas curvas, como las circunvoluciones.
function gyri(x, y, z) {
  const a = Math.sin(x * 9.1 + Math.sin(y * 5.3) * 1.6) + Math.sin(y * 10.3 + Math.sin(z * 6.1) * 1.4) + Math.sin(z * 8.7 + x * 3.1);
  return Math.abs(a) > 0.35;
}

// --- sombreadores ----------------------------------------------------------------------------

const common = /* glsl */ `
  uniform float uAct[${MAX_REGIONS}];
  uniform float uTime;
  uniform float uEnergy;
  uniform float uDist;
  float depthFade(vec4 mv) { return clamp(1.0 - (-mv.z - (uDist - 1.2)) / 2.8, 0.25, 1.0); }
`;

const pointsVertex = /* glsl */ `
  ${common}
  uniform float uPixel;
  attribute vec3 color;
  attribute float size;
  attribute float aRegion;
  attribute float aPhase;
  attribute float aBase;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float act = uAct[int(aRegion)];
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float tw = 0.55 + 0.45 * sin(uTime * 1.4 + aPhase);
    gl_PointSize = size * uPixel * (1.0 + act * 0.9 + uEnergy * 0.3) * (4.2 / -mv.z);
    gl_Position = projectionMatrix * mv;
    vColor = color;
    vAlpha = aBase * (0.45 + 0.55 * tw) * (0.55 + act * 1.5 + uEnergy * 0.4) * depthFade(mv);
  }
`;

const pointsFragment = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d);
    float core = smoothstep(0.16, 0.0, d);
    gl_FragColor = vec4(mix(vColor, vec3(1.0), core * 0.6), (a * a + core) * vAlpha);
  }
`;

// Dendritas y sinapsis: una señal recorre cada fibra desde el cuerpo hacia la punta.
const linesVertex = /* glsl */ `
  ${common}
  attribute vec3 color;
  attribute float aRegion;
  attribute float aT;
  attribute float aPhase;
  attribute float aBase;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float act = uAct[int(aRegion)];
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    float wave = 0.0;
    if (aT >= 0.0) {
      float s = fract(uTime * (0.16 + act * 0.5) + aPhase);
      wave = exp(-pow((aT - s * 1.3 + 0.15) * 9.0, 2.0)) * (0.35 + act * 1.6);
    }
    float body = aT >= 0.0 ? (1.0 - aT * 0.85) : 1.0;
    vColor = mix(color, vec3(1.0), wave * 0.5);
    vAlpha = (aBase * body * (0.5 + act * 1.4 + uEnergy * 0.35) + wave * 0.5) * depthFade(mv);
  }
`;

const linesFragment = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() { gl_FragColor = vec4(vColor, vAlpha); }
`;

// Sprites dinámicos (impulsos, agentes, brillo de región): sin actividad por región.
const spriteVertex = /* glsl */ `
  uniform float uPixel;
  uniform float uDist;
  attribute vec3 color;
  attribute float size;
  attribute float alpha;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = size * uPixel * (4.2 / -mv.z);
    gl_Position = projectionMatrix * mv;
    vColor = color;
    vAlpha = alpha;
  }
`;

const beamVertex = /* glsl */ `
  uniform float uTime;
  attribute vec3 color;
  attribute float alpha;
  attribute float aT;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    float dash = smoothstep(0.35, 0.5, fract(aT * 10.0 - uTime * 1.2));
    vColor = color;
    vAlpha = alpha * (0.3 + 0.7 * dash);
  }
`;

const glowVertex = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`;
const glowFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform float uStrength;
  varying vec2 vUv;
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    float a = pow(clamp(1.0 - d, 0.0, 1.0), 2.4) * uStrength;
    gl_FragColor = vec4(uColor, a);
  }
`;

function material(vertexShader, fragmentShader, uniforms) {
  return new THREE.ShaderMaterial({
    uniforms, vertexShader, fragmentShader,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  });
}

function geometry(attrs) {
  const geo = new THREE.BufferGeometry();
  for (const [name, [data, size]] of Object.entries(attrs)) {
    geo.setAttribute(name, new THREE.BufferAttribute(data instanceof Float32Array ? data : new Float32Array(data), size));
  }
  return geo;
}

// --- construcción -----------------------------------------------------------------------------

export class Brain3D {
  constructor(canvas, regions) {
    this.canvas = canvas;
    this.regions = regions;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: !lowPower, alpha: false, powerPreference: "high-performance" });
    this.renderer.setClearColor(0x000000, 1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(34, 1, 0.1, 50);
    this.group = new THREE.Group();
    this.group.rotation.x = 0.16;
    this.scene.add(this.group);
    this.spin = -0.6;
    this.pointer = [0, 0];
    this.tmp = new THREE.Vector3();

    const act = new Array(MAX_REGIONS).fill(0);
    this.uniforms = {
      uAct: { value: act }, uTime: { value: 0 }, uEnergy: { value: 0 }, uDist: { value: 5 },
      uPixel: { value: 1 },
    };
    this.regionColor = regions.map((r) => new THREE.Color(`rgb(${r.color.join(",")})`));
    this.white = new THREE.Color(0xdff4ff);

    this.buildCortex();
    this.buildNetwork();
    this.buildDynamic();
    this.buildGlow();

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), lowPower ? 0.7 : 0.85, 0.55, 0.05);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    addEventListener("pointermove", (e) => {
      this.pointer = [(e.clientX / innerWidth) * 2 - 1, (e.clientY / innerHeight) * 2 - 1];
    });
    this.resize();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  nearestRegion(p) {
    let best = 0;
    let bestD = Infinity;
    this.regions.forEach((r, i) => {
      if (r.id === "thalamus") return;
      const d = (p[0] - r.pos[0]) ** 2 + (p[1] - r.pos[1]) ** 2 * 1.2 + (p[2] - r.pos[2]) ** 2 * 0.5;
      if (d < bestD) [best, bestD] = [i, d];
    });
    return best;
  }

  // Corteza: miles de puntos en la superficie, casi blancos. Con el bloom forman el núcleo luminoso.
  buildCortex() {
    const target = lowPower ? 3600 : 7500;
    const pos = [], col = [], size = [], reg = [], phase = [], base = [];
    this.shell = [];
    const c = new THREE.Color();
    while (pos.length / 3 < target) {
      const x = jitter(1), y = rand() * 1.5 - 0.8, z = jitter(0.65);
      const q = inside(x, y, z);
      if (!q || q < 0.8 || !gyri(x, y, z)) continue;
      const p = [x, y, z];
      const r = this.nearestRegion(p);
      c.copy(this.white).lerp(this.regionColor[r], 0.3 + rand() * 0.3);
      pos.push(x, y, z);
      col.push(c.r, c.g, c.b);
      size.push(0.9 + rand() * 1.4);
      reg.push(r);
      phase.push(rand() * 6.28);
      base.push(0.2 + rand() * 0.18);
      if (q > 0.9) this.shell.push({ p, r });
    }
    const pts = new THREE.Points(
      geometry({ position: [pos, 3], color: [col, 3], size: [size, 1], aRegion: [reg, 1], aPhase: [phase, 1], aBase: [base, 1] }),
      material(pointsVertex, pointsFragment, this.uniforms),
    );
    this.group.add(pts);
  }

  // Racimos de neuronas por región, con sus sinapsis, y dendritas que salen hacia fuera.
  buildNetwork() {
    const nodes = [];
    const perRegion = lowPower ? 55 : 110;
    this.regions.forEach((r, ri) => {
      if (r.id === "thalamus") return;
      const near = this.shell.filter((s) => s.r === ri);
      for (let k = 0; k < perRegion && near.length; k++) {
        const s = near[(rand() * near.length) | 0].p;
        const out = 1 + rand() * 0.28;
        nodes.push({ p: [s[0] * out + jitter(0.03), s[1] * out + jitter(0.03), s[2] * out + jitter(0.03)], r: ri });
      }
    });

    const pPos = [], pCol = [], pSize = [], pReg = [], pPhase = [], pBase = [];
    const lPos = [], lCol = [], lReg = [], lT = [], lPhase = [], lBase = [];
    const addPoint = (p, color, s, r, b) => {
      pPos.push(...p); pCol.push(color.r, color.g, color.b); pSize.push(s); pReg.push(r); pPhase.push(rand() * 6.28); pBase.push(b);
    };
    const addSeg = (a, b, ca, cb, r, ta, tb, ph, base) => {
      lPos.push(...a, ...b); lCol.push(ca.r, ca.g, ca.b, cb.r, cb.g, cb.b); lReg.push(r, r); lT.push(ta, tb); lPhase.push(ph, ph); lBase.push(base, base);
    };

    // Sinapsis: cada neurona con sus dos o tres vecinas más cercanas de la misma región.
    nodes.forEach((n, i) => {
      const col = this.regionColor[n.r];
      addPoint(n.p, col, 2.2 + rand() * 2.6, n.r, 0.7);
      const near = [];
      for (let j = i + 1; j < nodes.length; j++) {
        if (nodes[j].r !== n.r) continue;
        const d = (n.p[0] - nodes[j].p[0]) ** 2 + (n.p[1] - nodes[j].p[1]) ** 2 + (n.p[2] - nodes[j].p[2]) ** 2;
        if (d < 0.06) near.push([d, j]);
      }
      near.sort((a, b) => a[0] - b[0]);
      for (const [, j] of near.slice(0, 2 + (rand() < 0.4))) addSeg(n.p, nodes[j].p, col, col, n.r, -1, -1, rand(), 0.28);
    });

    // Dendritas: ramas curvas que nacen en la corteza y se abren hacia fuera, cada vez más finas.
    const trees = lowPower ? 80 : 150;
    const grow = (start, dir, steps, r, depth, t0, ph, whiteness) => {
      let p = start.slice();
      let d = dir.slice();
      const col0 = this.regionColor[r].clone().lerp(this.white, whiteness);
      const col1 = this.regionColor[r].clone().lerp(this.white, Math.min(1, whiteness + 0.2));
      for (let s = 0; s < steps; s++) {
        d = [d[0] + jitter(0.35), d[1] + jitter(0.35), d[2] + jitter(0.35)];
        const len = Math.hypot(...d);
        d = d.map((v) => v / len);
        const step = 0.028 + rand() * 0.02;
        const q = [p[0] + d[0] * step, p[1] + d[1] * step, p[2] + d[2] * step];
        const ta = t0 + (s / steps) * (1 - t0) * 0.9;
        const tb = t0 + ((s + 1) / steps) * (1 - t0) * 0.9;
        addSeg(p, q, col0.clone().lerp(col1, ta), col0.clone().lerp(col1, tb), r, ta, tb, ph, depth === 0 ? 0.34 : 0.24);
        p = q;
        if (depth < 2 && s > 3 && rand() < 0.1) {
          grow(p, [d[0] + jitter(0.8), d[1] + jitter(0.8), d[2] + jitter(0.8)], (steps - s) * (0.5 + rand() * 0.4) | 0, r, depth + 1, tb, ph, whiteness);
        }
      }
      addPoint(p, col1, 1.4 + rand() * 1.6, r, 0.55); // terminal
    };
    for (let k = 0; k < trees; k++) {
      const root = rand() < 0.75 && nodes.length ? nodes[(rand() * nodes.length) | 0] : this.shell[(rand() * this.shell.length) | 0];
      const len = Math.hypot(...root.p) || 1;
      const outward = root.p.map((v) => v / len);
      const long = rand() < 0.22;
      grow(root.p, outward, long ? 34 + (rand() * 20) | 0 : 12 + (rand() * 14) | 0, root.r, 0, 0, rand(), long ? 0.25 + rand() * 0.3 : rand() * 0.12);
    }

    this.group.add(new THREE.LineSegments(
      geometry({ position: [lPos, 3], color: [lCol, 3], aRegion: [lReg, 1], aT: [lT, 1], aPhase: [lPhase, 1], aBase: [lBase, 1] }),
      material(linesVertex, linesFragment, this.uniforms),
    ));
    this.group.add(new THREE.Points(
      geometry({ position: [pPos, 3], color: [pCol, 3], size: [pSize, 1], aRegion: [pReg, 1], aPhase: [pPhase, 1], aBase: [pBase, 1] }),
      material(pointsVertex, pointsFragment, this.uniforms),
    ));
  }

  // Lo que cambia en cada fotograma: impulsos, agentes, brillo de las regiones y haces.
  buildDynamic() {
    const n = MAX_PULSES * TRAIL + MAX_SATS * 2 + MAX_REGIONS;
    this.sprites = geometry({ position: [new Float32Array(n * 3), 3], color: [new Float32Array(n * 3), 3], size: [new Float32Array(n), 1], alpha: [new Float32Array(n), 1] });
    this.sprites.getAttribute("position").setUsage(THREE.DynamicDrawUsage);
    const sm = material(spriteVertex, pointsFragment, { uPixel: this.uniforms.uPixel, uDist: this.uniforms.uDist });
    const sp = new THREE.Points(this.sprites, sm);
    sp.frustumCulled = false;
    this.group.add(sp);
    this.spriteCount = n;

    const b = MAX_SATS * BEAM_SEGS * 2;
    this.beams = geometry({ position: [new Float32Array(b * 3), 3], color: [new Float32Array(b * 3), 3], alpha: [new Float32Array(b), 1], aT: [new Float32Array(b), 1] });
    const bl = new THREE.LineSegments(this.beams, material(beamVertex, linesFragment, { uTime: this.uniforms.uTime }));
    bl.frustumCulled = false;
    this.group.add(bl);
  }

  // Halo del estado detrás del cerebro (ámbar en espera, cian escuchando...).
  buildGlow() {
    this.glow = new THREE.Mesh(
      new THREE.PlaneGeometry(4.2, 4.2),
      new THREE.ShaderMaterial({
        uniforms: { uColor: { value: new THREE.Color() }, uStrength: { value: 0.2 } },
        vertexShader: glowVertex, fragmentShader: glowFragment,
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      }),
    );
    this.glow.position.z = -1.2;
    this.scene.add(this.glow);
  }

  resize() {
    const { width, height } = this.canvas.getBoundingClientRect();
    if (!width || !height) return;
    const dpr = Math.min(lowPower ? 1.5 : 2, devicePixelRatio || 1);
    this.renderer.setPixelRatio(dpr);
    this.renderer.setSize(width, height, false);
    this.composer.setPixelRatio(dpr);
    this.composer.setSize(width, height);
    this.bloom.resolution.set(width / 2, height / 2);
    this.camera.aspect = width / height;
    // Que quepa entero (con las dendritas) a lo alto y a lo ancho.
    const radius = this.camera.aspect < 0.9 ? 1.3 : 1.75; // en vertical, más grande: las puntas se funden con el borde
    const vfov = THREE.MathUtils.degToRad(this.camera.fov) / 2;
    const hfov = Math.atan(Math.tan(vfov) * this.camera.aspect);
    this.dist = radius / Math.sin(Math.min(vfov, hfov)) * 0.92;
    this.uniforms.uDist.value = this.dist;
    this.uniforms.uPixel.value = dpr * Math.min(1.6, Math.max(0.8, height / 700));
    this.camera.updateProjectionMatrix();
  }

  // Proyecta un punto del cerebro a píxeles del lienzo de hud.js: [x, y, escala, profundidad].
  project(p, out, W, H) {
    const v = this.tmp.set(p[0], p[1], p[2]).applyMatrix4(this.group.matrixWorld);
    const zView = v.clone().applyMatrix4(this.camera.matrixWorldInverse).z;
    v.project(this.camera);
    out[0] = (v.x + 1) / 2 * W;
    out[1] = (1 - v.y) / 2 * H;
    out[2] = 1;
    out[3] = Math.max(0, Math.min(1, (zView + this.dist + 1) / 2));
    return out;
  }

  // Punto de una curva entre a y b que se abomba hacia fuera del cerebro.
  static arc(a, b, t, lift = 0.35) {
    const m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    const len = Math.hypot(...m) || 1;
    const c = m.map((v, i) => v + (v / len) * lift + (i === 1 ? 0.15 : 0));
    const u = 1 - t;
    return [0, 1, 2].map((i) => u * u * a[i] + 2 * u * t * c[i] + t * t * b[i]);
  }

  render({ now, dt, color, level, burst, regionState, pulses, satellites, W, H }) {
    const t = now / 1000;
    this.uniforms.uTime.value = t;
    this.uniforms.uEnergy.value = Math.min(1, level * 1.2 + burst * 0.5);
    regionState.forEach((r, i) => { this.uniforms.uAct.value[i] = r.act; });

    this.spin += dt * (reducedMotion ? 0.05 : 0.16);
    this.group.rotation.y = this.spin;
    this.group.position.y = Math.sin(t / 3) * 0.03;
    const cam = this.camera;
    cam.position.x += (this.pointer[0] * 0.35 - cam.position.x) * Math.min(1, dt * 2);
    cam.position.y += (0.18 - this.pointer[1] * 0.2 - cam.position.y) * Math.min(1, dt * 2);
    cam.position.z = this.dist;
    cam.lookAt(0, 0, 0);
    this.glow.lookAt(cam.position);
    this.glow.material.uniforms.uColor.value.setRGB(color[0] / 255, color[1] / 255, color[2] / 255, THREE.SRGBColorSpace);
    this.glow.material.uniforms.uStrength.value = 0.07 + level * 0.2 + burst * 0.08;
    this.scene.updateMatrixWorld();

    // Agentes: en órbita, en coordenadas del cerebro (giran con él).
    const pf = this.regions.findIndex((r) => r.id === "prefrontal");
    const satPos = new Map();
    for (const sat of satellites.values()) {
      const bob = Math.sin(now / 900 + sat.slot) * 0.05;
      satPos.set(sat.key, [sat.pos[0] * 1.05, sat.pos[1] + bob, sat.pos[2] * 1.05]);
    }

    const pos = this.sprites.getAttribute("position").array;
    const col = this.sprites.getAttribute("color").array;
    const size = this.sprites.getAttribute("size").array;
    const alpha = this.sprites.getAttribute("alpha").array;
    let k = 0;
    const sprite = (p, rgb, s, a) => {
      if (k >= this.spriteCount) return;
      pos.set(p, k * 3);
      col[k * 3] = rgb[0] / 255; col[k * 3 + 1] = rgb[1] / 255; col[k * 3 + 2] = rgb[2] / 255;
      size[k] = s; alpha[k] = a; k++;
    };

    // Brillo de cada región activa: la mancha de color del vídeo.
    regionState.forEach((r, i) => {
      if (r.act > 0.2) sprite(this.regions[i].pos, this.regions[i].color, 40 + r.act * 70, (r.act - 0.2) * 0.5);
    });

    // Impulsos: estela que viaja por un arco entre regiones o entre un agente y una región.
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i];
      p.t += dt / p.dur;
      const a = p.fromSat ? satPos.get(p.fromSat) : this.regions[p.from]?.pos;
      const b = p.toSat ? satPos.get(p.toSat) : this.regions[p.to]?.pos;
      if (!a || !b || p.t >= 1.3) { pulses.splice(i, 1); continue; }
      const rgb = p.rgb || this.regions[p.to].color;
      for (let s = 0; s < TRAIL; s++) {
        const tt = Math.min(1, p.t) - s * 0.03;
        if (tt < 0) break;
        sprite(Brain3D.arc(a, b, tt), s === 0 ? [255, 255, 255] : rgb, (s === 0 ? 9 : 7) - s * 0.5, (1 - s / TRAIL) * 0.95);
      }
      if (p.t >= 1) sprite(b, rgb, 30 * (1.3 - p.t) / 0.3, 0.6); // destello al llegar
    }

    // Agentes: núcleo blanco con halo de su color, y el haz hasta el prefrontal.
    const bpos = this.beams.getAttribute("position").array;
    const bcol = this.beams.getAttribute("color").array;
    const balpha = this.beams.getAttribute("alpha").array;
    const bt = this.beams.getAttribute("aT").array;
    let m = 0;
    for (const sat of satellites.values()) {
      const p = satPos.get(sat.key);
      const rgb = sat.state === "done" ? [48, 209, 88] : sat.state === "error" ? [255, 69, 58] : sat.rgb;
      const beat = sat.state === "working" ? 0.5 + 0.5 * Math.sin(now / 260) : 0.6;
      sprite(p, rgb, 34 + beat * 16, 0.55);
      sprite(p, [255, 255, 255], 8, 1);
      if (m >= MAX_SATS * BEAM_SEGS * 2) continue;
      let prev = this.regions[pf].pos;
      for (let s = 1; s <= BEAM_SEGS && m < MAX_SATS * BEAM_SEGS * 2; s++) {
        const cur = Brain3D.arc(this.regions[pf].pos, p, s / BEAM_SEGS, 0.5);
        for (const [q, tt] of [[prev, (s - 1) / BEAM_SEGS], [cur, s / BEAM_SEGS]]) {
          bpos.set(q, m * 3);
          bcol[m * 3] = rgb[0] / 255; bcol[m * 3 + 1] = rgb[1] / 255; bcol[m * 3 + 2] = rgb[2] / 255;
          balpha[m] = 0.7; bt[m] = tt; m++;
        }
        prev = cur;
      }
    }
    for (let i = k; i < this.spriteCount; i++) alpha[i] = 0;
    for (let i = m; i < balpha.length; i++) balpha[i] = 0;
    for (const name of ["position", "color", "size", "alpha"]) this.sprites.getAttribute(name).needsUpdate = true;
    for (const name of ["position", "color", "alpha", "aT"]) this.beams.getAttribute(name).needsUpdate = true;

    this.composer.render(dt);

    // Posiciones en pantalla para las etiquetas HTML de hud.js.
    const centers = this.regions.map((r) => this.project(r.pos, [0, 0, 0, 0], W, H));
    for (const sat of satellites.values()) this.project(satPos.get(sat.key), sat.proj, W, H);
    return centers;
  }
}

// Se registra solo si hay WebGL; si no, hud.js sigue con el dibujo en 2D.
try {
  const canvas = document.getElementById("gl");
  if (canvas && window.JARVIS_REGIONS) window.brain3d = new Brain3D(canvas, window.JARVIS_REGIONS);
} catch (err) {
  console.warn("Cerebro 3D no disponible, sigo en 2D:", err);
}
