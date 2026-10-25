/**
 * Framework-free vgpu driver for the backdrop: one device, one canvas surface
 * and one fullscreen effect per mount.
 *
 * Never throws. Init, pipeline and device errors go to `onFail` so the React
 * layer can swap in the CSS fallback. The loop runs only while the tab is
 * visible and the canvas is on screen; under reduced motion it renders a single
 * still frame (again on resize) and never starts a loop.
 */
import { prepareShader } from "@vgpu/wgsl/prepare";
import { clock, effect, frame, frameLoop, init, surface } from "vgpu";
import type { Clock, Effect, Frame, FrameLoopHandle, Gpu, ShaderSource, Surface } from "vgpu";
import type { BackdropVariant } from "./backdrop";
import { FPS, SHADERS } from "./shaders";

export type BackdropParams = { color: string; intensity: number };
export type BackdropRuntime = {
  update(params: BackdropParams): void;
  dispose(): void;
};
type StartOptions = BackdropParams & {
  variant: BackdropVariant;
  reduced: boolean;
  onReady(): void;
  onFail(error: unknown): void;
};

type Rgb = [number, number, number];
const TEAL: Rgb = [0x22 / 255, 0xc0 / 255, 0x9b / 255];
const AQUA: Rgb = [0x56 / 255, 0xa9 / 255, 0xd9 / 255];
/** Animation time of the reduced-motion still: glow mid-breath. */
const STILL_TIME = 6.5;
/** Longest step the animation clock takes, so a resumed loop does not jump. */
const MAX_STEP = 1 / 15;
/** Longest wait for the async pipeline before starting the loop anyway. */
const COMPILE_WAIT_MS = 2500;

/** User Timing marks (`fl-backdrop:*`) so start-up shows in the Performance panel. */
function mark(name: string) {
  try {
    performance.mark(`fl-backdrop:${name}`);
  } catch {
    /* unsupported */
  }
}

const prepared = new Map<BackdropVariant, ShaderSource>();
function shaderFor(variant: BackdropVariant): ShaderSource {
  let source = prepared.get(variant);
  if (!source) {
    source = prepareShader(SHADERS[variant]);
    prepared.set(variant, source);
  }
  return source;
}

/** `#rgb`, `#rrggbb`, `#rrggbbaa` or `rgb()/rgba()` to linear 0..1 channels; teal otherwise. */
export function parseColor(value: string): Rgb {
  const v = value.trim();
  const hex = /^#([0-9a-f]{3,8})$/i.exec(v);
  if (hex) {
    let h = hex[1];
    if (h.length === 3 || h.length === 4) h = [...h.slice(0, 3)].map((c) => c + c).join("");
    if (h.length === 6 || h.length === 8) return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255) as Rgb;
  }
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(v);
  if (rgb) return [rgb[1], rgb[2], rgb[3]].map((x) => Math.min(255, Number(x)) / 255) as Rgb;
  return TEAL;
}

