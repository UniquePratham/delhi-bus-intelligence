# Handover: Road-only bus route rendering

## Goal

The evaluator map must draw bus journeys only along rendered road centerlines.
It must never draw a straight chord across a block, park, river, building,
rail line, or other non-road area. The map must provide a working 2D/3D switch
and show only boarding, alighting, and transfer stops.

Live project:

- Vercel: https://delhi-bus-intelligence.vercel.app
- GitHub: private repository `UniquePratham/delhi-bus-intelligence`
- Branch: `main`
- Last committed code: `b4da8bd`

## What has already been established

The original "flying bus" screenshot was a combination of two separate bugs:

1. **MapLibre layer-order bug**
   - Route layers were appended above `city-3d`.
   - They were therefore painted over extruded building roofs.
   - Route layers now use `beforeId: "city-3d"` and are below buildings.
   - The route index must remain below `city-3d` in `getStyle().layers`.

2. **Geometry-fallback bug**
   - Missing per-pair geometry was represented by a two-point chord.
   - The browser's fallback in `Map3D.tsx` could therefore draw across
     parks/building blocks.
   - A chord fallback must never exist in the final implementation.

The current `Map3D` 2D/3D implementation is in:

- `web-next/components/Map3D.tsx`
- `web-next/app/page.tsx`

The 2D/3D toggle is implemented and station dots are limited to leg origin/
destination stops. Intermediate journey stops are not currently marked.

## Current worktree

The following route-on-road work is partially implemented and not committed:

- Modified: `web-next/public/data/geom_off.bin`
- Modified: `web-next/public/data/geom_xy.bin`
- Added: `scripts/route_on_roads.py`
- Added: `scripts/_diag_chord.py`
- Added: `scripts/audit_on_road.py`
- Added: `scripts/_check_cov2.py`
- Added: `scripts/_diag_fail2.py`
- Added: `scripts/_diag_ratio.py`

The authoritative road graph script was started and reached this point:

- 676,138 road nodes
- 759,609 raw road edges
- 888,591 local tile-connector edges
- 4,347 initial components
- 4,311 components bridged
- Largest component: 675,906 nodes (100%)
- 12,352 route pairs checked
- 10,014 pairs matched to roads (81.1%)
- 2,338 pairs not emitted (no geometry, never a chord)
- Output: 139,445 points, 1.12 MB

This run is from `scripts/route_on_roads.py`. It is the first attempt that
connected the road graph, but it is not complete: several valid short pairs
were rejected by a pure ratio-based detour gate.

## The exact remaining bug

The script in `scripts/route_on_roads.py` used:

```python
road_m <= 1.8 * chord_m
```

That rejects normal short road segments. Examples measured before this handover:

- 370 m chord -> 1,102 m road; ratio 2.98
- 712 m chord -> 1,713 m road; ratio 2.41
- 542 m chord -> 1,150 m road; ratio 2.12
- 236 m chord -> 502 m road; ratio 2.12
- 176 m chord -> 506 m road; ratio 2.88
- 259 m chord -> 2,951 m road; ratio 11.39

The first five are normal short segments where the real road goes around a
block. The 259 m -> 2,951 m case should still be rejected as a detour.

A length-aware rule was partially started in the conversation but not run:

```python
MAX_DETOUR = 1.8
DETOUR_SLACK_M = 500.0
road_m <= MAX_DETOUR * chord_m + DETOUR_SLACK_M
```

Do not consider that change verified. It must be tested against:

- Short pairs around blocks
- A pair genuinely spanning several kilometres
- All 12,352 route pairs
- Every currently missing demo-route pair
- An independent on-road distance audit

## Browser fallback that must be removed

In `web-next/components/Map3D.tsx`, the current per-pair loop still contains:

```typescript
if (geom.length >= 2) {
  // use geometry
} else {
  const ca = coord(a);
  const cb = coord(b);
  // add a straight chord
}
```

This is a forbidden fallback and must be removed or made impossible. If a pair
has no road geometry, emit nothing for that pair. Never draw a two-point chord.
Do not hide the gap in the UI.

The browser also currently marks one stop at `leg.fromStation` and
`leg.toStation`. If the line is continuous across multiple legs, avoid duplicate
and intermediate dots.

## Required completion criteria

1. Every rendered polyline point lies on a road centerline.
2. No browser code can fall back to a chord.
3. Every pair in the demo routes has geometry or is reported missing.
4. 241DOWN, 274AUP, and 930DOWN have no missing pairs.
5. Independently measure every drawn point's distance from the road graph.
   Report median, p90, p99, and percentage farther than 25 m.
6. The close-zoom screenshot must show the line on road pavement.
7. The 2D and 3D modes must both show the same road geometry.
8. 3D route layers must remain below `city-3d`.
9. Run the browser smoke test waiting for a real route-ready signal.
10. Rebuild and deploy to Vercel only after visual verification.

## Suggested files to use

Primary scripts:

- `scripts/route_on_roads.py`
- `scripts/build_route_geometry.py`
- `scripts/match_routes_to_roads.py`
- `scripts/ensure_route_geometry.py`
- `scripts/validate_geometry.py`
- `scripts/audit_on_road.py`
- `scripts/web_smoke.py`

Primary UI/model files:

- `web-next/components/Map3D.tsx`
- `web-next/app/page.tsx`
- `web-next/lib/planner.ts`

