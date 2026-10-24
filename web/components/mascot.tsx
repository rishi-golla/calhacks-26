"use client";
import type { CSSProperties } from "react";
import "./mascot.css";

type Point = readonly [number, number];
type Spec = { color: string; shape: string; face: Point };

const round = (value: number) => Math.round(value * 100) / 100;
const rad = (deg: number) => (deg * Math.PI) / 180;
function roundedPolygon(points: Point[], t: number) {
  return (
    points
      .map(([x, y], i) => {
        const [px, py] = points[(i + points.length - 1) % points.length];
        const [nx, ny] = points[(i + 1) % points.length];
        return `${i ? "L" : "M"}${round(x + (px - x) * t)} ${round(y + (py - y) * t)}Q${round(x)} ${round(y)} ${round(x + (nx - x) * t)} ${round(y + (ny - y) * t)}`;
      })
      .join("") + "Z"
  );
}
const polar = (count: number, radius: number, cx: number, cy: number, offset = -90) =>
  Array.from({ length: count }, (_, i): Point => [
    cx + Math.cos(rad(offset + (i * 360) / count)) * radius,
    cy + Math.sin(rad(offset + (i * 360) / count)) * radius,
  ]);
const circle = (cx: number, cy: number, r: number) => `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0Z`;
const lobes = (count: number, ring: number, r: number, cx: number, cy: number) =>
  polar(count, ring, cx, cy).map(([x, y]) => circle(round(x), round(y), r)).join("") + circle(cx, cy, ring);

const SPECS: Record<string, Spec> = {
  all: {
    color: "#14856b",
    shape: circle(60, 62, 40),
    face: [60, 60],
  },
  carrier_ap: {
    color: "#1683ff",
    shape: "M60 24C91 24 98 31 98 62C98 93 91 100 60 100C29 100 22 93 22 62C22 31 29 24 60 24Z",
    face: [60, 60],
  },
  shipper_ar: {
    color: "#ff2d55",
    shape: "M42 33H78A29 29 0 0 1 78 91H42A29 29 0 0 1 42 33Z",
    face: [60, 61],
  },
  risk: {
    color: "#ff3d9a",
    shape: "M60 22C67 22 71 28 75 35L97 73C103 84 99 98 84 98H36C21 98 17 84 23 73L45 35C49 28 53 22 60 22Z",
    face: [60, 73],
  },
  verification: {
    color: "#8f55ff",
    shape: roundedPolygon(polar(6, 42, 60, 62), 0.16),
    face: [60, 62],
  },
  controller: {
    color: "#69717e",
    shape: "M24 60C24 38 40 22 60 22C80 22 96 38 96 60V90C96 95 93 98 88 98H32C27 98 24 95 24 90Z",
    face: [60, 62],
  },
  voice_desk: {
    color: "#ff6a00",
    shape: "M30 44C41 29 62 22 80 28C98 34 106 56 100 74C94 92 74 100 54 96C34 92 18 82 18 66C18 57 24 51 30 44Z",
    face: [60, 62],
  },
  cfo: {
    color: "#f59e0b",
    shape: "M60 18C64 18 67 21 70 25L88 50C94 58 98 66 98 76C98 92 81 104 60 104C39 104 22 92 22 76C22 66 26 58 32 50L50 25C53 21 56 18 60 18Z",
    face: [60, 75],
  },
  validator: {
    color: "#00b0a0",
    shape: lobes(6, 25, 17, 60, 62),
    face: [60, 62],
  },
  documents: {
    color: "#a86b3c",
    shape:
      circle(38, 72, 18) + circle(60, 56, 24) + circle(84, 66, 18) + circle(90, 86, 12) + circle(30, 86, 12) + "M30 78V98H90V78Z",
    face: [60, 72],
  },
};
const FALLBACK: Spec = { color: "#8b95a5", shape: circle(60, 62, 40), face: [60, 60] };

export const MASCOT_IDS = Object.keys(SPECS);

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
  const spec = SPECS[id] ?? FALLBACK;
  const [x, y] = spec.face;
  const delay = ([...id].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 40) / 10;
  return (
    <svg
      className={`mascot ${working ? "working" : ""} ${className}`}
      data-mascot={SPECS[id] ? id : undefined}
      width={size}
      height={size}
      viewBox="0 0 120 120"
      aria-hidden="true"
      focusable="false"
      style={{ "--blink-delay": `${delay}s` } as CSSProperties}
    >
      {working &&
        [0, 0.8].map((d) => (
          <circle
            key={d}
            className="mascot-ping"
            cx={60}
            cy={62}
            r={44}
            fill="none"
            stroke={spec.color}
            strokeWidth={5}
            style={{ animationDelay: `${d}s` }}
          />
        ))}
      <g className="mascot-figure">
        <path className="mascot-body" d={spec.shape} fill={spec.color} />
        <g className="mascot-eyes">
          <path
            className="mascot-gaze"
            d={`M${x - 13} ${y - 4.5}V${y + 4.5}M${x + 13} ${y - 4.5}V${y + 4.5}`}
            stroke="#fff"
            strokeWidth={9.5}
            strokeLinecap="round"
          />
        </g>
      </g>
    </svg>
  );
}