export function startBackdrop(canvas: HTMLCanvasElement, opts: StartOptions): BackdropRuntime {
  let disposed = false;
  let failed = false;
  let ready = false;
  let gpu: Gpu | undefined;
  let surf: Surface | undefined;
  let fx: Effect | undefined;
  let time: Clock | undefined;
  let loop: FrameLoopHandle | undefined;
  let onScreen = true;
  let t = 0;
  let params: BackdropParams = { color: opts.color, intensity: opts.intensity };
  const cleanups: (() => void)[] = [];

  const uniforms = () => {
    const color = parseColor(params.color);
    const intensity = Math.min(1, Math.max(0, Number.isFinite(params.intensity) ? params.intensity : 0.6));
    return { color, color2: opts.variant === "flow" ? AQUA : color, intensity };
  };

  const teardown = () => {
    loop?.stop();
    loop = undefined;
    for (const undo of cleanups.splice(0)) {
      try {
        undo();
      } catch {
        /* already gone */
      }
    }
    try {
      gpu?.dispose();
    } catch {
      /* device already lost */
    }
    gpu = surf = fx = time = undefined;
  };

  const fail = (error: unknown) => {
    if (disposed || failed) return;
    failed = true;
    teardown();
    if (process.env.NODE_ENV !== "production") console.warn("[fx] WebGPU backdrop disabled, using CSS fallback:", error);
    opts.onFail(error);
  };

  const markReady = () => {
    if (ready) return;
    ready = true;
    mark("first-frame");
    opts.onReady();
  };

  const encode = (f: Frame, at: number) => {
    fx!.set({ params: { time: at } });
    f.pass(surf!, fx!);
  };

  const renderStill = () => {
    if (!gpu || !surf || !fx) return;
    try {
      frame(gpu, (f) => encode(f, STILL_TIME));
      markReady();
    } catch (error) {
      fail(error);
    }
  };

  const sync = () => {
    if (!gpu || !surf || !fx || !time) return;
    const run = !disposed && !failed && !opts.reduced && onScreen && !document.hidden;
    if (run && !loop) {
      const c = time;
      loop = frameLoop(
        gpu,
        (f) => {
          try {
            t += Math.min(c.deltaTime, MAX_STEP);
            encode(f, t);
            markReady();
          } catch (error) {
            // Tear down after this tick returns, never from inside the frame.
            queueMicrotask(() => fail(error));
          }
        },
        { fps: FPS[opts.variant] },
      );
    } else if (!run && loop) {
      loop.stop();
      loop = undefined;
    }
  };

  void (async () => {
    try {
      mark("init");
      const g = await init({ powerPreference: "low-power" });
      mark("device");
      if (disposed) {
        g.dispose();
        return;
      }
      gpu = g;
      cleanups.push(g.onError((error) => fail(error)));
      void g.lost.then((info) => fail(new Error(`WebGPU device lost (${info.reason}): ${info.message}`)));

      surf = surface(g, canvas, { dpr: [1, 2], clearColor: [0, 0, 0, 0], label: "fl-backdrop" });
      fx = effect(g, shaderFor(opts.variant), {
        label: `fl-backdrop-${opts.variant}`,
        set: { params: { ...uniforms(), time: 0, resolution: surf.size, dpr: surf.dpr } },
      });
      time = clock(g);
      const target = surf;
      cleanups.push(target.onResize(({ width, height, dpr }) => fx?.set({ params: { resolution: [width, height], dpr } })));

      // Build the pipeline off the critical path so the first frame does not
      // hitch, but never wait on a busy GPU process for long: past the cap the
      // first pass picks the pipeline up itself.
      const compiled = fx.compile(target).then(
        () => true,
        (error) => {
          fail(error);
          return false;
        },
      );
      const ok = await Promise.race([compiled, new Promise<boolean>((resolve) => setTimeout(resolve, COMPILE_WAIT_MS, true))]);
      mark("compiled");
      if (!ok || disposed || failed) return;

      const io = new IntersectionObserver((entries) => {
        onScreen = entries.some((e) => e.isIntersecting);
        sync();
      });
      io.observe(canvas);
      cleanups.push(() => io.disconnect());
      document.addEventListener("visibilitychange", sync);
      cleanups.push(() => document.removeEventListener("visibilitychange", sync));

      if (opts.reduced) {
        // The surface resizes at frame boundaries, so redraw the still on layout changes.
        const ro = new ResizeObserver(() => renderStill());
        ro.observe(canvas);
        cleanups.push(() => ro.disconnect());
      } else {
        sync();
      }
    } catch (error) {
      fail(error);
    }
  })();

  return {
    update(next) {
      params = next;
      if (!fx) return;
      fx.set({ params: uniforms() });
      if (opts.reduced) renderStill();
    },
    dispose() {
      disposed = true;
      teardown();
    },
  };
}
