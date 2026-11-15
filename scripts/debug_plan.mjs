// Debug the alternatives loop against the exported model.
// Run with: node --experimental-strip-types scripts/debug_plan.mjs
import { readFileSync } from "node:fs";
import { join } from "node:path";

const DATA = join(process.cwd(), "web-next", "public", "data");
const read = (f) => {
  const b = readFileSync(join(DATA, f));
  // Copy into a fresh ArrayBuffer: readFileSync returns a Buffer that may be a
  // view into a larger pooled ArrayBuffer, so using b.buffer directly reads
  // the wrong bytes.
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
};

const stations = JSON.parse(readFileSync(join(DATA, "stations.json"), "utf8"));
const routes = JSON.parse(readFileSync(join(DATA, "routes.json"), "utf8"));
const meta = JSON.parse(readFileSync(join(DATA, "meta.json"), "utf8"));

const u16 = (b) => new Uint16Array(b);
const u32 = (b) => new Uint32Array(b);
const u8 = (b) => new Uint8Array(b);

const coords = new Float32Array(read("stations.bin"));
const patOff = u32(read("patterns_off.bin"));
const patSta = u16(read("patterns_sta.bin"));
const edgeOff = u32(read("edges_off.bin"));
const edgeTo = u16(read("edges_to.bin"));
const edgeRoute = u16(read("edges_route.bin"));
const pairFrom = u16(read("pair_from.bin"));
const pairTo = u16(read("pair_to.bin"));
const hourOff = u32(read("hour_off.bin"));
const hourHow = u8(read("hour_how.bin"));
const hourSec = u16(read("hour_sec.bin"));
const bandVals = u16(read("band.bin"));
const baseVals = u16(read("base.bin"));
const schedVals = u16(read("sched.bin"));

const pairIndex = new Map();
for (let i = 0; i < pairFrom.length; i++) pairIndex.set(pairFrom[i] * 65536 + pairTo[i], i);

const BAND_NAMES = ["night", "early_peak", "morning_peak", "midday", "evening_peak", "late"];
function bandIndex(h) {
  if (h <= 5) return 0;
  if (h <= 7) return 1;
  if (h <= 11) return 2;
  if (h <= 16) return 3;
  if (h <= 20) return 4;
  return 5;
}
function travelTime(from, to, when) {
  const pi = pairIndex.get(from * 65536 + to);
  const how = when.getDay() === 0 ? 6 : when.getDay() - 1;
  const howk = how * 24 + when.getHours();
  const bi = bandIndex(when.getHours());
  if (pi !== undefined) {
    for (let k = hourOff[pi]; k < hourOff[pi + 1]; k++) {
      if (hourHow[k] === howk && hourSec[k] > 0) return hourSec[k];
    }
    if (bandVals[pi * 6 + bi] > 0) return bandVals[pi * 6 + bi];
    if (baseVals[pi] > 0) return baseVals[pi];
    if (schedVals[pi] > 0) return schedVals[pi] * (meta.band_factor[BAND_NAMES[bi]] ?? 1);
  }
  const gb = meta.global_band[BAND_NAMES[bi]];
  if (gb) return gb;
  return meta.global_median;
}

