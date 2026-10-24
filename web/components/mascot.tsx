"use client";
import type { CSSProperties } from "react";
import "./mascot.css";

const COLORS: Record<string, string> = {
  all: "#14b88a",
  carrier_ap: "#2f7bff",
  shipper_ar: "#ff3b5c",
  risk: "#ff4fa3",
  verification: "#8b5cff",
  controller: "#6b7a90",
  voice_desk: "#ff7a1a",
  cfo: "#ffc21a",
  validator: "#14b8a6",
  documents: "#b07445",
};

export const MASCOT_IDS = Object.keys(COLORS);

export function Mascot({
  id,
  size = 40,
  working = false,
  className = "",
}: {
  id: string;
  size?: number;
  working?: boolean;
  className?: string;
}) {
  const known = id in COLORS;
  const delay =
    ([...id].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 40) / 10;
  return (
    <span
      className={`mascot ${working ? "working" : ""} ${className}`}
      data-mascot={known ? id : undefined}
      aria-hidden="true"
      style={
        {
          width: size,
          height: size,
          "--mascot-color": COLORS[id] ?? "#8e8e93",
          "--mascot-delay": `${-delay}s`,
        } as CSSProperties
      }
    >
      <span className="mascot-shadow" />
      <span className="mascot-body">
        {known ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            className="mascot-figure"
            src={`/mascots/${id}.webp`}
            width={size}
            height={size}
            alt=""
            draggable={false}
          />
        ) : (
          <svg className="mascot-figure" viewBox="0 0 120 120">
            <circle cx="60" cy="62" r="40" fill="#8e8e93" />
            <path
              d="M47 55.5V64.5M73 55.5V64.5"
              stroke="#fff"
              strokeWidth={9.5}
              strokeLinecap="round"
            />
          </svg>
        )}
      </span>
    </span>
  );
}
