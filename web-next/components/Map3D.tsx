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
function routePath(routeId: number | null, from: number, to: number): [number, number][] {
  if (routeId === null || !ROUTE_PATH) return [];
  const ids = ROUTE_PATH(routeId, from, to);
  const out: [number, number][] = [];
  for (const id of ids) {
    const c = coord(id);
    if (c) out.push(c);
  }
  return out;
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
  const drawRoute = useCallback(() => {
    const m = map.current;
    if (!m || !m.isStyleLoaded()) return;

    for (const id of ["rt-glow", "rt-line", "rt-pts"]) {
      if (m.getLayer(id)) m.removeLayer(id);
    }
    for (const id of ["rt", "rtp"]) {
      if (m.getSource(id)) m.removeSource(id);
    }
    if (!journey) return;

    const segments: [number, number][][] = [];
    const points: GeoJSON.Feature[] = [];
    for (const leg of journey.legs) {
      if (leg.fromStation === null || leg.toStation === null) continue;
      // Draw the full stop sequence of the ride, not just its endpoints, so
      // the line follows the route instead of cutting across the city.
      const path = routePath(leg.routeId, leg.fromStation, leg.toStation);
      if (path.length >= 2) {
        segments.push(path);
        for (const c of path) {
          points.push({
            type: "Feature",
            properties: { band: leg.crowding?.band ?? "unknown" },
            geometry: { type: "Point", coordinates: c },
          });
        }
      } else {
        const a = coord(leg.fromStation);
        const b = coord(leg.toStation);
        if (!a || !b) continue;
        segments.push([a, b]);
        points.push({
          type: "Feature",
          properties: { band: leg.crowding?.band ?? "unknown" },
          geometry: { type: "Point", coordinates: a },
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

    // Frame the route, but keep the camera close enough that the extruded
    // buildings are visible. A long trip would otherwise fit at zoom ~11,
    // below the extrusion's minzoom, and the city would look flat.
    const flat = segments.flat();
    const bounds = flat.reduce(
      (acc, c) => acc.extend(c),
      new maplibregl.LngLatBounds(flat[0], flat[0])
    );
    // A pitched camera compresses the lower half of the viewport, so the
    // bottom padding has to be larger than the top or the destination marker
    // falls off-screen.
    m.fitBounds(bounds, {
      padding: { top: 60, bottom: 170, left: 70, right: 70 },
      maxZoom: 13.4,
      duration: 900,
      pitch: 52,
      bearing: -22,
    });
    m.setMinZoom(11);
  }, [journey]);

  useEffect(() => {
    const m = map.current;
    if (!m || !ready) return;
    drawRoute();
    // The style can finish loading after the first draw. Redraw once, then
    // stop listening: an `idle` handler that redraws forever would remove and
    // re-add the layers on every frame.
    const once = () => {
      drawRoute();
      m.off("idle", once);
    };
    m.on("idle", once);
    return () => {
      m.off("idle", once);
    };
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
