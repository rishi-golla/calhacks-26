"use client";
import type { CSSProperties, ReactNode } from "react";
import "./mascot.css";

type Arm = readonly [number, number, number];
type Agent = {
  color: string;
  arms?: Arm[];
  back?: ReactNode;
  prop?: ReactNode;
};

const INK = "#1d1d22";
const PAPER = "#ffffff";
const EDGE = "rgba(0,0,0,0.1)";
const OK = "#22c55e";
const DEFAULT_ARMS: Arm[] = [
  [25, 78, 20],
  [95, 78, -20],
];

function shade(hex: string, amount: number) {
  const n = parseInt(hex.slice(1), 16);
  const ch = (v: number) =>
    Math.max(0, Math.min(255, Math.round(v * (1 + amount))))
      .toString(16)
      .padStart(2, "0");
  return `#${ch(n >> 16)}${ch((n >> 8) & 255)}${ch(n & 255)}`;
}

const AGENTS: Record<string, Agent> = {
  all: {
    color: "#14b88a",
    arms: [[95, 78, -20]],
    prop: (
      <>
        <g className="m-wave-arm">
          <ellipse cx={22} cy={56} rx={7.5} ry={9.5} fill="#12a57c" />
        </g>
        <g className="m-bubble">
          <path
            d="M82 10h20a10 10 0 0 1 10 10v6a10 10 0 0 1-10 10h-9l-7 6v-6h-4a10 10 0 0 1-10-10v-6a10 10 0 0 1 10-10z"
            fill="#0d7a5c"
          />
          <circle className="m-dot m-dot1" cx={83} cy={23} r={3} fill={PAPER} />
          <circle className="m-dot m-dot2" cx={92} cy={23} r={3} fill={PAPER} />
          <circle
            className="m-dot m-dot3"
            cx={101}
            cy={23}
            r={3}
            fill={PAPER}
          />
        </g>
      </>
    ),
  },
  carrier_ap: {
    color: "#2f7bff",
    arms: [[25, 78, 20]],
    back: (
      <g
        className="m-speed"
        stroke="#9aa4b2"
        strokeWidth={3.5}
        strokeLinecap="round"
      >
        <path d="M4 60h10M2 70h12M6 80h8" />
      </g>
    ),
    prop: (
      <>
        <g className="m-hat">
          <path
            d="M37 44C37 27 47 19 60 19C73 19 83 27 83 44Z"
            fill="#1c3f9e"
          />
          <path
            d="M77 40C88 38 99 39 105 44C99 48 88 48 77 46Z"
            fill="#15306f"
          />
          <circle cx={60} cy={20} r={2.5} fill="#15306f" />
        </g>
        <g className="m-truck">
          <rect
            x={70}
            y={74}
            width={28}
            height={20}
            rx={3.5}
            fill={PAPER}
            stroke={EDGE}
            strokeWidth={1.5}
          />
          <path d="M98 80h8l7 8v6a2 2 0 0 1-2 2H98z" fill="#ff5a4f" />
          <path d="M100 82h5l4.5 5H100z" fill="#cfe4ff" />
          <circle cx={78} cy={96} r={4.5} fill={INK} />
          <circle cx={105} cy={96} r={4.5} fill={INK} />
        </g>
      </>
    ),
  },
  shipper_ar: {
    color: "#ff3b5c",
    arms: [
      [62, 84, 10],
      [97, 80, -15],
    ],
    prop: (
      <g className="m-invoice">
        <g transform="rotate(-8 80 76)">
          <rect
            x={64}
            y={54}
            width={32}
            height={42}
            rx={4}
            fill={PAPER}
            stroke={EDGE}
            strokeWidth={1.5}
          />
          <rect
            x={70}
            y={62}
            width={18}
            height={3.5}
            rx={1.75}
            fill="#d4d4d8"
          />
          <rect
            x={70}
            y={69}
            width={20}
            height={3.5}
            rx={1.75}
            fill="#d4d4d8"
          />
          <rect
            x={70}
            y={76}
            width={13}
            height={3.5}
            rx={1.75}
            fill="#d4d4d8"
          />
          <path
            className="m-check"
            d="M72 86l4 4 8-8"
            fill="none"
            stroke={OK}
            strokeWidth={4}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </g>
      </g>
    ),
  },
  risk: {
    color: "#ff4fa3",
    arms: [[25, 78, 20]],
    prop: (
      <g className="m-lens">
        <path
          d="M82 70L98 92"
          stroke="#7c4a1e"
          strokeWidth={6.5}
          strokeLinecap="round"
        />
        <circle
          cx={72}
          cy={57}
          r={13}
          fill="rgba(255,255,255,0.35)"
          stroke="#fbbf24"
          strokeWidth={5}
        />
        <ellipse cx={97} cy={90} rx={7.5} ry={9.5} fill="#e8478f" />
      </g>
    ),
  },
  verification: {
    color: "#8b5cff",
    arms: [
      [40, 84, 15],
      [80, 84, -15],
    ],
    prop: (
      <g className="m-shield">
        <path
          d="M60 66L80 72V84C80 94 71 100 60 103C49 100 40 94 40 84V72Z"
          fill={PAPER}
          stroke={EDGE}
          strokeWidth={1.5}
        />
        <path
          d="M51 85l6 6 12-12"
          fill="none"
          stroke={OK}
          strokeWidth={5}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    ),
  },
  controller: {
    color: "#6b7a90",
    arms: [[25, 78, 20]],
    back: (
      <ellipse
        className="m-ink"
        cx={92}
        cy={101}
        rx={13}
        ry={3}
        fill="#ef4444"
      />
    ),
    prop: (
      <g className="m-stamp">
        <circle cx={92} cy={56} r={6.5} fill="#a0673a" />
        <rect x={88.5} y={60} width={7} height={17} rx={2} fill="#a0673a" />
        <rect x={79} y={76} width={26} height={9} rx={2.5} fill="#ef4444" />
        <rect x={81} y={85} width={22} height={4} rx={1} fill="#b91c1c" />
        <ellipse cx={92} cy={70} rx={7.5} ry={9.5} fill="#5f6d81" />
      </g>
    ),
  },
  voice_desk: {
    color: "#ff7a1a",
    back: (
      <g
        className="m-waves"
        fill="none"
        stroke="#ff9a4d"
        strokeWidth={3.5}
        strokeLinecap="round"
      >
        <path className="m-wave1" d="M104 36q5 6 0 12" />
        <path className="m-wave2" d="M110 31q9 11 0 22" />
      </g>
    ),
    prop: (
      <g className="m-headset">
        <path
          d="M30 54C30 26 90 26 90 54"
          fill="none"
          stroke={INK}
          strokeWidth={6}
        />
        <rect x={21} y={45} width={13} height={19} rx={5.5} fill={INK} />
        <rect x={86} y={45} width={13} height={19} rx={5.5} fill={INK} />
        <path
          d="M28 63C30 74 38 78 47 77"
          fill="none"
          stroke={INK}
          strokeWidth={3.5}
          strokeLinecap="round"
        />
        <circle cx={49} cy={77} r={4} fill={INK} />
      </g>
    ),
  },
  cfo: {
    color: "#ffc21a",
    arms: [
      [25, 78, 20],
      [80, 88, -10],
    ],
    prop: (
      <>
        <g className="m-hat">
          <g transform="rotate(-8 58 30)">
            <ellipse cx={58} cy={31} rx={22} ry={4.5} fill={INK} />
            <rect x={44} y={9} width={28} height={22} rx={3} fill={INK} />
            <rect x={44} y={23} width={28} height={5} fill="#ef4444" />
          </g>
        </g>
        <g className="m-coin">
          <circle
            cx={94}
            cy={84}
            r={13}
            fill="#f59e0b"
            stroke="#d97706"
            strokeWidth={2.5}
          />
          <text
            x={94}
            y={89.5}
            textAnchor="middle"
            fontSize={16}
            fontWeight={800}
            fill={PAPER}
            fontFamily="system-ui, sans-serif"
          >
            $
          </text>
        </g>
      </>
    ),
  },
  validator: {
    color: "#06b6d4",
    arms: [
      [65, 86, 10],
      [97, 78, -15],
    ],
    prop: (
      <g className="m-clipboard">
        <rect x={64} y={56} width={32} height={43} rx={4} fill="#c08a4f" />
        <rect x={68} y={62} width={24} height={33} rx={2} fill={PAPER} />
        <rect x={74} y={52} width={12} height={7} rx={2} fill="#9ca3af" />
        {[70, 79, 88].map((y, i) => (
          <g key={y}>
            <path
              className={`m-tick m-tick${i + 1}`}
              d={`M71 ${y}l2.5 2.5 4.5-4.5`}
              fill="none"
              stroke={OK}
              strokeWidth={2.6}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <rect
              x={80}
              y={y - 1.5}
              width={9}
              height={3}
              rx={1.5}
              fill="#d4d4d8"
            />
          </g>
        ))}
      </g>
    ),
  },
  documents: {
    color: "#b07445",
    arms: [
      [58, 88, 10],
      [101, 86, -10],
    ],
    prop: (
      <g className="m-folder">
        <rect x={58} y={63} width={42} height={33} rx={4} fill="#d99a3c" />
        <g className="m-papers">
          <rect
            x={62}
            y={57}
            width={30}
            height={30}
            rx={2.5}
            fill={PAPER}
            stroke={EDGE}
            strokeWidth={1.5}
            transform="rotate(-6 77 72)"
          />
          <rect
            x={66}
            y={55}
            width={30}
            height={30}
            rx={2.5}
            fill={PAPER}
            stroke={EDGE}
            strokeWidth={1.5}
            transform="rotate(5 81 70)"
          />
        </g>
        <rect x={56} y={72} width={46} height={27} rx={4} fill="#f2bd5c" />
      </g>
    ),
  },
};

