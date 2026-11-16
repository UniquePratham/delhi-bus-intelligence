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

export default function Map3D({
  journey,
  origin,
  destination,
  onLoaded,
}: {
  journey: Journey | null;
  origin: { lat: number; lon: number } | null;
  destination: { lat: number; lon: number } | null;
  onLoaded?: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const map = useRef<MlMap | null>(null);
  const [ready, setReady] = useState(false);

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
                  12, ["*", 1.6, ["coalesce", ["get", "height"], 8]],
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

  const drawRoute = useCallback(() => {
    const m = map.current;
    if (!m) return;
    // `isStyleLoaded()` is false while basemap tiles are still arriving, which
    // is exactly when the first journey lands. Bail out and let the idle
    // listener retry rather than dropping the route for good.
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
    for (const leg of journey.legs) {
      if (leg.fromStation === null || leg.toStation === null) continue;

      // Walk the ride's stop sequence and draw each consecutive pair along its
      // observed road geometry. A pair with no trace falls back to the chord,
      // so the line is always continuous.
      const stops = routePath(leg.routeId, leg.fromStation, leg.toStation);
      const seq = stops.length >= 2 ? stops : [leg.fromStation, leg.toStation];
      const path: [number, number][] = [];
      for (let i = 0; i < seq.length - 1; i++) {
        const a = seq[i];
        const b = seq[i + 1];
        const geom = pairGeom(a, b);
        if (geom.length >= 2) {
          // Skip the first point on continuation to avoid duplicate vertices.
          for (let k = path.length ? 1 : 0; k < geom.length; k++) path.push(geom[k]);
        } else {
          const ca = coord(a);
          const cb = coord(b);
          if (ca && cb) {
            if (!path.length) path.push(ca);
            path.push(cb);
          }
        }
      }
      if (path.length < 2) continue;

      segments.push(path);
      // Mark the stations the ride actually calls at, not every road vertex.
      for (const sid of seq) {
        const c = coord(sid);
        if (!c) continue;
        points.push({
          type: "Feature",
          properties: { band: leg.crowding?.band ?? "unknown" },
          geometry: { type: "Point", coordinates: c },
        });
      }
    }
    if (!segments.length) return;

    m.addSource("rt", {
      type: "geojson",
      data: {
        type: "Feature",
        properties: {},
        geometry: { type: "MultiLineString", coordinates: segments },
      },
    });
    m.addLayer({
      id: "rt-glow",
      type: "line",
      source: "rt",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#7aa8ff",
        "line-width": 26,
        "line-opacity": 0.2,
        "line-blur": 20,
      },
    });
    m.addLayer({
      id: "rt-line",
      type: "line",
      source: "rt",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#e2ecff", "line-width": 4, "line-opacity": 1 },
    });

    m.addSource("rtp", {
      type: "geojson",
      data: { type: "FeatureCollection", features: points },
    });
    m.addLayer({
      id: "rt-pts",
      type: "circle",
      source: "rtp",
      paint: {
        "circle-radius": 5,
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
    });

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
    }
    m.setMinZoom(11);
    renderedKey.current = key;
  }, [journey]);

  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    drawRoute();
    // The style is often still loading when the first journey arrives, and
    // `idle` will not fire again once the map is already idle. Poll until the
    // route is actually rendered, then stop. `renderedKey` makes each attempt
    // a no-op once it has succeeded, and the attempt cap bounds the work.
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      drawRoute();
      if (renderedKey.current !== null || attempts > 60) {
        window.clearInterval(timer);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, [drawRoute, ready]);

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
    <div
      ref={holder}
      className="h-full w-full"
      role="img"
      aria-label="3D map of the planned bus journey"
    />
  );
}
