"use client";

import { useMemo } from "react";
import type { Band } from "@/lib/planner";

const COLOR: Record<Band, string> = {
  seats_available: "#4ade9b",
  standing_room: "#f5c451",
  crowded: "#ff6b81",
  unknown: "#5c6577",
};

const LABEL: Record<Band, string> = {
  seats_available: "Seats",
  standing_room: "Standing",
  crowded: "Crowded",
  unknown: "Unknown",
};

/**
 * Crowding along a route, drawn as a vertical strip of stretches.
 *
 * Each row is one stop-to-stop stretch in route order, so the shape of the
 * route is readable top to bottom: where the bus fills up, where it empties.
 * The bar length is the relative load index, which is comparable across
 * stretches of the same route.
 */
export default function CrowdStrip({
  rows,
  maxLoad,
}: {
  rows: { station: string; band: Band; load: number; conf: string }[];
  maxLoad: number;
}) {
  const peak = useMemo(
    () => rows.reduce((best, r, i) => (r.load > (rows[best]?.load ?? 0) ? i : best), 0),
    [rows]
  );
  if (!rows.length) {
    return (
      <p className="py-6 text-[12px] leading-relaxed text-text-3">
        No ticketing evidence for this route at this hour. Coverage is 63% of
        routes, so this is expected for quieter services.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {rows.map((r, i) => {
        const pct = Math.max(4, (r.load / Math.max(1, maxLoad)) * 100);
        const isPeak = i === peak;
        return (
          <div key={`${r.station}-${i}`} className="group">
            <div className="flex items-baseline justify-between gap-3">
              <span
                className={`truncate text-[12px] leading-5 transition-colors ${
                  isPeak ? "text-text" : "text-text-2 group-hover:text-text"
                }`}
              >
                {r.station}
              </span>
              <span className="mono shrink-0 text-[10px] text-text-3">
                {r.load}
                <span className="ml-1.5 uppercase tracking-wider" style={{ color: COLOR[r.band] }}>
                  {LABEL[r.band]}
                </span>
              </span>
            </div>
            <div className="mt-1 h-[5px] w-full overflow-hidden rounded-full bg-ink-3">
              <div
                className="h-full rounded-full transition-[width] duration-500"
                style={{
                  width: `${pct}%`,
                  background: COLOR[r.band],
                  opacity: r.conf === "low" ? 0.6 : 1,
                }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
