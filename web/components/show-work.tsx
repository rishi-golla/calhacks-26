"use client";
/**
 * <ShowWork workings={...} title="Exposure">$1,225.00</ShowWork> makes a
 * figure a button that opens its shown work beside it. A native popover: it
 * sits in the top layer (clear of the panel's overflow), closes on Esc, an
 * outside click or a scroll, and keeps the surrounding theme's tokens because
 * it stays where it is in the DOM. Renders the figure plainly when there is
 * no work to show.
 */
import { useEffect, useId, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { Workings } from "./workings";
import type { Working } from "@/lib/workings";

const GAP = 6;
const EDGE = 8;

export function ShowWork({ workings, title, children, className = "" }: { workings: Working[]; title: string; children: ReactNode; className?: string }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const has = workings.length > 0;

  // Re-runs when the work arrives: the popover is not rendered until then.
  useEffect(() => {
    const el = pop.current;
    const button = trigger.current;
    if (!el || !button) return;
    // Close when a scroll moves the figure (the page or a container of it), not when an
    // unrelated panel scrolls (the live trace auto-scrolls) or the popover itself does.
    const hide = (e: Event) => {
      if (e.type === "scroll" && e.target !== document && !(e.target instanceof Node && e.target.contains(button))) return;
      el.hidePopover();
    };
    // Below the figure, or above it when there is no room; inside the viewport either way.
    const place = (e: Event) => {
      if ((e as ToggleEvent).newState !== "open") {
        delete el.dataset.placed;
        window.removeEventListener("scroll", hide, true);
        window.removeEventListener("resize", hide);
        return;
      }
      const at = button.getBoundingClientRect();
      const { width, height } = el.getBoundingClientRect();
      const left = Math.min(Math.max(EDGE, at.left), window.innerWidth - width - EDGE);
      const below = at.bottom + GAP;
      const top = below + height <= window.innerHeight - EDGE ? below : Math.max(EDGE, at.top - GAP - height);
      el.style.left = `${Math.max(EDGE, left)}px`;
      el.style.top = `${top}px`;
      // Hidden by CSS until here, so it never paints at the UA's default spot.
      el.dataset.placed = "";
      window.addEventListener("scroll", hide, true);
      window.addEventListener("resize", hide);
    };
    el.addEventListener("toggle", place);
    return () => {
      el.removeEventListener("toggle", place);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [has]);

  if (!has) return <>{children}</>;
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={`sw-trigger ${className}`}
        popoverTarget={id}
        aria-haspopup="dialog"
        title={`How ${title.toLowerCase()} is calculated`}
        // A figure inside a clickable row or card owns this click.
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </button>
      {/* The panel sits in the DOM under the figure: its clicks stay out of the row or card around it. */}
      <div ref={pop} id={id} popover="auto" role="dialog" aria-label={`${title}: shown work`} className="sw-pop" onClick={(e) => e.stopPropagation()}>
        <div className="sw-head">
          <span className="sw-title">{title}</span>
          <button type="button" className="sw-close" popoverTarget={id} popoverTargetAction="hide" aria-label="Close">
            <X size={13} aria-hidden />
          </button>
        </div>
        <Workings items={workings} title={null} />
      </div>
    </>
  );
}
