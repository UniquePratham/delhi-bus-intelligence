import type { Metadata, Viewport } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const display = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-display",
  display: "swap",
});

const sans = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Delhi Bus Intelligence",
  description:
    "Travel times learned from the bus fleet's own GPS, and crowding learned from ticketing. A journey planner for Delhi, built for Transport Stack Hackathon 2026.",
  applicationName: "Delhi Bus Intelligence",
  openGraph: {
    title: "Delhi Bus Intelligence",
    description:
      "Predict bus travel time by hour of week, plan journeys, and see crowding on every leg.",
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#06070a",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
