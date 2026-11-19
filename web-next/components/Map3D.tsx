"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import maplibregl, { Map as MlMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Journey } from "@/lib/planner";

const BAND_COLOR: Record<string, string> = {
  seats_available: "#4ade9b",
  standing_room: "#f5c451",
  crowded: "#ff6b81",
  unknown: "#5c6577",
};

// Station coordinates are injected once the planner has loaded.
let STATION_COORDS: ((idx: number) => [number, number] | null) | null = null;
export function setStationCoords(fn: (idx: number) => [number, number] | null) {
  STATION_COORDS = fn;
}
function coord(idx: number): [number, number] | null {
  return STATION_COORDS ? STATION_COORDS(idx) : null;
}

// Route patterns are injected too, so a ride can be drawn along every stop it
// serves rather than as one straight chord.
let ROUTE_PATH: ((routeId: number, from: number, to: number) => number[]) | null = null;
export function setRoutePath(fn: (routeId: number, from: number, to: number) => number[]) {
  ROUTE_PATH = fn;
}
function routePath(routeId: number | null, from: number, to: number): number[] {
  if (routeId === null || !ROUTE_PATH) return [];
  return ROUTE_PATH(routeId, from, to);
}

// Road-following geometry per station pair, reconstructed from GPS traces.
let PAIR_GEOM: ((from: number, to: number) => [number, number][]) | null = null;
export function setPairGeometry(fn: (from: number, to: number) => [number, number][]) {
  PAIR_GEOM = fn;
}
function pairGeom(from: number, to: number): [number, number][] {
  return PAIR_GEOM ? PAIR_GEOM(from, to) : [];
}

/** Metres between two [lon, lat] points, near enough at city scale. */
function metres(a: [number, number], b: [number, number]): number {
  const kx = 111320 * Math.cos(((a[1] + b[1]) * Math.PI) / 360);
  return Math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540);
}

