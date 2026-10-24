import type { CSSProperties, ReactNode } from "react";
import "./mascot.css";

type Palette = { body: string; deep: string };
const INK = "#1d2330";

export const MASCOT_COLORS: Record<string, Palette> = {
  carrier_ap: { body: "#6ca8ff", deep: "#3f7fe0" },
  shipper_ar: { body: "#ed91b5", deep: "#cf6390" },
  risk: { body: "#f8b86a", deep: "#d98a2f" },
  verification: { body: "#b99af7", deep: "#8b68e0" },
  controller: { body: "#89aaff", deep: "#5a7fe6" },
  voice_desk: { body: "#e2a272", deep: "#bf7a45" },
  cfo: { body: "#8fc38c", deep: "#5e9d5a" },
  validator: { body: "#b8a2e8", deep: "#8f74cf" },
  documents: { body: "#67c4b6", deep: "#3a9a8b" },
  all: { body: "#f5c451", deep: "#d29b1e" },
};
const FALLBACK: Palette = { body: "#a5b1c4", deep: "#7b879a" };

function Face({ x = 24, y = 23 }: { x?: number; y?: number }) {
  return (
    <g>
      <g className="mascot-eyes">
        <ellipse cx={x - 5} cy={y} rx={2.1} ry={2.6} fill={INK} />
        <ellipse cx={x + 5} cy={y} rx={2.1} ry={2.6} fill={INK} />
        <circle cx={x - 4.3} cy={y - 0.9} r={0.75} fill="#fff" />
        <circle cx={x + 5.7} cy={y - 0.9} r={0.75} fill="#fff" />
      </g>
      <ellipse cx={x - 8.6} cy={y + 3.6} rx={1.9} ry={1.15} fill="#fff" opacity={0.38} />
      <ellipse cx={x + 8.6} cy={y + 3.6} rx={1.9} ry={1.15} fill="#fff" opacity={0.38} />
      <path
        d={`M${x - 2.5} ${y + 4.1} Q${x} ${y + 6.4} ${x + 2.5} ${y + 4.1}`}
        stroke={INK}
        strokeWidth={1.6}
        strokeLinecap="round"
        fill="none"
      />
    </g>
  );
}

const SCALLOPS = Array.from({ length: 10 }, (_, i) => {
  const angle = (i / 10) * Math.PI * 2;
  return [24 + Math.cos(angle) * 13.6, 25 + Math.sin(angle) * 13.6] as const;
});

