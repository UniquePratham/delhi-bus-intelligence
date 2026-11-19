/**
 * Client-side journey planner.
 *
 * The whole model is precomputed into binary blobs (see scripts/export_web.py)
 * and loaded into typed arrays here. Planning then runs in the browser, so the
 * deployed site is fully static and every query is instant.
 *
 * The search is the same round-based label-setting algorithm as the Python
 * implementation: one round is one route boarding, labels are kept per round,
 * and a label records the boarding station and route so a ride can be
 * reconstructed as a single leg.
 */

export type Band = "seats_available" | "standing_room" | "crowded" | "unknown";

export interface CrowdingInfo {
  band: Band;
  confidence: "none" | "low" | "medium" | "high";
  loadIndex: number;
  scope: "hour" | "all_hours" | "route_hour" | "route" | "none";
  reason?: string;
}

export interface Leg {
  kind: "walk" | "bus";
  fromName: string;
  toName: string;
  fromStation: number | null;
  toStation: number | null;
  depart: number; // epoch ms
  arrive: number;
  routeId: number | null;
  routeName: string | null;
  distanceM: number | null;
  confidence: "measured" | "high" | "medium" | "low";
  crowding: CrowdingInfo | null;
}

export interface Journey {
  legs: Leg[];
  durationS: number;
  transfers: number;
  walkS: number;
  busS: number;
  crowdingSummary: {
    band: Band;
    confidence: string;
    legsKnown: number;
    legsTotal: number;
  };
}

const HOUR_BANDS = [
  [0, 5, "night"],
  [6, 7, "early_peak"],
  [8, 11, "morning_peak"],
  [12, 16, "midday"],
  [17, 20, "evening_peak"],
  [21, 23, "late"],
] as const;

const BAND_NAMES = ["night", "early_peak", "morning_peak", "midday", "evening_peak", "late"];
const BAND_CODE: Band[] = ["seats_available", "standing_room", "crowded"];
const CONF_CODE = ["low", "medium", "high"] as const;

const WALK_SPEED = 1.25; // m/s
const MAX_WALK_M = 1200;
const EARTH_R = 6371000;

function haversine(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = (lat1 * Math.PI) / 180;
  const p2 = (lat2 * Math.PI) / 180;
  const dphi = p2 - p1;
  const dlam = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dphi / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dlam / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.sqrt(a));
}

function bandIndex(hour: number): number {
  for (let i = 0; i < HOUR_BANDS.length; i++) {
    const [lo, hi] = HOUR_BANDS[i];
    if (hour >= lo && hour <= hi) return i;
  }
  return 5;
}

async function fetchBin(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`failed to load ${url}: ${res.status}`);
  return res.arrayBuffer();
}

export class Planner {
  stations!: { ids: number[]; names: string[] };
  routes!: { ids: number[]; names: string[] };
  meta!: any;

  private coords!: Float32Array;
  private patOff!: Uint32Array;
  private patSta!: Uint16Array;
  private edgeOff!: Uint32Array;
  private edgeTo!: Uint16Array;
  private edgeRoute!: Uint16Array;
  private pairFrom!: Uint16Array;
  private pairTo!: Uint16Array;
  private hourOff!: Uint32Array;
  private hourHow!: Uint8Array;
  private hourSec!: Uint16Array;
  private bandVals!: Uint16Array;
  private baseVals!: Uint16Array;
  private schedVals!: Uint16Array;

  private crowdIndex!: Map<number, number>;
  private crowdBand!: Uint8Array;
  private crowdConf!: Uint8Array;
  private crowdLoad!: Uint8Array;

  private pairIndex!: Map<number, number>;
  private routeIndex!: Map<number, number>;

  private geomIndex!: Map<number, number>;
  private geomFrom!: Int32Array;
  private geomTo!: Int32Array;
  private geomOff!: Uint32Array;
  private geomXY!: Float32Array;

