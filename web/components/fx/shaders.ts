/**
 * WGSL for the backdrop variants. Raw strings; `backdrop-runtime.ts` wraps them
 * with `prepareShader()` (no bundler loader in this app).
 *
 * Every variant is one fullscreen fragment pass over the same uniform block and
 * writes premultiplied alpha, so the canvas composites over whatever page
 * surface sits behind it. `uv` is top-origin (vgpu convention).
 */
import type { BackdropVariant } from "./backdrop";

const PRELUDE = /* wgsl */ `
struct Params {
  color: vec3f,
  time: f32,
  color2: vec3f,
  intensity: f32,
  resolution: vec2f,
  dpr: f32,
}
@group(0) @binding(0) var<uniform> params: Params;

const TAU: f32 = 6.2831853;

fn hash21(p: vec2f) -> f32 {
  var q = fract(p * vec2f(123.34, 456.21));
  q += dot(q, q + 45.32);
  return fract(q.x * q.y);
}

fn hash22(p: vec2f) -> vec2f {
  let n = hash21(p);
  return vec2f(n, hash21(p + n + 17.17));
}

fn vnoise(p: vec2f) -> f32 {
  let i = floor(p);
  let f = fract(p);
  let u = f * f * (3.0 - 2.0 * f);
  let a = hash21(i);
  let b = hash21(i + vec2f(1.0, 0.0));
  let c = hash21(i + vec2f(0.0, 1.0));
  let d = hash21(i + vec2f(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// Premultiplied "src over dst".
fn over(top: vec4f, bottom: vec4f) -> vec4f {
  return top + bottom * (1.0 - top.a);
}

// Light of colour c at coverage a, premultiplied.
fn light(c: vec3f, a: f32) -> vec4f {
  let k = clamp(a, 0.0, 1.0);
  return vec4f(c * k, k);
}

// Final output: triangular +-1/255 dither on the premultiplied colour so slow
// dark gradients (glow, vignette) do not band; rgb stays <= alpha.
fn finish(c: vec4f, px: vec2f) -> vec4f {
  let n = (hash21(px) + hash21(px + vec2f(0.371, 0.813)) - 1.0) / 255.0;
  let a = clamp(c.a + n, 0.0, 1.0);
  return vec4f(clamp(c.rgb + n, vec3f(0.0), vec3f(a)), a);
}
`;

/** Sparse drifting dot field, three parallax layers, ambient glow. */
const FIELD = /* wgsl */ `${PRELUDE}
fn layer(px: vec2f, t: f32, cell: f32, radius: f32, speed: f32, seed: f32, density: f32) -> f32 {
  let dir = normalize(vec2f(-1.0, -0.32));
  // Slow large-scale sway so the field does not read as a rigid scroll.
  let sway = vec2f(sin(t * 0.05 + px.y * 0.0021 + seed), cos(t * 0.043 + px.x * 0.0017 + seed)) * 14.0;
  let p = px - dir * speed * t + sway;
  let g = p / cell;
  let id = floor(g);
  let f = fract(g);
  let h = hash22(id + seed * 31.7);
  if (hash21(id * 1.73 + seed * 7.1) > density) {
    return 0.0;
  }
  let wob = vec2f(sin(t * (0.21 + 0.3 * h.x) + h.y * TAU), cos(t * (0.17 + 0.3 * h.y) + h.x * TAU)) * 0.1;
  let center = 0.22 + 0.56 * h + wob;
  let d = (f - center) * cell;
  // Gaussian dot widened by half a device pixel, energy kept constant, so
  // sub-pixel motion does not shimmer.
  let aa = 0.5 / params.dpr;
  let s2 = radius * radius + aa * aa;
  let dotv = exp(-dot(d, d) / (2.0 * s2)) * (radius * radius / s2);
  let twinkle = 0.55 + 0.45 * sin(t * (0.5 + 1.3 * h.x) + h.y * 40.0);
  // A few dots carry a faint halo.
  let glint = select(0.0, exp(-dot(d, d) / 32.0) * 0.14, h.x > 0.93);
  return (dotv + glint) * twinkle;
}

@fragment fn fs_main(@location(0) uv: vec2f) -> @location(0) vec4f {
  let res = params.resolution;
  let frag = uv * res;
  let px = frag / params.dpr;
  let t = params.time;
  let k = clamp(params.intensity, 0.0, 1.0);
  let aspect = res.x / max(res.y, 1.0);
  let density = mix(0.22, 0.62, k);

  var dots = layer(px, t, 30.0, 0.55, 3.0, 1.0, density) * 0.38;
  dots += layer(px, t, 52.0, 0.9, 6.5, 2.0, density) * 0.66;
  dots += layer(px, t, 88.0, 1.3, 12.0, 3.0, density) * 1.0;

  let q = (uv - vec2f(0.5, 0.42)) * vec2f(aspect, 1.0);
  let ambient = exp(-dot(q, q) * 2.4) * 0.06;

  let v = smoothstep(0.35, 1.05, length((uv - 0.5) * vec2f(aspect * 0.85, 1.15)));
  let fade = 1.0 - 0.7 * v;

  let a = (dots * mix(0.55, 1.0, k) + ambient * k) * fade;
  let c = mix(params.color, vec3f(1.0), clamp(dots * 0.35, 0.0, 0.4));
  let vignette = vec4f(0.0, 0.0, 0.0, v * 0.4);
  return finish(over(light(c, a), vignette), frag);
}
`;