export default function Map3D({
  journey,
  origin,
  destination,
  onLoaded,
  mode = "3d",
}: {
  journey: Journey | null;
  origin: { lat: number; lon: number } | null;
  destination: { lat: number; lon: number } | null;
  onLoaded?: () => void;
  mode?: "2d" | "3d";
}) {
  const holder = useRef<HTMLDivElement>(null);
  const map = useRef<MlMap | null>(null);
  const [ready, setReady] = useState(false);
  // Two different honesty problems, reported separately: a pair with no road
  // geometry at all, and a short break where two mapped fragments meet.
  const [routeNotes, setRouteNotes] = useState({ missingPairs: 0, linkGaps: 0, maxGapM: 0 });

  // `onLoaded` is typically an inline arrow, so its identity changes on every
  // render. Holding it in a ref keeps the map-init effect from tearing the map
  // down and rebuilding it (which reset the camera and dropped the route).
  const loadedCb = useRef(onLoaded);
  loadedCb.current = onLoaded;

  useEffect(() => {
    if (!holder.current || map.current) return;

    const m = new maplibregl.Map({
      container: holder.current,
      style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
      center: [77.2167, 28.6448],
      zoom: 12.4,
      pitch: 62,
      bearing: -22,
      antialias: true,
      attributionControl: false,
      dragRotate: true,
    });

    m.addControl(
      new maplibregl.AttributionControl({ compact: true }),
      "bottom-right"
    );

    m.on("load", () => {
      // Recolour the basemap so the city reads as a dark model rather than a
      // flat map: lift the ground, cool the water, warm the road network.
      for (const layer of m.getStyle().layers ?? []) {
        if (layer.type === "background") {
          m.setPaintProperty(layer.id, "background-color", "#0b0e14");
        }
        if (layer.type === "fill" && /water/i.test(layer.id)) {
          m.setPaintProperty(layer.id, "fill-color", "#0d1a28");
        }
        if (layer.type === "line" && /road|street|highway|bridge|tunnel/i.test(layer.id)) {
          try {
            m.setPaintProperty(layer.id, "line-color", "#39445a");
          } catch {
            /* data-driven colour; leave it */
          }
        }
        if (layer.type === "symbol") {
          try {
            m.setPaintProperty(layer.id, "text-color", "#7b8699");
            m.setPaintProperty(layer.id, "text-halo-color", "#0b0e14");
          } catch {
            /* not a text layer */
          }
        }
      }

      // Extrude buildings. CARTO's vector tiles carry `building` with
      // `render_height`, which is what makes the city read as 3D.
      const sources = m.getStyle().sources;
      const vector = Object.keys(sources).find(
        (k) => (sources[k] as { type?: string }).type === "vector"
      );
      if (vector && !m.getLayer("city-3d")) {
        const firstLabel = m
          .getStyle()
          .layers?.find((l) => l.type === "symbol" && l.layout?.["text-field"]);
        // CARTO's Protomaps tiles expose buildings under `buildings` (plural)
        // with `height`/`min_height`, not the OpenMapTiles `building` layer.
        // Wrapped in try/catch: a bad source-layer must not blank the map.
        try {
          m.addLayer(
            {
              id: "city-3d",
              type: "fill-extrusion",
              source: vector,
              "source-layer": "buildings",
              minzoom: 11,
              paint: {
                "fill-extrusion-color": [
                  "interpolate",
                  ["linear"],
                  ["coalesce", ["get", "height"], 8],
                  0, "#1b2231",
                  30, "#252e42",
                  90, "#313d57",
                  200, "#3d4b6b",
                ],
                "fill-extrusion-height": [
                  "interpolate",
                  ["linear"],
                  ["zoom"],
                  12, ["*", 0.9, ["coalesce", ["get", "height"], 8]],
                  13, ["coalesce", ["get", "height"], 8],
                ],
                "fill-extrusion-base": ["coalesce", ["get", "min_height"], 0],
                "fill-extrusion-opacity": 0.95,
                "fill-extrusion-vertical-gradient": true,
              },
            },
            firstLabel?.id
          );
        } catch (err) {
          console.warn("3D buildings unavailable:", err);
        }
      }

      setReady(true);
      loadedCb.current?.();
    });

    map.current = m;
    return () => {
      m.remove();
      map.current = null;
    };
  }, []);

  // Route geometry. Redraws whenever the journey or the style changes.
  const renderedKey = useRef<string | null>(null);
  const framedKey = useRef<string | null>(null);

  const drawRoute = useCallback(() => {
    const m = map.current;
    if (!m) return;
    // Layers added before the style finishes loading are silently dropped from
    // the render tree, so wait for it. The retry loop below keeps calling this
    // until the style is ready and the camera has framed the route.
    if (!m.isStyleLoaded()) return;
    if (!journey) return;

    const key = journey.legs
      .map((l) => `${l.routeId}:${l.fromStation}:${l.toStation}`)
      .join("|");
    if (renderedKey.current === key) return;

    for (const id of ["rt-glow", "rt-line", "rt-pts"]) {
      if (m.getLayer(id)) m.removeLayer(id);
    }
    for (const id of ["rt", "rtp"]) {
      if (m.getSource(id)) m.removeSource(id);
    }

    const segments: [number, number][][] = [];
    const points: GeoJSON.Feature[] = [];
    const markedStops = new Set<number>();
    let missingPairs = 0;
    let linkGaps = 0;
    let maxGapM = 0;
    let lastPoint: [number, number] | null = null;
    let gapStart: [number, number] | null = null;
    for (const leg of journey.legs) {
      if (leg.kind !== "bus" || leg.fromStation === null || leg.toStation === null) continue;

      // Walk the ride's stop sequence and draw each consecutive pair along its
      // road geometry. Missing pairs split the line and are reported; they are
      // never replaced by station-to-station chords.
      const stops = routePath(leg.routeId, leg.fromStation, leg.toStation);
      const seq = stops.length >= 2 ? stops : [];
      if (seq.length < 2) missingPairs++;
      let path: [number, number][] = [];
      const flushPath = () => {
        if (path.length >= 2) segments.push(path);
        path = [];
      };
      for (let i = 0; i < seq.length - 1; i++) {
        const a = seq[i];
        const b = seq[i + 1];
        const geom = pairGeom(a, b);
        if (geom.length < 2) {
          // No road geometry for this pair. Omit it entirely: a two-point chord
          // would cut across whatever sits between the two stops.
          flushPath();
          lastPoint = null;
          gapStart = null;
          missingPairs++;
          continue;
        }
        // Skip the first point on continuation to avoid duplicate vertices.
        for (let k = path.length ? 1 : 0; k < geom.length; k++) {
          const point = geom[k];
          if (!Number.isFinite(point[0]) || !Number.isFinite(point[1])) {
            // A break where two mapped road fragments meet. Split the line;
            // the jump itself is never drawn as road.
            gapStart = lastPoint;
            flushPath();
            continue;
          }
          if (gapStart) {
            linkGaps++;
            maxGapM = Math.max(maxGapM, metres(gapStart, point));
            gapStart = null;
          }
          path.push(point);
          lastPoint = point;
        }
      }
      flushPath();
      // Mark only the stops that matter: where you board, where you get off,
      // and the transfer points between legs. Every intermediate stop would
      // bury the line under dots.
      for (const sid of [leg.fromStation, leg.toStation]) {
        if (markedStops.has(sid)) continue;
        markedStops.add(sid);
        const c = coord(sid);
        if (!c) continue;
        points.push({
          type: "Feature",
          properties: { band: leg.crowding?.band ?? "unknown" },
          geometry: { type: "Point", coordinates: c },
        });
      }
    }
    if (!segments.length) {
      setRouteNotes({ missingPairs, linkGaps, maxGapM });
      if (holder.current) {
        holder.current.dataset.routeReady = "true";
        holder.current.dataset.routeDrawn = "false";
        holder.current.dataset.routeSegments = "0";
        holder.current.dataset.routeMissingPairs = String(missingPairs);
        holder.current.dataset.routeLinkGaps = String(linkGaps);
        holder.current.dataset.routeMaxGapM = String(Math.round(maxGapM));
      }
      renderedKey.current = key;
      return;
    }

    m.addSource("rt", {
      type: "geojson",
      data: {
        type: "Feature",
        properties: {},
        geometry: { type: "MultiLineString", coordinates: segments },
      },
    });
    // Insert the route *below* the building extrusion. A road is at ground
    // level, so buildings in front of it must occlude it; drawing the line on
    // top of the roofs is what made it read as a bus flying over the city.
    const belowBuildings = m.getLayer("city-3d") ? "city-3d" : undefined;
    m.addLayer(
      {
        id: "rt-glow",
        type: "line",
        source: "rt",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#7aa8ff",
          "line-width": 18,
          "line-opacity": 0.22,
          "line-blur": 14,
        },
      },
      belowBuildings
    );
    m.addLayer(
      {
        id: "rt-line",
        type: "line",
        source: "rt",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#e2ecff", "line-width": 3, "line-opacity": 1 },
      },
      belowBuildings
    );

    m.addSource("rtp", {
      type: "geojson",
      data: { type: "FeatureCollection", features: points },
    });
    m.addLayer(
      {
        id: "rt-pts",
        type: "circle",
        source: "rtp",
        paint: {
          "circle-radius": 4.5,
          "circle-color": [
            "match",
            ["get", "band"],
            "seats_available", BAND_COLOR.seats_available,
            "standing_room", BAND_COLOR.standing_room,
            "crowded", BAND_COLOR.crowded,
            BAND_COLOR.unknown,
          ],
          "circle-stroke-width": 2,
          "circle-stroke-color": "#06070a",
        },
      },
      belowBuildings
    );

    // Frame the route. `fitBounds` ignores `pitch`/`bearing` and clamps to
    // minZoom, which left the camera stuck at the floor. Compute the camera
    // for the bounds, then apply the tilt explicitly.
    const flat = segments.flat();
    const bounds = flat.reduce(
      (acc, c) => acc.extend(c),
      new maplibregl.LngLatBounds(flat[0], flat[0])
    );
    // A pitched camera compresses the lower half of the viewport, so the
    // bottom padding has to be larger than the top or the destination marker
    // falls off-screen.
    const cam = m.cameraForBounds(bounds, {
      padding: { top: 60, bottom: 170, left: 70, right: 70 },
      maxZoom: 13.4,
    });
    if (cam && cam.center) {
      m.easeTo({
        center: cam.center,
        zoom: Math.max(11.5, cam.zoom ?? 12),
        pitch: 52,
        bearing: -22,
        duration: 900,
      });
      framedKey.current = key;
    }
    m.setMinZoom(11);
    renderedKey.current = key;
    // A stable, inspectable signal that the route is on the map. The smoke
    // test waits on this instead of guessing at a delay.
    if (holder.current) {
      const layers = m.getStyle().layers ?? [];
      const routeIndex = Math.max(...["rt-glow", "rt-line", "rt-pts"].map((id) => layers.findIndex((l) => l.id === id)));
      const buildingIndex = layers.findIndex((l) => l.id === "city-3d");
      holder.current.dataset.routeReady = "true";
      holder.current.dataset.routeDrawn = "true";
      holder.current.dataset.routeSegments = String(segments.length);
      holder.current.dataset.routeMissingPairs = String(missingPairs);
      holder.current.dataset.routeLinkGaps = String(linkGaps);
      holder.current.dataset.routeMaxGapM = String(Math.round(maxGapM));
      holder.current.dataset.routeBelowBuildings = String(buildingIndex < 0 || routeIndex < buildingIndex);
    }
    setRouteNotes({ missingPairs, linkGaps, maxGapM });
  }, [journey]);

  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    drawRoute();

    // The basemap source can take tens of seconds to finish loading, and
    // `isStyleLoaded()` stays false until it does. Layers added before then are
    // dropped, so keep trying until the route is drawn and framed. Drive it
    // from the map's own events as well as a timer, and bound the total work.
    const attempt = () => {
      drawRoute();
      if (renderedKey.current !== null && framedKey.current !== null) {
        m.off("load", attempt);
        m.off("idle", attempt);
        window.clearInterval(timer);
      }
    };
    const timer = window.setInterval(attempt, 400);
    m.on("load", attempt);
    m.on("idle", attempt);
    const stop = window.setTimeout(() => {
      window.clearInterval(timer);
      m.off("load", attempt);
      m.off("idle", attempt);
    }, 120_000);

    return () => {
      window.clearInterval(timer);
      window.clearTimeout(stop);
      m.off("load", attempt);
      m.off("idle", attempt);
    };
  }, [drawRoute, ready]);

  // 2D / 3D presentation. 2D flattens the camera and hides the extrusion so
  // the route reads as a clean schematic; 3D tilts and shows the city.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    const apply = () => {
      if (!m.isStyleLoaded()) return;
      if (m.getLayer("city-3d")) {
        m.setLayoutProperty(
          "city-3d",
          "visibility",
          mode === "3d" ? "visible" : "none"
        );
      }
      m.easeTo({
        pitch: mode === "3d" ? 52 : 0,
        bearing: mode === "3d" ? -22 : 0,
        duration: 700,
      });
    };
    apply();
    const timer = window.setInterval(() => {
      if (m.isStyleLoaded()) {
        apply();
        window.clearInterval(timer);
      }
    }, 400);
    return () => window.clearInterval(timer);
  }, [mode, ready]);

  // Origin and destination markers.
  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    if (!m.isStyleLoaded()) return;
    for (const id of ["pin-o", "pin-d"]) {
      if (m.getLayer(id)) m.removeLayer(id);
      if (m.getSource(id)) m.removeSource(id);
    }
    const add = (id: string, pt: { lat: number; lon: number } | null, color: string) => {
      if (!pt) return;
      m.addSource(id, {
        type: "geojson",
        data: {
          type: "Feature",
          properties: {},
          geometry: { type: "Point", coordinates: [pt.lon, pt.lat] },
        },
      });
      m.addLayer({
        id,
        type: "circle",
        source: id,
        paint: {
          "circle-radius": 7,
          "circle-color": color,
          "circle-stroke-width": 3,
          "circle-stroke-color": "#06070a",
        },
      });
    };
    add("pin-o", origin, "#7aa8ff");
    add("pin-d", destination, "#eef1f6");
  }, [origin, destination, ready]);

  return (
    <div className="relative h-full w-full">
      <div
        ref={holder}
        className="h-full w-full"
        role="img"
        aria-label="Map of the planned bus journey"
      />
      {(routeNotes.missingPairs > 0 || routeNotes.linkGaps > 0) && (
        <div
          className={
            routeNotes.missingPairs > 0
              ? "pointer-events-none absolute bottom-3 left-3 z-10 max-w-sm rounded-lg border border-amber-300/30 bg-black/80 px-3 py-2 text-xs text-amber-100 shadow-lg"
              : "pointer-events-none absolute bottom-3 left-3 z-10 max-w-sm rounded-lg border border-line-soft bg-black/70 px-3 py-2 text-xs text-text-3 shadow-lg"
          }
        >
          {routeNotes.missingPairs > 0 && (
            <p>
              No road geometry for {routeNotes.missingPairs} stretch
              {routeNotes.missingPairs === 1 ? "" : "es"}; those sections are omitted from the map.
            </p>
          )}
          {routeNotes.linkGaps > 0 && (
            <p className={routeNotes.missingPairs > 0 ? "mt-1" : ""}>
              {routeNotes.linkGaps} short gap{routeNotes.linkGaps === 1 ? "" : "s"} up to{" "}
              {Math.round(routeNotes.maxGapM)} m where two mapped road fragments meet; not drawn as road.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