  static async load(base = "/data"): Promise<Planner> {
    const p = new Planner();
    const [
      stationsJson,
      routesJson,
      metaJson,
      coords,
      patOff,
      patSta,
      edgeOff,
      edgeTo,
      edgeRoute,
      pairFrom,
      pairTo,
      hourOff,
      hourHow,
      hourSec,
      bandVals,
      baseVals,
      schedVals,
      crowdRoute,
      crowdHour,
      crowdSta,
      crowdBand,
      crowdConf,
      crowdLoad,
      geomFrom,
      geomTo,
      geomOff,
      geomXY,
    ] = await Promise.all([
      fetch(`${base}/stations.json`).then((r) => r.json()),
      fetch(`${base}/routes.json`).then((r) => r.json()),
      fetch(`${base}/meta.json`).then((r) => r.json()),
      fetchBin(`${base}/stations.bin`),
      fetchBin(`${base}/patterns_off.bin`),
      fetchBin(`${base}/patterns_sta.bin`),
      fetchBin(`${base}/edges_off.bin`),
      fetchBin(`${base}/edges_to.bin`),
      fetchBin(`${base}/edges_route.bin`),
      fetchBin(`${base}/pair_from.bin`),
      fetchBin(`${base}/pair_to.bin`),
      fetchBin(`${base}/hour_off.bin`),
      fetchBin(`${base}/hour_how.bin`),
      fetchBin(`${base}/hour_sec.bin`),
      fetchBin(`${base}/band.bin`),
      fetchBin(`${base}/base.bin`),
      fetchBin(`${base}/sched.bin`),
      fetchBin(`${base}/crowd_route.bin`),
      fetchBin(`${base}/crowd_hour.bin`),
      fetchBin(`${base}/crowd_sta.bin`),
      fetchBin(`${base}/crowd_band.bin`),
      fetchBin(`${base}/crowd_conf.bin`),
      fetchBin(`${base}/crowd_load.bin`),
      fetchBin(`${base}/geom_from.bin`),
      fetchBin(`${base}/geom_to.bin`),
      fetchBin(`${base}/geom_off.bin`),
      fetchBin(`${base}/geom_xy.bin`),
    ]);

    p.stations = stationsJson;
    p.routes = routesJson;
    p.meta = metaJson;
    p.coords = new Float32Array(coords);
    p.patOff = new Uint32Array(patOff);
    p.patSta = new Uint16Array(patSta);
    p.edgeOff = new Uint32Array(edgeOff);
    p.edgeTo = new Uint16Array(edgeTo);
    p.edgeRoute = new Uint16Array(edgeRoute);
    p.pairFrom = new Uint16Array(pairFrom);
    p.pairTo = new Uint16Array(pairTo);
    p.hourOff = new Uint32Array(hourOff);
    p.hourHow = new Uint8Array(hourHow);
    p.hourSec = new Uint16Array(hourSec);
    p.bandVals = new Uint16Array(bandVals);
    p.baseVals = new Uint16Array(baseVals);
    p.schedVals = new Uint16Array(schedVals);
    p.crowdBand = new Uint8Array(crowdBand);
    p.crowdConf = new Uint8Array(crowdConf);
    p.crowdLoad = new Uint8Array(crowdLoad);
    p.geomFrom = new Int32Array(geomFrom);
    p.geomTo = new Int32Array(geomTo);
    p.geomOff = new Uint32Array(geomOff);
    p.geomXY = new Float32Array(geomXY);

    p.geomIndex = new Map();
    for (let i = 0; i < p.geomFrom.length; i++) {
      p.geomIndex.set(p.geomFrom[i] * 65536 + p.geomTo[i], i);
    }

    p.pairIndex = new Map();
    for (let i = 0; i < p.pairFrom.length; i++) {
      p.pairIndex.set(p.pairFrom[i] * 65536 + p.pairTo[i], i);
    }
    p.routeIndex = new Map();
    for (let i = 0; i < p.routes.ids.length; i++) p.routeIndex.set(p.routes.ids[i], i);

    const cr = new Uint16Array(crowdRoute);
    const ch = new Uint8Array(crowdHour);
    const cs = new Uint16Array(crowdSta);
    p.crowdIndex = new Map();
    for (let i = 0; i < cr.length; i++) {
      p.crowdIndex.set(cr[i] * 100000 + ch[i] * 4096 + cs[i], i);
    }

    return p;
  }