function search(access, egress, depart, maxTransfers, bannedRoutes) {
  const labels = [new Map()];
  for (const a of access) {
    labels[0].set(a.idx, { arrive: depart.getTime() + (a.dist / 1.25) * 1000, board: null, route: null, alight: null });
  }
  for (let round = 0; round <= maxTransfers; round++) {
    const reachable = new Map();
    for (let k = 0; k <= round; k++) {
      for (const [sid, l] of labels[k]) {
        const cur = reachable.get(sid);
        if (!cur || l.arrive < cur.arrive) reachable.set(sid, l);
      }
    }
    const boardable = new Map();
    for (const [sid, l] of reachable) {
      for (let e = edgeOff[sid]; e < edgeOff[sid + 1]; e++) {
        const rid = edgeRoute[e];
        if (bannedRoutes.has(rid)) continue;
        const cur = boardable.get(rid);
        if (!cur || l.arrive < cur.arrive) boardable.set(rid, { station: sid, arrive: l.arrive });
      }
    }
    const improved = new Map();
    for (const [rid, start] of boardable) {
      let idx = -1;
      for (let k = patOff[rid]; k < patOff[rid + 1]; k++) {
        if (patSta[k] === start.station) { idx = k; break; }
      }
      if (idx < 0) continue;
      let arrive = start.arrive;
      let prev = start.station;
      for (let k = idx + 1; k < patOff[rid + 1]; k++) {
        const nxt = patSta[k];
        const tt = travelTime(prev, nxt, new Date(arrive));
        if (!Number.isFinite(tt) || tt <= 0) { console.log("BAD tt", prev, nxt, tt); }
        arrive += tt * 1000;
        const cur = improved.get(nxt);
        if (!cur || arrive < cur.arrive) improved.set(nxt, { arrive, board: start.station, route: rid, alight: nxt });
        prev = nxt;
      }
    }
    labels.push(improved);
  }
  let bestSid = null, bestRound = 0, bestTotal = Infinity;
  for (let k = 1; k < labels.length; k++) {
    for (const [sid, l] of labels[k]) {
      const d = egress.get(sid);
      if (d === undefined) continue;
      const total = l.arrive + (d / 1.25) * 1000;
      if (total < bestTotal) { bestTotal = total; bestSid = sid; bestRound = k; }
    }
  }
  if (bestSid === null) return null;
  console.log("  bestTotal", bestTotal, "bestSid", bestSid, "bestRound", bestRound);
  const rides = [];
  let sid = bestSid, k = bestRound;
  while (sid !== null && k >= 0) {
    const l = labels[k].get(sid);
    if (!l) { k--; continue; }
    if (l.board === null || l.route === null) break;
    rides.push({ route: l.route, board: l.board, alight: l.alight ?? sid });
    sid = l.board; k--;
  }
  rides.reverse();
  return rides.length ? { rides, total: bestTotal } : null;
}

function nearby(lat, lon, radius = 1200) {
  const out = [];
  for (let i = 0; i < stations.ids.length; i++) {
    const dlat = ((coords[i * 2] - lat) * Math.PI) / 180;
    const dlon = ((coords[i * 2 + 1] - lon) * Math.PI) / 180;
    const a = Math.sin(dlat / 2) ** 2 + Math.cos((lat * Math.PI) / 180) * Math.cos((coords[i * 2] * Math.PI) / 180) * Math.sin(dlon / 2) ** 2;
    const d = 2 * 6371000 * Math.asin(Math.sqrt(a));
    if (d <= radius) out.push({ idx: i, dist: d });
  }
  out.sort((a, b) => a.dist - b.dist);
  return out;
}

function findStation(q) {
  const lower = q.toLowerCase();
  let best = -1, bestScore = 0;
  for (let i = 0; i < stations.names.length; i++) {
    const n = stations.names[i].toLowerCase();
    let s = 0;
    if (n === lower) s = 100;
    else if (n.startsWith(lower)) s = 80;
    else if (n.includes(lower)) s = 60;
    if (s > bestScore) { bestScore = s; best = i; }
  }
  return best;
}

const oIdx = findStation("ISBT Kashmere Gate");
const dIdx = findStation("Nehru Place");
console.log("origin:", stations.names[oIdx], "dest:", stations.names[dIdx]);
console.log("origin coords:", coords[oIdx*2], coords[oIdx*2+1]);
console.log("dest coords:", coords[dIdx*2], coords[dIdx*2+1]);

const access = nearby(coords[oIdx * 2], coords[oIdx * 2 + 1]);
const egressList = nearby(coords[dIdx * 2], coords[dIdx * 2 + 1]);
const egress = new Map(egressList.map((e) => [e.idx, e.dist]));
console.log("access:", access.length, "egress:", egressList.length);
console.log("egress sample:", egressList.slice(0,3).map(e => [stations.names[e.idx], Math.round(e.dist)]));
const depart = new Date("2026-10-15T08:30:00+05:30").getTime();
const departDate = new Date(depart);

const banned = new Set();
for (let attempt = 0; attempt < 12; attempt++) {
  const r = search(access, egress, departDate, 2, banned);
  if (!r) { console.log(`attempt ${attempt}: no journey`); break; }
  const names = r.rides.map((x) => routes.names[x.route]);
  console.log(`attempt ${attempt}: ${((r.total - departDate.getTime()) / 60000).toFixed(1)} min  ${names.join(" -> ")}  banned=${banned.size}`);
  for (const x of r.rides) banned.add(x.route);
}