export const MASCOT_IDS = Object.keys(AGENTS);

const BODY =
  "M60 28C82 28 97 44 98 66C99 88 88 100 60 100C32 100 21 89 22 67C23 45 38 28 60 28Z";
const SHADE =
  "M22 70C24 88 36 100 60 100C86 100 97 88 98 70C92 84 78 92 60 92C42 92 28 84 22 70Z";

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
  const agent = AGENTS[id];
  const color = agent?.color ?? "#8e8e93";
  const armFill = shade(color, -0.1);
  const delay =
    ([...id].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 40) / 10;
  return (
    <svg
      className={`mascot ${working ? "working" : ""} ${className}`}
      data-mascot={agent ? id : undefined}
      width={size}
      height={size}
      viewBox="0 0 120 120"
      aria-hidden="true"
      focusable="false"
      style={{ "--mascot-delay": `${-delay}s` } as CSSProperties}
    >
      <ellipse
        className="m-shadow"
        cx={60}
        cy={110}
        rx={30}
        ry={4}
        fill={color}
      />
      {agent?.back}
      <g className="m-x">
        <g className="m-y">
          <g className="m-squash">
            <g className="m-lean">
              <path d={BODY} fill={color} />
              <path d={SHADE} fill="#000" opacity={0.1} />
              <ellipse
                cx={41}
                cy={43}
                rx={7}
                ry={4}
                fill="#fff"
                opacity={0.35}
                transform="rotate(-35 41 43)"
              />
              <g className="m-gaze">
                <g className="m-blink">
                  <path
                    className="m-eyes-open"
                    d="M49 52V62M71 52V62"
                    stroke={INK}
                    strokeWidth={7.5}
                    strokeLinecap="round"
                  />
                  <path
                    className="m-eyes-focus"
                    d="M49 56V59M71 56V59"
                    stroke={INK}
                    strokeWidth={7.5}
                    strokeLinecap="round"
                  />
                </g>
                <path
                  className="m-eyes-happy"
                  d="M44.5 59Q49 52.5 53.5 59M66.5 59Q71 52.5 75.5 59"
                  fill="none"
                  stroke={INK}
                  strokeWidth={4}
                  strokeLinecap="round"
                />
                <path
                  className="m-mouth-smile"
                  d="M56 69Q60 73 64 69"
                  fill="none"
                  stroke={INK}
                  strokeWidth={3}
                  strokeLinecap="round"
                />
                <ellipse
                  className="m-mouth-o"
                  cx={60}
                  cy={70}
                  rx={3.5}
                  ry={3}
                  fill={INK}
                />
              </g>
              {agent?.prop}
              {(agent?.arms ?? DEFAULT_ARMS).map(([cx, cy, r]) => (
                <ellipse
                  key={`${cx}-${cy}`}
                  cx={cx}
                  cy={cy}
                  rx={7.5}
                  ry={9.5}
                  fill={armFill}
                  transform={`rotate(${r} ${cx} ${cy})`}
                />
              ))}
            </g>
          </g>
        </g>
      </g>
    </svg>
  );
}