  latLon(idx: number): [number, number] {
    return [this.coords[idx * 2], this.coords[idx * 2 + 1]];
  }

  /** Station indices along a route between two stations, inclusive. */
  routeStations(routeId: number, from: number, to: number): number[] {
    const ridx = this.routeIndex.get(routeId);
    if (ridx === undefined) return [];
    const start = this.patOff[ridx];
    const end = this.patOff[ridx + 1];
    let i0 = -1;
    let i1 = -1;
    for (let i = start; i < end; i++) {
      if (this.patSta[i] === from) i0 = i;
      if (this.patSta[i] === to) i1 = i;
    }
    if (i0 < 0 || i1 <= i0) return [];
    return Array.from(this.patSta.subarray(i0, i1 + 1));
  }

  /**
   * Road-following polyline between two adjacent stations, as [lon, lat].
   *
   * Reconstructed from road centerlines. Returns an empty array when the pair
   * has no geometry; callers must omit that segment rather than draw a chord.
   *
   * Takes station *indices* (the planner's internal 0..n-1 space), not station
   * IDs, and translates before lookup.
   */
  pairGeometry(fromIdx: number, toIdx: number): [number, number][] {
    const from = this.stations.ids[fromIdx];
    const to = this.stations.ids[toIdx];
    if (from === undefined || to === undefined) return [];
    const i = this.geomIndex.get(from * 65536 + to);
    if (i === undefined) return [];
    const start = this.geomOff[i];
    const end = this.geomOff[i + 1];
    const out: [number, number][] = [];
    for (let k = start; k < end; k++) {
      out.push([this.geomXY[k * 2], this.geomXY[k * 2 + 1]]);
    }
    return out;
  }
  /** Nearest stations to a point, sorted by distance. */
  nearby(lat: number, lon: number, radius = MAX_WALK_M): { idx: number; dist: number }[] {
    const out: { idx: number; dist: number }[] = [];
    for (let i = 0; i < this.stations.ids.length; i++) {
      const d = haversine(lat, lon, this.coords[i * 2], this.coords[i * 2 + 1]);
      if (d <= radius) out.push({ idx: i, dist: d });
    }
    out.sort((a, b) => a.dist - b.dist);
    return out;
  }

