"use client";
/**
 * Canvas UI Decrypt Reveal sized to its content. The effect lays its children
 * out inside a canvas, so its box needs an explicit height: we measure the
 * children and feed that back. With html-in-canvas (Chrome flag) the block
 * reads as cipher glyphs that decrypt around the cursor; everywhere else the
 * children render as plain HTML. The cipher's backdrop is read from the
 * nearest `bgFrom` ancestor so empty space stays empty.
 */
import { useCallback, useRef, useState, type ReactNode } from "react";
// Static import: it has no dependencies, and a lazy chunk would blank the fields while it loads.
import { DecryptReveal } from "@/components/canvasui/DecryptReveal";

/** Teal accent, matching the intro's Grid tint. */
const CIPHER = "#22c09b";

export function DecryptBlock({ children, bgFrom }: { children: ReactNode; bgFrom: string }) {
  const [height, setHeight] = useState<number>();
  const [background, setBackground] = useState<string>();
  const observer = useRef<ResizeObserver | null>(null);
  // Callback ref: the effect remounts the content when it falls back, so follow the element.
  const measure = useCallback(
    (el: HTMLDivElement | null) => {
      observer.current?.disconnect();
      observer.current = null;
      if (!el) return;
      const bg = getComputedStyle(el.closest(bgFrom) ?? el).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)") setBackground(bg);
      const update = () => setHeight(Math.ceil(el.getBoundingClientRect().height));
      observer.current = new ResizeObserver(update);
      observer.current.observe(el);
      update();
    },
    [bgFrom],
  );
  return (
    <DecryptReveal
      style={{ height }}
      radius={130}
      cell={9}
      color={CIPHER}
      colored={0.35}
      passthrough={0.12}
      aberration={4}
      edgeGlow={1.2}
      {...(background ? { background } : {})}
    >
      <div ref={measure}>{children}</div>
    </DecryptReveal>
  );
}
