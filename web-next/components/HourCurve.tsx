"use client";

import { useMemo } from "react";

/**
 * The signature visual: how the same journey changes across 24 hours.
 *
 * A commuter's real question is not "how long does this take" but "how much
 * does it vary". This plots the planner's own prediction for the selected
 * journey at every hour of the chosen day, so the shape of the day is visible
 * at a glance: the morning ramp, the evening peak, the quiet night.
 */
export default function HourCurve({
  values,
  currentHour,
  onPick,
  height = 132,
}: {
  values: number[]; // minutes, index = hour 0..23
  currentHour: number;
  onPick?: (hour: number) => void;
  height?: number;
}) {
  const { path, area, min, max, peakHour, quietHour } = useMemo(() => {
    const w = 240;
    const h = height;
    const pad = 10;
    if (!values.length) {
      return { path: "", area: "", min: 0, max: 0, peakHour: 0, quietHour: 0 };
    }
    const valid = values.filter((v) => v > 0);
    const min = valid.length ? Math.min(...valid) : 0;
    const max = valid.length ? Math.max(...valid) : 1;
    const span = Math.max(1, max - min);
    const pts = values.map((v, i) => {
      const x = pad + (i / 23) * (w - pad * 2);
      const y = h - pad - ((v - min) / span) * (h - pad * 2);
      return [x, y] as const;
    });
    const path = pts
      .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
      .join(" ");
    const area = `${path} L${pts[pts.length - 1][0].toFixed(1)},${h - pad} L${pts[0][0].toFixed(1)},${h - pad} Z`;
    let peakHour = 0;
    let quietHour = 0;
    values.forEach((v, i) => {
      if (v > values[peakHour]) peakHour = i;
      if (v > 0 && (values[quietHour] === 0 || v < values[quietHour])) quietHour = i;
    });
    return { path, area, min, max, peakHour, quietHour };
  }, [values, height]);

  const w = 240;
  const h = height;
  const pad = 10;
  const xFor = (hour: number) => pad + (hour / 23) * (w - pad * 2);
  const span = Math.max(1, max - min);
  const yFor = (v: number) => h - pad - ((v - min) / span) * (h - pad * 2);

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`Journey time by hour. Slowest at ${peakHour}:00 with ${Math.round(max)} minutes, fastest at ${quietHour}:00 with ${Math.round(min)} minutes.`}
      >
        <defs>
          <linearGradient id="curve-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#7aa8ff" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#7aa8ff" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Hour gridlines at 0, 6, 12, 18, 23 */}
        {[0, 6, 12, 18, 23].map((hr) => (
          <line
            key={hr}
            x1={xFor(hr)}
            y1={pad}
            x2={xFor(hr)}
            y2={h - pad}
            stroke="#1a1e27"
            strokeWidth="1"
          />
        ))}

        <path d={area} fill="url(#curve-fill)" />
        <path
          d={path}
          fill="none"
          stroke="#7aa8ff"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {/* Current hour marker */}
        <line
          x1={xFor(currentHour)}
          y1={pad - 2}
          x2={xFor(currentHour)}
          y2={h - pad}
          stroke="#eef1f6"
          strokeWidth="1"
          strokeDasharray="2 3"
          opacity="0.5"
        />
        <circle
          cx={xFor(currentHour)}
          cy={yFor(values[currentHour] || min)}
          r="3.2"
          fill="#eef1f6"
          stroke="#06070a"
          strokeWidth="1.5"
        />

        {/* Peak annotation */}
        <circle cx={xFor(peakHour)} cy={yFor(max)} r="2.4" fill="#ff6b81" />
      </svg>

      <div className="mt-1 flex justify-between px-1">
        {[0, 6, 12, 18, 23].map((hr) => (
          <button
            key={hr}
            type="button"
            onClick={() => onPick?.(hr)}
            className="mono text-[9px] text-text-3 transition-colors hover:text-text-2"
            aria-label={`Set departure to ${hr}:00`}
          >
            {String(hr).padStart(2, "0")}
          </button>
        ))}
      </div>
    </div>
  );
}