  /** Resolve a free-text place name to a station index. */
  search(query: string, limit = 8): { idx: number; name: string; score: number }[] {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const tokens = q.split(/\s+/).filter(Boolean);
    const out: { idx: number; name: string; score: number }[] = [];
    for (let i = 0; i < this.stations.names.length; i++) {
      const name = this.stations.names[i];
      const lower = name.toLowerCase();
      let score = 0;
      if (lower === q) score = 100;
      else if (lower.startsWith(q)) score = 80;
      else if (lower.includes(q)) score = 60;
      else {
        const hits = tokens.filter((t) => lower.includes(t)).length;
        if (hits === tokens.length) score = 40;
        else if (hits > 0) score = 10 * hits;
      }
      if (score > 0) out.push({ idx: i, name, score: score - name.length * 0.05 });
    }
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, limit);
  }

  /** Predicted seconds for one station pair at a given time. */
  travelTime(from: number, to: number, when: Date): { seconds: number; level: string } {
    const key = from * 65536 + to;
    const pi = this.pairIndex.get(key);
    const how = when.getDay() === 0 ? 6 : when.getDay() - 1;
    const hourOfWeek = how * 24 + when.getHours();
    const bi = bandIndex(when.getHours());

    if (pi !== undefined) {
      const start = this.hourOff[pi];
      const end = this.hourOff[pi + 1];
      for (let k = start; k < end; k++) {
        if (this.hourHow[k] === hourOfWeek) {
          const v = this.hourSec[k];
          if (v > 0) return { seconds: v, level: "pair_hour" };
        }
      }
      const bandVal = this.bandVals[pi * 6 + bi];
      if (bandVal > 0) return { seconds: bandVal, level: "pair_band" };
      if (this.baseVals[pi] > 0) return { seconds: this.baseVals[pi], level: "pair" };
      if (this.schedVals[pi] > 0) {
        const factor = this.meta.band_factor[BAND_NAMES[bi]] ?? 1;
        return { seconds: this.schedVals[pi] * factor, level: "schedule_scaled" };
      }
    }
    const gb = this.meta.global_band[BAND_NAMES[bi]];
    if (gb) return { seconds: gb, level: "global_band" };
    return { seconds: this.meta.global_median, level: "global" };
  }

  private crowdingFor(routeIdx: number, hour: number, stationIdx: number): CrowdingInfo | null {
    const key = routeIdx * 100000 + hour * 4096 + stationIdx;
    const i = this.crowdIndex.get(key);
    if (i === undefined) return null;
    return {
      band: BAND_CODE[this.crowdBand[i]] ?? "unknown",
      confidence: CONF_CODE[this.crowdConf[i]] ?? "low",
      loadIndex: this.crowdLoad[i],
      scope: "hour",
    };
  }

  private crowdingFallback(routeIdx: number, hour: number, stationIdx: number): CrowdingInfo {
    const direct = this.crowdingFor(routeIdx, hour, stationIdx);
    if (direct) return direct;
    // Fall back to any hour for this route+station, then this route+hour.
    let best: CrowdingInfo | null = null;
    for (let h = 0; h < 24; h++) {
      const c = this.crowdingFor(routeIdx, h, stationIdx);
      if (c) {
        best = { ...c, confidence: "low", scope: "all_hours" };
        break;
      }
    }
    if (best) return best;
    for (let s = 0; s < this.stations.ids.length; s++) {
      const c = this.crowdingFor(routeIdx, hour, s);
      if (c) return { ...c, confidence: "low", scope: "route_hour" };
    }
    return {
      band: "unknown",
      confidence: "none",
      loadIndex: 0,
      scope: "none",
      reason: "no ticketing evidence for this route",
    };
  }

  plan(
    origin: { lat: number; lon: number },
    destination: { lat: number; lon: number },
    depart: Date,
    maxTransfers = 2,
    alternatives = 3
  ): Journey[] {
    const access = this.nearby(origin.lat, origin.lon);
    const egressList = this.nearby(destination.lat, destination.lon);
    if (!access.length || !egressList.length) return [];
    const egress = new Map<number, number>();
    for (const e of egressList) egress.set(e.idx, e.dist);

    const journeys: Journey[] = [];
    const bannedRoutes = new Set<number>();
    const seen = new Set<string>();

    for (let attempt = 0; attempt < alternatives * 3 && journeys.length < alternatives; attempt++) {
      const j = this.searchRoutes(access, egress, depart, maxTransfers, bannedRoutes);
      if (!j) break;
      const key = j.legs
        .filter((l) => l.kind === "bus")
        .map((l) => `${l.routeId}:${l.fromStation}:${l.toStation}`)
        .join("|");
      if (seen.has(key)) {
        // Ban every route on this duplicate so the next pass must differ.
        for (const l of j.legs) {
          if (l.kind === "bus" && l.routeId != null) {
            const idx = this.routeIndex.get(l.routeId);
            if (idx !== undefined) bannedRoutes.add(idx);
          }
        }
        continue;
      }
      seen.add(key);
      journeys.push(j);
      // Ban every route this journey used, so the next pass must differ
      // structurally. `bannedRoutes` holds route *indices* (the CSR index used
      // by `edgeRoute`), not route IDs, so the ID must be mapped back.
      for (const l of j.legs) {
        if (l.kind === "bus" && l.routeId != null) {
          const idx = this.routeIndex.get(l.routeId);
          if (idx !== undefined) bannedRoutes.add(idx);
        }
      }
    }
    return journeys;
  }

  private searchRoutes(
    access: { idx: number; dist: number }[],
    egress: Map<number, number>,
    depart: Date,
    maxTransfers: number,
    bannedRoutes: Set<number>
  ): Journey | null {
    type Label = { arrive: number; board: number | null; route: number | null; alight: number | null };
    const labels: Map<number, Label>[] = [new Map()];

    for (const a of access) {
      const arrive = depart.getTime() + (a.dist / WALK_SPEED) * 1000;
      const cur = labels[0].get(a.idx);
      if (!cur || arrive < cur.arrive) {
        labels[0].set(a.idx, { arrive, board: null, route: null, alight: null });
      }
    }

    for (let round = 0; round <= maxTransfers; round++) {
      const reachable = new Map<number, Label>();
      for (let k = 0; k <= round; k++) {
        for (const [sid, label] of labels[k]) {
          const cur = reachable.get(sid);
          if (!cur || label.arrive < cur.arrive) reachable.set(sid, label);
        }
      }

      const boardable = new Map<number, { station: number; arrive: number }>();
      for (const [sid, label] of reachable) {
        const start = this.edgeOff[sid];
        const end = this.edgeOff[sid + 1];
        for (let e = start; e < end; e++) {
          const rid = this.edgeRoute[e];
          if (bannedRoutes.has(rid)) continue;
          const cur = boardable.get(rid);
          if (!cur || label.arrive < cur.arrive) {
            boardable.set(rid, { station: sid, arrive: label.arrive });
          }
        }
      }

      const improved = new Map<number, Label>();
      for (const [rid, start] of boardable) {
        const pStart = this.patOff[rid];
        const pEnd = this.patOff[rid + 1];
        let idx = -1;
        for (let k = pStart; k < pEnd; k++) {
          if (this.patSta[k] === start.station) {
            idx = k;
            break;
          }
        }
        if (idx < 0) continue;
        let arrive = start.arrive;
        let prev = start.station;
        for (let k = idx + 1; k < pEnd; k++) {
          const nxt = this.patSta[k];
          const when = new Date(arrive);
          const tt = this.travelTime(prev, nxt, when).seconds;
          arrive += tt * 1000;
          const cur = improved.get(nxt);
          if (!cur || arrive < cur.arrive) {
            improved.set(nxt, { arrive, board: start.station, route: rid, alight: nxt });
          }
          prev = nxt;
        }
      }
      labels.push(improved);
    }

    let bestSid: number | null = null;
    let bestRound = 0;
    let bestTotal = Infinity;
    for (let k = 1; k < labels.length; k++) {
      // Start at round 1: round 0 is the access walk only, and a journey with
      // no bus leg is not a bus journey. Accepting it produced a 0.4-minute
      // "journey" that was just a walk between two nearby stations.
      for (const [sid, label] of labels[k]) {
        const dist = egress.get(sid);
        if (dist === undefined) continue;
        const total = label.arrive + (dist / WALK_SPEED) * 1000;
        if (total < bestTotal) {
          bestTotal = total;
          bestSid = sid;
          bestRound = k;
        }
      }
    }
    if (bestSid === null) return null;

    const rides: { route: number; board: number; alight: number }[] = [];
    let sid: number | null = bestSid;
    let k = bestRound;
    while (sid !== null && k >= 0) {
      const label = labels[k].get(sid);
      if (!label) {
        k--;
        continue;
      }
      if (label.board === null || label.route === null) break;
      rides.push({ route: label.route, board: label.board, alight: label.alight ?? sid });
      sid = label.board;
      k--;
    }
    rides.reverse();
    if (!rides.length) return null;

    const legs: Leg[] = [];
    const accessHit = access.find((a) => a.idx === rides[0].board) ?? access[0];
    let t = depart.getTime();
    const walkArrive = t + (accessHit.dist / WALK_SPEED) * 1000;
    legs.push({
      kind: "walk",
      fromName: "Origin",
      toName: this.stations.names[accessHit.idx],
      fromStation: null,
      toStation: accessHit.idx,
      depart: t,
      arrive: walkArrive,
      routeId: null,
      routeName: null,
      distanceM: accessHit.dist,
      confidence: "measured",
      crowding: null,
    });
    t = walkArrive;

    for (const ride of rides) {
      const pStart = this.patOff[ride.route];
      const pEnd = this.patOff[ride.route + 1];
      let i0 = -1;
      let i1 = -1;
      for (let i = pStart; i < pEnd; i++) {
        if (this.patSta[i] === ride.board) i0 = i;
        if (this.patSta[i] === ride.alight) i1 = i;
      }
      if (i0 < 0 || i1 <= i0) continue;
      let level = "pair_hour";
      const rideStart = t;
      for (let i = i0; i < i1; i++) {
        const a = this.patSta[i];
        const b = this.patSta[i + 1];
        const r = this.travelTime(a, b, new Date(t));
        t += r.seconds * 1000;
        if (r.level !== "pair_hour") level = r.level;
      }
      const routeIdx = this.routeIndex.get(this.routes.ids[ride.route]) ?? ride.route;
      legs.push({
        kind: "bus",
        fromName: this.stations.names[ride.board],
        toName: this.stations.names[ride.alight],
        fromStation: ride.board,
        toStation: ride.alight,
        depart: rideStart,
        arrive: t,
        routeId: this.routes.ids[ride.route],
        routeName: this.routes.names[ride.route],
        distanceM: null,
        confidence:
          level === "pair_hour" ? "high" : level === "pair_band" || level === "pair" ? "medium" : "low",
        crowding: this.crowdingFallback(routeIdx, new Date(rideStart).getHours(), ride.board),
      });
    }

    const egressDist = egress.get(bestSid) ?? 0;
    legs.push({
      kind: "walk",
      fromName: this.stations.names[bestSid],
      toName: "Destination",
      fromStation: bestSid,
      toStation: null,
      depart: t,
      arrive: t + (egressDist / WALK_SPEED) * 1000,
      routeId: null,
      routeName: null,
      distanceM: egressDist,
      confidence: "measured",
      crowding: null,
    });

    const busLegs = legs.filter((l) => l.kind === "bus");
    const order: Record<Band, number> = {
      crowded: 3,
      standing_room: 2,
      seats_available: 1,
      unknown: 0,
    };
    const worst = busLegs.reduce<Band>((acc, l) => {
      const b = l.crowding?.band ?? "unknown";
      return order[b] > order[acc] ? b : acc;
    }, "unknown");
    const known = busLegs.filter((l) => (l.crowding?.band ?? "unknown") !== "unknown").length;

    return {
      legs,
      durationS: (legs[legs.length - 1].arrive - legs[0].depart) / 1000,
      transfers: Math.max(0, busLegs.length - 1),
      walkS: legs.filter((l) => l.kind === "walk").reduce((s, l) => s + (l.arrive - l.depart) / 1000, 0),
      busS: busLegs.reduce((s, l) => s + (l.arrive - l.depart) / 1000, 0),
      crowdingSummary: {
        band: worst,
        confidence: worst === "unknown" ? "none" : "low",
        legsKnown: known,
        legsTotal: busLegs.length,
      },
    };
  }
}
