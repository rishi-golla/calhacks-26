const W = 1400;
const H = 460;
const COLS = 110;
const ROWS = 34;

const DOTS = (() => {
  const out: { x: number; y: number; r: number; o: number }[] = [];
  for (let j = 0; j < ROWS; j++) {
    const v = j / (ROWS - 1);
    for (let i = 0; i < COLS; i++) {
      const u = i / (COLS - 1);
      const swell = Math.sin(u * Math.PI * 2.2 + v * 2.6) * 70 + Math.sin(u * Math.PI * 5 - v * 4) * 18;
      const x = W / 2 + (u - 0.5) * W * (0.75 + 0.5 * v);
      const y = 90 + v * v * 300 + swell * (0.35 + 0.65 * v);
      const edge = Math.sin(u * Math.PI);
      const crest = 0.55 + 0.45 * Math.sin(u * Math.PI * 2.2 + v * 2.6);
      out.push({
        x: Math.round(x * 10) / 10,
        y: Math.round(y * 10) / 10,
        r: Math.round((0.5 + 1.3 * v) * 100) / 100,
        o: Math.round(Math.max(0.04, (0.2 + 0.8 * v) * edge * crest) * 100) / 100,
      });
    }
  }
  return out;
})();

/** Static dotted-wave backdrop for the page header. */
export function DotWave() {
  return (
    <div className="fm-wave" aria-hidden>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice">
        {DOTS.map((d, k) => (
          <circle key={k} cx={d.x} cy={d.y} r={d.r} fill="#fff" fillOpacity={d.o} />
        ))}
      </svg>
    </div>
  );
}
