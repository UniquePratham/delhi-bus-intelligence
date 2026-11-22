"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { AnimatePresence, motion } from "framer-motion";
import { Planner, type Journey, type Band } from "@/lib/planner";
import { setStationCoords, setRoutePath, setPairGeometry } from "@/components/Map3D";
import HourCurve from "@/components/HourCurve";
import CrowdStrip from "@/components/CrowdStrip";
import ThemeToggle from "@/components/ThemeToggle";

const Map3D = dynamic(() => import("@/components/Map3D"), { ssr: false });

type Pref = "fastest" | "balanced" | "less_crowded";

const BAND_LABEL: Record<Band, string> = {
  seats_available: "Seats available",
  standing_room: "Standing room",
  crowded: "Crowded",
  unknown: "Unknown",
};

const BAND_COLOR: Record<Band, string> = {
  seats_available: "var(--seats)",
  standing_room: "var(--standing)",
  crowded: "var(--crowded)",
  unknown: "var(--unknown)",
};

const clock = (ms: number) =>
  new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(new Date(ms));

const mins = (s: number) => Math.round(s / 60);

function rank(j: Journey, pref: Pref): number {
  const m = j.durationS / 60;
  if (pref === "fastest") return m;
  const order: Record<Band, number> = {
    crowded: 1,
    standing_room: 0.5,
    seats_available: 0,
    unknown: 0.35,
  };
  const bus = j.legs.filter((l) => l.kind === "bus" && l.crowding);
  if (!bus.length) return m;
  const penalty = Math.max(...bus.map((l) => order[l.crowding!.band] ?? 0.35));
  return pref === "less_crowded" ? m + 30 * penalty : m + 0.35 * penalty * 30;
}

