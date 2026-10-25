"use client";
/**
 * First-load intro: Senra-style hairline grid drawing itself in with GSAP
 * (scaleX / scaleY from 0, quint.inOut, staggered), wordmark fade, then the
 * overlay lifts. Plays on every full page load (a refresh or a typed URL), not
 * on in-app navigation, since the shell stays mounted. Skipped under reduced
 * motion, or when sessionStorage `freightline.intro.v1` is "1" (screenshot and
 * test scripts set it to land on the page directly).
 * `onDone` fires when the overlay starts lifting (or at once when skipped).
 *
 * The whole screen sits inside Canvas UI's Grid, so on Chrome with
 * html-in-canvas enabled the intro is a field of 3D tiles that ripple away
 * from the cursor (and on their own while idle). Everywhere else the Grid
 * renders its children as plain HTML, so the intro is unchanged.
 */
import { useEffect, useRef, useState } from "react";
import gsap from "gsap";
import { Grid } from "@/components/canvasui/Grid";

/** Opt-out for automation; the intro itself never sets it. */
const SKIP_KEY = "freightline.intro.v1";
const COLS = 8;
const ROWS = 5;
/** Teal accent as 0..1 RGB for the lifted-tile tint. */
const TINT: [number, number, number] = [0x22 / 255, 0xc0 / 255, 0x9b / 255];

export default function Intro({ onDone }: { onDone?: () => void }) {
  const [show, setShow] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  // Parent re-renders every second; keep the callback out of effect deps.
  const done = useRef(onDone);
  done.current = onDone;
  useEffect(() => {
    let skip = false;
    try {
      skip = sessionStorage.getItem(SKIP_KEY) === "1";
    } catch {
      /* ignore */
    }
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (skip || reduced) {
      done.current?.();
      return;
    }
    setShow(true);
  }, []);
  useEffect(() => {
    if (!show || !root.current) return;
    const el = root.current;
    const xs = el.querySelectorAll<HTMLElement>(".fl-intro-grid i.x");
    const ys = el.querySelectorAll<HTMLElement>(".fl-intro-grid i.y");
    const word = el.querySelector<HTMLElement>(".fl-intro-word");
    const tl = gsap.timeline({
      defaults: { ease: "quint.inOut" },
      onComplete: () => setShow(false),
    });
    tl.to(xs, { scaleX: 1, duration: 1.4, stagger: 0.08 }, 0)
      .to(ys, { scaleY: 1, duration: 1.4, stagger: 0.06 }, 0.15)
      .to(word, { opacity: 1, duration: 1.1, ease: "quint.out" }, 0.7)
      .to(word, { y: -10, opacity: 0, duration: 0.5, ease: "quint.in" }, "+=0.5")
      .to([xs, ys], { opacity: 0, duration: 0.6, ease: "quint.out", stagger: 0.015 }, "<")
      .addLabel("lift", "-=0.2")
      // Release the page's entry choreography as the overlay starts lifting, so it plays in view.
      .call(() => done.current?.(), undefined, "lift")
      .to(el, { yPercent: -100, duration: 0.8, ease: "quint.inOut" }, "lift");
    return () => {
      tl.kill();
    };
  }, [show]);
  if (!show) return null;
  return (
    <div ref={root} className="fl-intro" role="presentation">
      <Grid
        className="fl-intro-stage"
        tileSize={96}
        gap={1}
        amplitude={2}
        waveSpeed={0.45}
        frequency={10}
        waveWidth={0.06}
        liftHeight={28}
        maxLift={0.8}
        jitter={0.12}
        perspective={1400}
        tilt={0.6}
        shading={0.08}
        tint={TINT}
        tintStrength={0.18}
        idleRipples={1.6}
      >
        <div className="fl-intro-inner">
          <div className="fl-intro-grid" aria-hidden>
            {Array.from({ length: ROWS + 1 }, (_, i) => (
              <i key={`x${i}`} className="x" style={{ top: `${(i / ROWS) * 100}%` }} />
            ))}
            {Array.from({ length: COLS + 1 }, (_, i) => (
              <i key={`y${i}`} className="y" style={{ left: `${(i / COLS) * 100}%` }} />
            ))}
          </div>
          <div className="fl-intro-word">
            FreightLine
            <small>AUTONOMOUS BACK OFFICE</small>
          </div>
        </div>
      </Grid>
    </div>
  );
}