/** Noise-advected horizontal streamlines with travelling packets, colour -> color2. */
const FLOW = /* wgsl */ `${PRELUDE}
@fragment fn fs_main(@location(0) uv: vec2f) -> @location(0) vec4f {
  let res = params.resolution;
  let frag = uv * res;
  let px = frag / params.dpr;
  let t = params.time;
  let k = clamp(params.intensity, 0.0, 1.0);

  let n1 = vnoise(vec2f(px.x * 0.0032 - t * 0.045, px.y * 0.0052 + 3.1));
  let n2 = vnoise(vec2f(px.x * 0.0085 - t * 0.1, px.y * 0.011 + 7.7));
  let yy = px.y + (n1 - 0.5) * 84.0 + (n2 - 0.5) * 24.0;

  let spacing = 16.0;
  let lc = yy / spacing;
  let id = floor(lc);
  let dpx = abs(fract(lc) - 0.5) * spacing;
  let w = max(fwidth(lc) * spacing, 0.25);
  let line = clamp(1.0 - dpx / w, 0.0, 1.0);

  let hl = hash21(vec2f(id, 3.7));
  let base = 0.07 + 0.2 * hl * hl;

  // Packets: comets flowing left to right on some lines.
  let carries = hash21(vec2f(id, 5.3)) < mix(0.18, 0.45, k);
  let speed = 50.0 + 140.0 * hash21(vec2f(id, 8.1));
  let span = 240.0 + 420.0 * hash21(vec2f(id, 9.9));
  let p = fract((px.x - t * speed) / span + hl * 7.0);
  let comet = pow(p, 18.0) * smoothstep(1.0, 0.992, p);
  let packet = select(0.0, comet, carries);

  // Strongest through the middle band, softer toward the edges.
  let yb = (uv.y - 0.48) * 2.1;
  let my = exp(-yb * yb);
  let mx = smoothstep(0.0, 0.18, uv.x) * smoothstep(1.0, 0.82, uv.x);
  let mask = (0.3 + 0.7 * my) * (0.45 + 0.55 * mx);

  let a = line * (base + packet * 0.85) * mask * mix(0.6, 1.25, k);
  let g = smoothstep(0.05, 0.95, uv.x + (n1 - 0.5) * 0.35);
  let c = mix(mix(params.color, params.color2, g), vec3f(1.0), packet * 0.3);
  let haze = exp(-yb * yb * 1.53) * 0.035 * k;
  return finish(light(c, a + haze * (0.6 + 0.4 * n1)), frag);
}
`;

/** Soft radial glow breathing at 0.2 Hz, plus a faint ring per breath. */
const GLOW = /* wgsl */ `${PRELUDE}
@fragment fn fs_main(@location(0) uv: vec2f) -> @location(0) vec4f {
  let res = params.resolution;
  let frag = uv * res;
  let t = params.time;
  let k = clamp(params.intensity, 0.0, 1.0);
  let aspect = res.x / max(res.y, 1.0);

  let phase = fract(t * 0.2);
  let breath = 0.5 - 0.5 * cos(phase * TAU);
  let q = (uv - vec2f(0.5, 0.4)) * vec2f(aspect, 1.0);
  let r2 = dot(q, q);
  let core = exp(-r2 * mix(4.6, 3.4, breath));
  let inner = exp(-r2 * 18.0);
  let ringR = 0.12 + 0.55 * phase;
  let rd = (sqrt(r2) - ringR) * 30.0;
  let ring = exp(-rd * rd) * (1.0 - phase) * (1.0 - phase) * 0.03;

  let a = (core * (0.095 + 0.065 * breath) + inner * 0.035 * breath + ring) * mix(0.55, 1.35, k);
  return finish(light(params.color, a), frag);
}
`;

export const SHADERS: Record<BackdropVariant, string> = { field: FIELD, flow: FLOW, glow: GLOW };

/** Frame-rate cap per variant: glow is nearly static, so it runs at half rate. */
export const FPS: Record<BackdropVariant, number> = { field: 60, flow: 60, glow: 30 };