export default function Page() {
  const [planner, setPlanner] = useState<Planner | null>(null);
  const [loadPct, setLoadPct] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [from, setFrom] = useState("Kashmere Gate ISBT");
  const [to, setTo] = useState("Nehru Place");
  const [date, setDate] = useState("2026-10-15");
  const [hour, setHour] = useState(8);
  const [minute, setMinute] = useState(30);
  const [transfers, setTransfers] = useState(2);
  const [pref, setPref] = useState<Pref>("balanced");

  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [pick, setPick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [origin, setOrigin] = useState<{ lat: number; lon: number } | null>(null);
  const [destination, setDestination] = useState<{ lat: number; lon: number } | null>(null);
  const [ms, setMs] = useState<number | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [mapMode, setMapMode] = useState<"2d" | "3d">("3d");

  useEffect(() => {
    let dead = false;
    const t = setInterval(() => setLoadPct((p) => Math.min(90, p + 6)), 130);
    Planner.load()
      .then((p) => {
        if (dead) return;
        setStationCoords((i) => {
          const [lat, lon] = p.latLon(i);
          return [lon, lat];
        });
        setRoutePath((routeId, from, to) => p.routeStations(routeId, from, to));
        setPairGeometry((from, to) => p.pairGeometry(from, to));
        setPlanner(p);
        setLoadPct(100);
      })
      .catch((e) => !dead && setLoadError(String(e)))
      .finally(() => clearInterval(t));
    return () => {
      dead = true;
      clearInterval(t);
    };
  }, []);

  const run = useCallback(
    (h = hour, m = minute) => {
      if (!planner) return;
      setBusy(true);
      setError(null);
      requestAnimationFrame(() => {
        try {
          const o = planner.search(from, 1)[0];
          const d = planner.search(to, 1)[0];
          if (!o || !d) {
            setError(`No station matches ${!o ? `“${from}”` : `“${to}”`}. Try a landmark nearby.`);
            setJourneys([]);
            return;
          }
          const [oLat, oLon] = planner.latLon(o.idx);
          const [dLat, dLon] = planner.latLon(d.idx);
          setOrigin({ lat: oLat, lon: oLon });
          setDestination({ lat: dLat, lon: dLon });

          const depart = new Date(`${date}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00+05:30`);
          const t0 = performance.now();
          const found = planner.plan({ lat: oLat, lon: oLon }, { lat: dLat, lon: dLon }, depart, transfers, 4);
          setMs(performance.now() - t0);
          const ranked = [...found].sort((a, b) => rank(a, pref) - rank(b, pref));
          setJourneys(ranked);
          setPick(0);
          if (!ranked.length) setError("No bus journey found between these points.");
        } catch (e) {
          setError(String(e));
        } finally {
          setBusy(false);
        }
      });
    },
    [planner, from, to, date, hour, minute, transfers, pref]
  );

  useEffect(() => {
    if (planner) run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planner]);

  const current = journeys[pick] ?? null;

  // The 24-hour curve for the selected journey's route sequence.
  const curve = useMemo(() => {
    if (!planner || !current) return [] as number[];
    const o = planner.search(from, 1)[0];
    const d = planner.search(to, 1)[0];
    if (!o || !d) return [];
    const [oLat, oLon] = planner.latLon(o.idx);
    const [dLat, dLon] = planner.latLon(d.idx);
    const out: number[] = [];
    for (let h = 0; h < 24; h++) {
      const dep = new Date(`${date}T${String(h).padStart(2, "0")}:00:00+05:30`);
      const js = planner.plan({ lat: oLat, lon: oLon }, { lat: dLat, lon: dLon }, dep, transfers, 1);
      out.push(js.length ? js[0].durationS / 60 : 0);
    }
    return out;
  }, [planner, current, from, to, date, transfers]);
  const peak = curve.length ? Math.max(...curve) : 0;
  const quiet = curve.filter((v) => v > 0).length ? Math.min(...curve.filter((v) => v > 0)) : 0;

  return (
    <main className="relative min-h-screen">
      {/* ---------- Hero ---------- */}
      <section className="relative overflow-hidden border-b border-line-soft">
        <div className="pointer-events-none absolute inset-0">
          <div className="absolute left-1/2 top-0 h-[420px] w-[820px] -translate-x-1/2 rounded-full bg-accent/[0.07] blur-[140px]" />
        </div>

        <div className="relative mx-auto max-w-[1440px] px-6 pb-10 pt-8 lg:px-10">
          <nav className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-breathe rounded-full bg-accent" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
              </span>
              <span className="eyebrow">Transport Stack Hackathon 2026</span>
            </div>
            <div className="flex items-center gap-3">
              <div className="hidden items-center gap-6 sm:flex">
                <span className="eyebrow">Delhi · October 2026</span>
                <span className="eyebrow">Not a live feed</span>
              </div>
              <ThemeToggle />
            </div>
          </nav>

          <div className="mt-14 grid gap-10 lg:grid-cols-[1.15fr_0.85fr] lg:items-end">
            <div>
              <h1 className="display text-balance text-[clamp(2.6rem,6vw,5.2rem)] text-text">
                The bus fleet
                <br />
                is a traffic sensor.
              </h1>
              <p className="mt-6 max-w-xl text-pretty text-[15px] leading-relaxed text-text-2">
                Every Delhi bus reports where it is, every few seconds. Put thousands
                together and you know how fast the city actually moves. This planner
                turns that signal into travel times you can rely on — and adds how
                full the bus is likely to be.
              </p>
            </div>

            <dl className="grid grid-cols-2 gap-x-8 gap-y-6 border-l border-line-soft pl-8">
              {[
                ["Segment error", "22.4 s", "vs 41.6 s timetable"],
                ["Journey error", "0.93 min", "on 400 real trips"],
                ["Better than timetable", "60%", "lower error"],
                ["Crowding coverage", "63%", "of routes"],
              ].map(([k, v, s]) => (
                <div key={k}>
                  <dt className="eyebrow">{k}</dt>
                  <dd className="mono mt-1.5 text-2xl text-text">{v}</dd>
                  <dd className="mt-0.5 text-[11px] text-text-3">{s}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      </section>

      {loadError && (
        <div className="mx-auto max-w-[1440px] px-6 py-6 lg:px-10">
          <p className="border-l-2 border-crowded pl-4 text-sm text-crowded">
            Could not load the model: {loadError}
          </p>
        </div>
      )}

      {!planner && !loadError && (
        <div className="mx-auto max-w-[1440px] px-6 py-24 lg:px-10">
          <div className="flex items-baseline justify-between">
            <span className="eyebrow">Loading the network model</span>
            <span className="mono text-[11px] text-text-3">{loadPct}%</span>
          </div>
          <div className="mt-4 h-px w-full bg-line-soft">
            <div
              className="h-px bg-accent transition-[width] duration-200"
              style={{ width: `${loadPct}%` }}
            />
          </div>
          <p className="mt-6 max-w-md text-[12px] leading-relaxed text-text-3">
            3,688 stations, 2,403 routes, 803,764 travel-time profiles and 460,415
            crowding cells load once, then every query runs in your browser.
          </p>
        </div>
      )}

      {planner && (
        <>
          {/* ---------- Planner + map ---------- */}
          <section className="relative">
            <div className="grid lg:grid-cols-[420px_1fr]">
              {/* Left rail */}
              <div className="relative z-10 border-r border-line-soft bg-ink px-6 py-8 lg:px-8">
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    run();
                  }}
                >
                  <div className="space-y-7">
                    <Field
                      id="from"
                      label="From"
                      value={from}
                      onChange={setFrom}
                      suggestions={planner.search(from, 3).map((s) => s.name)}
                    />
                    <Field
                      id="to"
                      label="To"
                      value={to}
                      onChange={setTo}
                      suggestions={planner.search(to, 3).map((s) => s.name)}
                    />

                    <div className="grid grid-cols-2 gap-6">
                      <div>
                        <label htmlFor="date" className="eyebrow">
                          Date
                        </label>
                        <input
                          id="date"
                          type="date"
                          value={date}
                          onChange={(e) => setDate(e.target.value)}
                          className="field mono mt-1 text-[13px]"
                        />
                      </div>
                      <div>
                        <span className="eyebrow">Depart</span>
                        <div className="mt-1 flex items-baseline gap-1">
                          <input
                            aria-label="Departure hour"
                            type="number"
                            min={0}
                            max={23}
                            value={hour}
                            onChange={(e) => setHour(Math.max(0, Math.min(23, +e.target.value)))}
                            className="field mono w-12 text-center text-[13px]"
                          />
                          <span className="mono text-text-3">:</span>
                          <input
                            aria-label="Departure minute"
                            type="number"
                            min={0}
                            max={59}
                            step={5}
                            value={minute}
                            onChange={(e) => setMinute(Math.max(0, Math.min(59, +e.target.value)))}
                            className="field mono w-12 text-center text-[13px]"
                          />
                        </div>
                      </div>
                    </div>

                    <div>
                      <span className="eyebrow">Changes allowed</span>
                      <div className="mt-2 flex gap-1.5">
                        {[0, 1, 2, 3].map((n) => (
                          <button
                            key={n}
                            type="button"
                            onClick={() => setTransfers(n)}
                            aria-pressed={transfers === n}
                            className={`mono h-8 w-8 border text-[11px] transition-colors ${
                              transfers === n
                                ? "border-text bg-text text-ink"
                                : "border-line text-text-3 hover:border-text-3 hover:text-text-2"
                            }`}
                          >
                            {n}
                          </button>
                        ))}
                      </div>
                    </div>

                    <div>
                      <span className="eyebrow">Rank by</span>
                      <div className="mt-2 grid grid-cols-3 gap-1.5">
                        {(
                          [
                            ["fastest", "Fastest"],
                            ["balanced", "Balanced"],
                            ["less_crowded", "Calmer"],
                          ] as [Pref, string][]
                        ).map(([k, l]) => (
                          <button
                            key={k}
                            type="button"
                            onClick={() => setPref(k)}
                            aria-pressed={pref === k}
                            className={`border px-2 py-2 text-[11px] transition-colors ${
                              pref === k
                                ? "border-text bg-text text-ink"
                                : "border-line text-text-3 hover:border-text-3 hover:text-text-2"
                            }`}
                          >
                            {l}
                          </button>
                        ))}
                      </div>
                    </div>

                    <button type="submit" className="btn btn-solid w-full" disabled={busy}>
                      {busy ? "Planning…" : "Plan journey"}
                    </button>
                  </div>
                </form>

                {error && (
                  <p className="mt-6 border-l-2 border-crowded pl-3 text-[12px] leading-relaxed text-crowded">
                    {error}
                  </p>
                )}

                {/* Options */}
                <div className="mt-10">
                  <div className="flex items-baseline justify-between">
                    <span className="eyebrow">
                      {journeys.length === 1 ? "1 option" : `${journeys.length} options`}
                    </span>
                    {ms !== null && <span className="mono text-[10px] text-text-3">{ms.toFixed(0)} ms</span>}
                  </div>

                  <div className="mt-3 space-y-px">
                    <AnimatePresence initial={false}>
                      {journeys.map((j, i) => (
                        <motion.button
                          key={`${i}-${Math.round(j.durationS)}`}
                          layout
                          initial={{ opacity: 0, y: 6 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0 }}
                          transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
                          type="button"
                          onClick={() => setPick(i)}
                          data-journey-card
                          aria-pressed={pick === i}
                          className={`group w-full border-l-2 py-3 pl-3 pr-2 text-left transition-colors ${
                            pick === i
                              ? "border-accent bg-ink-2"
                              : "border-transparent hover:border-line hover:bg-ink-1"
                          }`}
                        >
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="mono text-[19px] leading-none text-text">
                              {mins(j.durationS)}
                              <span className="ml-0.5 text-[11px] text-text-3">min</span>
                            </span>
                            <span className="mono text-[10px] text-text-3">
                              {clock(j.legs[0].depart)}–{clock(j.legs[j.legs.length - 1].arrive)}
                            </span>
                          </div>
                          <div className="mt-1.5 flex items-center gap-2 text-[11px] text-text-3">
                            <span
                              className="dot"
                              style={{ background: BAND_COLOR[j.crowdingSummary.band] }}
                              aria-hidden
                            />
                            <span>{BAND_LABEL[j.crowdingSummary.band]}</span>
                            <span aria-hidden>·</span>
                            <span>
                              {j.transfers} change{j.transfers === 1 ? "" : "s"}
                            </span>
                            <span aria-hidden>·</span>
                            <span>{mins(j.walkS)} min walk</span>
                          </div>
                        </motion.button>
                      ))}
                    </AnimatePresence>
                  </div>
                </div>
              </div>

              {/* Map */}
              <div className="relative h-[clamp(460px,72vh,860px)] bg-ink-1">
                <Map3D
                  journey={current}
                  origin={origin}
                  destination={destination}
                  onLoaded={() => setMapReady(true)}
                  mode={mapMode}
                />

                {/* Map chrome */}
                <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between p-5">
                  <div className="pointer-events-auto glass flex items-center gap-1 p-1">
                    {(["2d", "3d"] as const).map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => setMapMode(m)}
                        aria-pressed={mapMode === m}
                        className={`mono px-2.5 py-1 text-[10px] uppercase tracking-wider transition-colors ${
                          mapMode === m
                            ? "bg-text text-ink"
                            : "text-text-3 hover:text-text-2"
                        }`}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                  <div className="pointer-events-auto glass px-3 py-2">
                    <span className="eyebrow">
                      {mapReady ? "Live model" : "Loading tiles"}
                    </span>
                  </div>
                </div>

                <div className="pointer-events-none absolute bottom-5 left-5 glass px-3 py-2.5">
                  <div className="eyebrow mb-2">Crowding</div>
                  <div className="space-y-1">
                    {(["seats_available", "standing_room", "crowded", "unknown"] as Band[]).map((b) => (
                      <div key={b} className="flex items-center gap-2">
                        <span className="dot" style={{ background: BAND_COLOR[b] }} aria-hidden />
                        <span className="text-[10px] text-text-2">{BAND_LABEL[b]}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Leg detail overlay */}
                <AnimatePresence>
                  {current && (
                    <motion.div
                      key={pick}
                      initial={{ opacity: 0, y: 12 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
                      className="pointer-events-auto absolute bottom-5 right-5 w-[min(340px,calc(100%-2.5rem))] glass p-4"
                    >
                      <div className="flex items-baseline justify-between">
                        <span className="eyebrow">This journey</span>
                        <span className="mono text-[10px] text-text-3">
                          {current.crowdingSummary.legsKnown}/{current.crowdingSummary.legsTotal} known
                        </span>
                      </div>
                      <ol className="mt-3 space-y-2">
                        {current.legs.map((leg, i) => (
                          <li key={i} className="grid grid-cols-[14px_1fr_auto] items-baseline gap-2">
                            <span
                              className="dot mt-1"
                              style={{
                                background:
                                  leg.kind === "walk"
                                    ? "var(--text-3)"
                                    : BAND_COLOR[leg.crowding?.band ?? "unknown"],
                              }}
                              aria-hidden
                            />
                            <span className="min-w-0 text-[11px] leading-snug text-text-2">
                              <span className="block truncate">
                                {leg.fromName} → {leg.toName}
                              </span>
                              {leg.routeName && (
                                <span className="mono text-[10px] text-accent">{leg.routeName}</span>
                              )}
                            </span>
                            <span className="mono text-[10px] text-text-3">
                              {mins((leg.arrive - leg.depart) / 1000)}
                            </span>
                          </li>
                        ))}
                      </ol>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            </div>
          </section>

          {/* ---------- Signature visual: the day ---------- */}
          <section className="border-t border-line-soft">
            <div className="mx-auto max-w-[1440px] px-6 py-16 lg:px-10">
              <div className="grid gap-12 lg:grid-cols-[1fr_1.1fr] lg:gap-20">
                <div>
                  <span className="eyebrow">The same trip, every hour</span>
                  <h2 className="display mt-4 text-balance text-[clamp(1.9rem,3.4vw,3rem)] text-text">
                    A trip is not one number.
                  </h2>
                  <p className="mt-5 max-w-md text-pretty text-[14px] leading-relaxed text-text-2">
                    The published timetable assumes a fixed speed. Real travel time
                    moves with the hour, the day and the road. This is the planner&rsquo;s
                    own estimate for your journey at each hour of {date}.
                  </p>

                  <div className="mt-8 grid grid-cols-3 gap-6 border-t border-line-soft pt-6">
                    <div>
                      <div className="eyebrow">Slowest</div>
                      <div className="mono mt-1 text-xl text-crowded">{Math.round(peak)} min</div>
                    </div>
                    <div>
                      <div className="eyebrow">Quietest</div>
                      <div className="mono mt-1 text-xl text-seats">{Math.round(quiet)} min</div>
                    </div>
                    <div>
                      <div className="eyebrow">Spread</div>
                      <div className="mono mt-1 text-xl text-text">
                        {Math.round(peak - quiet)} min
                      </div>
                    </div>
                  </div>
                </div>

                <div className="relative">
                  <HourCurve
                    values={curve}
                    currentHour={hour}
                    height={200}
                    onPick={(h) => {
                      setHour(h);
                      run(h, minute);
                    }}
                  />
                  <div className="mt-4 flex items-center justify-between border-t border-line-soft pt-3">
                    <span className="eyebrow">Hour of day (IST)</span>
                    <span className="mono text-[10px] text-text-3">
                      {Math.round(quiet)}–{Math.round(peak)} min
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </section>

          {/* ---------- Signature visual: crowding along the route ---------- */}
          <CrowdingSection planner={planner} hour={hour} />

          {/* ---------- Method ---------- */}
          <Method meta={planner.meta} />
        </>
      )}

      <footer className="border-t border-line-soft">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-3 px-6 py-8 text-[11px] text-text-3 sm:flex-row sm:items-center sm:justify-between lg:px-10">
          <p>
            Built for Transport Stack Hackathon 2026. Data: DTC bus GPS and ONDC
            e-ticketing, October 2026.
          </p>
          <p>Historical estimates, not a live feed. Crowding is a relative index.</p>
        </div>
      </footer>
    </main>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  suggestions,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  suggestions: string[];
}) {
  const show = suggestions.length > 0 && suggestions[0].toLowerCase() !== value.toLowerCase();
  return (
    <div>
      <label htmlFor={id} className="eyebrow">
        {label}
      </label>
      <input
        id={id}
        name={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        placeholder="Station or landmark…"
        className="field mt-1"
      />
      {show && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {suggestions.slice(0, 2).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onChange(s)}
              className="border border-line px-2 py-1 text-[10px] text-text-3 transition-colors hover:border-text-3 hover:text-text-2"
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CrowdingSection({ planner, hour }: { planner: Planner; hour: number }) {
  const [route, setRoute] = useState("241DOWN");
  const [h, setH] = useState(hour);

  useEffect(() => setH(hour), [hour]);

  const rows = useMemo(() => {
    const ridx = planner.routes.names.indexOf(route);
    if (ridx < 0) return [];
    const out: { station: string; band: Band; load: number; conf: string }[] = [];
    const start = planner["patOff"][ridx];
    const end = planner["patOff"][ridx + 1];
    for (let i = start; i < end; i++) {
      const sid = planner["patSta"][i];
      const ci = planner["crowdIndex"].get(ridx * 100000 + h * 4096 + sid);
      if (ci === undefined) continue;
      out.push({
        station: planner.stations.names[sid],
        band: (["seats_available", "standing_room", "crowded"] as Band[])[planner["crowdBand"][ci]] ?? "unknown",
        load: planner["crowdLoad"][ci],
        conf: (["low", "medium", "high"] as const)[planner["crowdConf"][ci]] ?? "low",
      });
    }
    return out;
  }, [planner, route, h]);

  const maxLoad = Math.max(1, ...rows.map((r) => r.load));

  return (
    <section className="border-t border-line-soft">
      <div className="mx-auto max-w-[1440px] px-6 py-16 lg:px-10">
        <div className="grid gap-12 lg:grid-cols-[1fr_1.1fr] lg:gap-20">
          <div>
            <span className="eyebrow">Crowding, stretch by stretch</span>
            <h2 className="display mt-4 text-balance text-[clamp(1.9rem,3.4vw,3rem)] text-text">
              Where the bus fills up.
            </h2>
            <p className="mt-5 max-w-md text-pretty text-[14px] leading-relaxed text-text-2">
              Delhi records every bus ticket it sells. Grouped by route and hour, those
              records show how full a bus is along its run — where it fills, and where
              it empties. This is a relative index from a sample, not an occupancy
              percentage.
            </p>

            <div className="mt-8 flex flex-wrap items-end gap-6">
              <div>
                <label htmlFor="route" className="eyebrow">
                  Route
                </label>
                <input
                  id="route"
                  value={route}
                  onChange={(e) => setRoute(e.target.value.toUpperCase())}
                  spellCheck={false}
                  className="field mono mt-1 w-28 text-[13px]"
                />
              </div>
              <div>
                <label htmlFor="crowd-hour" className="eyebrow">
                  Hour
                </label>
                <input
                  id="crowd-hour"
                  type="number"
                  min={0}
                  max={23}
                  value={h}
                  onChange={(e) => setH(Math.max(0, Math.min(23, +e.target.value)))}
                  className="field mono mt-1 w-16 text-center text-[13px]"
                />
              </div>
              <div className="pb-2">
                <span className="mono text-[10px] text-text-3">
                  {rows.length} stretches with evidence
                </span>
              </div>
            </div>
          </div>

          <div className="max-h-[460px] overflow-y-auto pr-2">
            <CrowdStrip rows={rows} maxLoad={maxLoad} />
          </div>
        </div>
      </div>
    </section>
  );
}

function Method({ meta }: { meta: any }) {
  const ev = meta?.evaluation;
  const t = ev?.traffic?.baselines;
  const j = ev?.journeys;
  const c = ev?.crowding?.coverage;
  const temporal = ev?.temporal;

  return (
    <section className="border-t border-line-soft">
      <div className="mx-auto max-w-[1440px] px-6 py-16 lg:px-10">
        <span className="eyebrow">Method &amp; evidence</span>
        <h2 className="display mt-4 max-w-2xl text-balance text-[clamp(1.9rem,3.4vw,3rem)] text-text">
          Measured, not claimed.
        </h2>

        <div className="mt-12 grid gap-x-10 gap-y-10 sm:grid-cols-2 lg:grid-cols-4">
          {[
            {
              k: "Segment error",
              v: t ? `${t.model.mae_s.toFixed(1)} s` : "—",
              d: t ? `Timetable: ${t.schedule.mae_s.toFixed(1)} s on 1.24 M runs` : "",
            },
            {
              k: "Journey error",
              v: j ? `${j.mae_min.toFixed(2)} min` : "—",
              d: j ? `${j.within_10_min_pct}% within 10 min, on 400 real trips` : "",
            },
            {
              k: "Better than timetable",
              v: j ? `${j.improvement_vs_schedule_pct}%` : "—",
              d: "Lower error on the same paths",
            },
            {
              k: "Crowding coverage",
              v: c ? `${c.route_coverage_pct}%` : "—",
              d: c ? `${c.routes_with_crowding} of ${c.routes_total} routes` : "",
            },
          ].map((s) => (
            <div key={s.k} className="border-t border-line pt-4">
              <div className="eyebrow">{s.k}</div>
              <div className="mono mt-2 text-2xl text-text">{s.v}</div>
              <div className="mt-1 text-[11px] leading-relaxed text-text-3">{s.d}</div>
            </div>
          ))}
        </div>

        {temporal?.comparisons && (
          <div className="mt-14 grid gap-10 lg:grid-cols-[1fr_1.1fr] lg:gap-20">
            <div>
              <h3 className="text-[15px] font-medium text-text">The model knows the day</h3>
              <p className="mt-3 max-w-md text-[13px] leading-relaxed text-text-2">
                If the time dimension were noise, these comparisons would sit near 50%.
                They do not.
              </p>
            </div>
            <div className="space-y-4">
              {temporal.comparisons.map((c: any) => (
                <div key={c.comparison}>
                  <div className="flex items-baseline justify-between">
                    <span className="text-[12px] text-text-2">{c.comparison}</span>
                    <span className="mono text-[11px] text-text">{c.first_slower_pct}%</span>
                  </div>
                  <div className="mt-1.5 h-[3px] w-full overflow-hidden rounded-full bg-ink-3">
                    <div
                      className="h-full rounded-full bg-accent"
                      style={{ width: `${c.first_slower_pct}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-14 grid gap-8 border-t border-line-soft pt-8 sm:grid-cols-3">
          {[
            [
              "Route identity is never read from GPS",
              "The GPS feed's route_id shares no ID space with GTFS — 0% semantic agreement over 36,719 pairs. Journeys are linked geometrically, by stop passage.",
            ],
            [
              "Stops are clustered into stations first",
              "GTFS assigns a separate stop_id per platform. 10,559 stop_ids collapse to 3,688 stations. Without this, measured run times were 2.4× too short.",
            ],
            [
              "Passages are timed at closest approach",
              "First contact with a snap radius truncates ~240 m from every run and inflates implied speed from 20 to 38 km/h.",
            ],
          ].map(([h, p]) => (
            <div key={h}>
              <h4 className="text-[13px] font-medium leading-snug text-text">{h}</h4>
              <p className="mt-2 text-[12px] leading-relaxed text-text-3">{p}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
