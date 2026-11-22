"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

/**
 * Light / dark switch.
 *
 * The choice is written to `data-theme` on <html>, which is what the CSS
 * variables key off, and remembered in localStorage. The initial value is
 * applied before paint by the inline script in layout.tsx, so there is no
 * flash of the wrong theme.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const current = document.documentElement.dataset.theme;
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  const apply = (next: Theme) => {
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* storage can be blocked; the attribute still applies */
    }
    window.dispatchEvent(new CustomEvent("themechange", { detail: next }));
  };

  return (
    <div
      className="glass flex items-center gap-1 p-1"
      role="group"
      aria-label="Colour theme"
    >
      {(["light", "dark"] as const).map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => apply(t)}
          aria-pressed={theme === t}
          className={`mono px-2.5 py-1 text-[10px] uppercase tracking-wider transition-colors ${
            theme === t ? "bg-text text-ink" : "text-text-3 hover:text-text-2"
          }`}
        >
          {t}
        </button>
      ))}
    </div>
  );
}