function figure(id: string, { body, deep }: Palette): ReactNode {
  switch (id) {
    case "carrier_ap": // truck cab
      return (
        <>
          <rect x={20} y={6.5} width={8} height={4.5} rx={2} fill={deep} />
          <rect x={8} y={10} width={32} height={26} rx={9} fill={body} />
          <circle cx={16} cy={37.5} r={4.2} fill={deep} />
          <circle cx={32} cy={37.5} r={4.2} fill={deep} />
          <circle cx={16} cy={37.5} r={1.4} fill="#fff" opacity={0.6} />
          <circle cx={32} cy={37.5} r={1.4} fill="#fff" opacity={0.6} />
          <Face y={22} />
        </>
      );
    case "shipper_ar": // coin
      return (
        <>
          <circle cx={24} cy={24} r={17} fill={body} />
          <circle cx={24} cy={24} r={13.6} fill="none" stroke="#fff" strokeOpacity={0.4} strokeWidth={1.5} />
          <Face y={23.5} />
        </>
      );
    case "risk": // auditor with a monocle
      return (
        <>
          <rect x={8} y={9} width={32} height={32} rx={12} fill={body} />
          <Face y={23} />
          <circle cx={29} cy={23} r={4.6} fill="#fff" fillOpacity={0.28} stroke={deep} strokeWidth={1.8} />
          <path d="M32.3 26.3 L36.2 30.2" stroke={deep} strokeWidth={2.4} strokeLinecap="round" />
        </>
      );
    case "verification": // shield
      return (
        <>
          <path d="M24 5.5 L39.5 11 V23 C39.5 32.5 33 39.6 24 42.8 C15 39.6 8.5 32.5 8.5 23 V11 Z" fill={body} />
          <Face y={21.5} />
        </>
      );
    case "controller": // approval stamp
      return (
        <>
          <circle cx={24} cy={8.5} r={4.5} fill={deep} />
          <rect x={21} y={10.5} width={6} height={6.5} rx={1.5} fill={deep} />
          <rect x={9} y={16} width={30} height={21} rx={8} fill={body} />
          <rect x={7} y={35.5} width={34} height={5.5} rx={2.75} fill={deep} />
          <Face y={25} />
        </>
      );
    case "voice_desk": // headset
      return (
        <>
          <path d="M8 25 A16 16 0 0 1 40 25" fill="none" stroke={deep} strokeWidth={3} strokeLinecap="round" />
          <circle cx={24} cy={26} r={14.5} fill={body} />
          <rect x={5} y={21} width={6.5} height={11} rx={3.25} fill={deep} />
          <rect x={36.5} y={21} width={6.5} height={11} rx={3.25} fill={deep} />
          <path d="M39.5 31 Q39 37.5 32 38" fill="none" stroke={deep} strokeWidth={1.8} strokeLinecap="round" />
          <circle cx={31} cy={38} r={2} fill={deep} />
          <Face y={25.5} />
        </>
      );
    case "cfo": // sprout
      return (
        <>
          <path d="M24 16 V9.5" stroke={deep} strokeWidth={2} strokeLinecap="round" />
          <path d="M24 10 C24 6.2 27.4 4 31.2 4.4 C31.2 8 28.2 10.6 24 10 Z" fill={deep} />
          <path d="M24 11.2 C23.6 8.2 21 6.6 17.8 7 C17.8 9.9 20.4 11.7 24 11.2 Z" fill={deep} />
          <rect x={9.5} y={15} width={29} height={26} rx={11} fill={body} />
          <Face y={27} />
        </>
      );
    case "validator": // rosette + sparkle
      return (
        <>
          {SCALLOPS.map(([cx, cy], i) => (
            <circle key={i} cx={cx} cy={cy} r={5.4} fill={body} />
          ))}
          <circle cx={24} cy={25} r={14.2} fill={body} />
          <path d="M39 3.5 Q39.7 7.3 43.5 8 Q39.7 8.7 39 12.5 Q38.3 8.7 34.5 8 Q38.3 7.3 39 3.5 Z" fill={deep} />
          <Face y={24.5} />
        </>
      );
    case "documents": // page with folded corner
      return (
        <>
          <path d="M14 6 H29 L38 15 V38 Q38 42 34 42 H14 Q10 42 10 38 V10 Q10 6 14 6 Z" fill={body} />
          <path d="M29 6 V12 Q29 15 32 15 H38 Z" fill={deep} />
          <Face y={25} />
        </>
      );
    case "all": // chat bubble
      return (
        <>
          <path d="M15.5 33 L13 41.5 L24 34 Z" fill={body} />
          <rect x={5.5} y={7.5} width={37} height={28} rx={12.5} fill={body} />
          <circle cx={40} cy={9} r={3.4} fill={deep} />
          <Face y={21.5} />
        </>
      );
    default:
      return (
        <>
          <circle cx={24} cy={24} r={16} fill={body} />
          <Face y={23.5} />
        </>
      );
  }
}

const blinkDelay = (id: string) =>
  [...id].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 50 / 10;

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
  const palette = MASCOT_COLORS[id] ?? FALLBACK;
  return (
    <svg
      className={`mascot ${working ? "working" : ""} ${className}`}
      width={size}
      height={size}
      viewBox="0 0 48 48"
      aria-hidden="true"
      focusable="false"
      style={{ "--blink-delay": `${blinkDelay(id)}s` } as CSSProperties}
    >
      {figure(id, palette)}
    </svg>
  );
}
