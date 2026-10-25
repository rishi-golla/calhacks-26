"use client";
/**
 * First-load intro: Senra-style hairline grid drawing itself in with GSAP
 * (scaleX / scaleY from 0, quint.inOut, staggered), wordmark fade, then the
 * overlay lifts. Plays once per session; skipped under reduced motion.
 *
 * The whole screen sits inside Canvas UI's Grid, so on Chrome with
 * html-in-canvas enabled the intro is a field of 3D tiles that ripple away
 * from the cursor (and on their own while idle). Everywhere else the Grid
 * renders its children as plain HTML, so the intro is unchanged.
 */
import { useEffect, useRef, useState } from "react";
import gsap from "gsap";
import { Grid } from "@/components/canvasui/Grid";

const KEY = "freightline.intro.v1";
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
    let seen = false;
    try {
      seen = sessionStorage.getItem(KEY) === "1";
    } catch {
      /* ignore */
    }
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (seen || reduced) {
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
      onComplete: () => {
        try {
          sessionStorage.setItem(KEY, "1");
        } catch {
          /* ignore */
        }
        setShow(false);
        done.current?.();
      },
    });
    tl.to(xs, { scaleX: 1, duration: 1.4, stagger: 0.08 }, 0)
      .to(ys, { scaleY: 1, duration: 1.4, stagger: 0.06 }, 0.15)
      .to(word, { opacity: 1, duration: 1.1, ease: "quint.out" }, 0.7)
      .to(word, { y: -10, opacity: 0, duration: 0.5, ease: "quint.in" }, "+=0.5")
      .to([xs, ys], { opacity: 0, duration: 0.6, ease: "quint.out", stagger: 0.015 }, "<")
      .to(el, { yPercent: -100, duration: 0.8, ease: "quint.inOut" }, "-=0.2");
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
            <small>Autonomous back office</small>
          </div>
        </div>
      </Grid>
    </div>
  );
}
