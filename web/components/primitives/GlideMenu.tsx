"use client";

import {
  useRef,
  useState,
  type FocusEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

type Highlight = {
  top: number;
  left: number;
  width: number;
  height: number;
  visible: boolean;
};

type GlideMenuProps = {
  children: ReactNode;
  className?: string;
  highlightClassName?: string;
  rowSelector?: string;
};

export default function GlideMenu({
  children,
  className = "",
  highlightClassName = "",
  rowSelector = "[data-glide-row]",
}: GlideMenuProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [highlight, setHighlight] = useState<Highlight | null>(null);

  const moveToRow = (target: EventTarget | null) => {
    if (!(target instanceof Element)) return;
    const root = rootRef.current;
    const row = target.closest(rowSelector);
    if (!root || !row || !root.contains(row)) return;
    const rowRect = row.getBoundingClientRect();
    const rootRect = root.getBoundingClientRect();
    setHighlight({
      top: rowRect.top - rootRect.top,
      left: rowRect.left - rootRect.left,
      width: rowRect.width,
      height: rowRect.height,
      visible: true,
    });
  };

  const hideHighlight = () =>
    setHighlight((current) =>
      current ? { ...current, visible: false } : current,
    );

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null))
      hideHighlight();
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) =>
    moveToRow(event.target);

  return (
    <div
      ref={rootRef}
      className={`glide-menu ${className}`}
      onPointerMove={handlePointerMove}
      onPointerLeave={hideHighlight}
      onFocusCapture={(event) => moveToRow(event.target)}
      onBlurCapture={handleBlur}
    >
      <div
        aria-hidden="true"
        className={`glide-menu-highlight ${highlightClassName}`}
        style={{
          height: highlight?.height ?? 0,
          opacity: highlight?.visible ? 1 : 0,
          transform: `translate3d(${highlight?.left ?? 0}px, ${highlight?.top ?? 0}px, 0)`,
          width: highlight?.width ?? 0,
        }}
      />
      <div className="glide-menu-content">{children}</div>
    </div>
  );
}