Geometry binaries:

- `web-next/public/data/geom_from.bin`
- `web-next/public/data/geom_to.bin`
- `web-next/public/data/geom_off.bin`
- `web-next/public/data/geom_xy.bin`

## Recommended next-agent method

Do not start by adding more smoothing or changing the visual style.

Start by bisecting the problem:

1. Treat the road graph as the authoritative source.
2. Verify that every station's nearest road node is within a documented radius.
3. Bridge graph components and verify the largest component is 100%.
4. Route every pair, but reject bad matches explicitly.
5. Replace the browser's chord fallback with a no-geometry state.
6. Audit all emitted points against the road graph.
7. Visual-test the exact screenshot location at z14-z15.
8. Test 2D and 3D.
9. Rebuild, run Playwright, deploy, then verify the live domain.

Do not claim success until an independent audit proves the points sit on roads.

## Commands

```bash
# Build road-only geometry for every route pair (~4 min with the cached graph;
# add --rebuild-graph to refetch the road tiles, which is much slower)
python -u scripts/route_on_roads.py

# Independent on-road audit. Exits non-zero if any drawn point is off the road
# data or if a chord is found.
python scripts/audit_on_road.py

# Existing geometry checks
python scripts/validate_geometry.py
python scripts/_check_cov2.py

# UI
cd web-next
npm run build
npx next start -p 3100

# From repository root
python scripts/web_smoke.py http://127.0.0.1:3100/

# Deploy
cd web-next
npx vercel --prod --yes
```

## Git warning

The worktree contains uncommitted geometry binaries and new scripts. Do not
commit or deploy the current result until the no-chord rule and independent
on-road audit pass.

## Status after this pass

Road-only geometry is implemented and verified. Nothing is committed or deployed.

### What changed

- `scripts/route_on_roads.py` is a single road-truth builder. The road graph is
  the only source of a drawn point: 665,790 nodes, 725,259 raw centerline edges,
  519,843 tile-stitch edges and 4,403 component bridges. Every route pair is
  routed with an A* shortest-path search; a component bridge costs an extra
  300 m, so the search only crosses a break when nothing else connects.
- Drawable geometry is raw road edges plus tile-stitch hops of at most 30 m (the
  same pavement continued across a tile or feature boundary). Longer component
  bridges are never drawn: the line is split with an explicit gap.
- `web-next/components/Map3D.tsx` cannot draw a chord. A pair with no geometry is
  omitted and counted; a short fragment join is counted separately and shown as
  a short gap, not as a missing stretch.
- `scripts/audit_on_road.py` is rewritten as an independent check: it rebuilds
  a reference from raw centerlines cut into 20 m pieces and never uses the
  router's path, so it can disagree with the builder.

### Measured

| Check | Result |
|---|---|
| Route pairs | 12,352 |
| Routed on roads | 12,316 (99.7%) |
| No geometry (omitted, never a chord) | 36: 25 stations >200 m from any road, 11 wrong-road snaps |
| Points / size | 226,299 / 1.81 MB |
| Road/chord ratio | median 1.20, p90 2.34, p99 6.78, max 22.23 |
| Drawn links | 160,335 road edges, 51,706 tile-stitch hops |
| On-road audit, 1,751,507 samples at 10 m | median 0.08 m, p90 0.25 m, p99 5.23 m, max 14.92 m, 0% beyond 25 m |
| Pairs with any point >25 m from a road | 0 |
| Station-to-station chords drawn | 0 |
| 241DOWN / 274AUP / 930DOWN missing pairs | 0 / 0 / 0 |
| 2D/3D geometry identical, route below city-3d | yes (smoke test) |

931 routed pairs contain one short gap (median 47 m, p90 91 m) where two mapped
road fragments meet. The gap is reported on screen and never drawn as road.

### The detour question, resolved with evidence

The handover asked for the 259 m to 2,951 m case to stay rejected as a detour
while also requiring 930DOWN to have no missing pairs. Those two demands
conflict on the same pair: station 834 is Central Secretariat and station 835 is
Udyog Bhawan, adjacent stops on 930DOWN whose straight line crosses the Central
Vista. The shortest real road path between them is 2,867 m. The same pattern
appears for Talkatora/RML (Ridge forest), Shankar Road/Ridge Road, and SDM
Office/Geeta Colony (Yamuna crossing): genuine barriers, 3-12x the chord.

Refusing to draw those would leave holes in a bus route exactly where a chord
would be a lie. The gate is therefore length-aware and generous rather than
rejecting anything but the most direct road path:
ratio-only:

```python
road_m <= 15.0 * chord_m + 1000.0
```

The five short pairs around blocks in the handover pass (ratios 2.1-3.0), the
barrier pairs pass, and the 11 rejected pairs are all wrong-road snaps whose
best path is 8-16 km for a 400-600 m gap (ratios 19-31). They are omitted and
named in the build log.

### Superseded

`scripts/build_route_geometry.py` and `scripts/ensure_route_geometry.py` still
write `web-next/public/data/geom_*.bin` and have no on-road guarantee. Running
either one overwrites this result. The README now points at
`route_on_roads.py` and `audit_on_road.py`, and says so.

### Not done

No commit and no Vercel deploy. The commands are in this document; the local
build, smoke test and screenshots are in `data/artifacts/`.
