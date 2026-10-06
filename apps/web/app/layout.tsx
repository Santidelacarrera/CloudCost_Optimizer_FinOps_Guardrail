import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import type { ReactNode } from "react";
import "./globals.css";

// Tipografías incluidas en el repositorio (licencia OFL): el build no depende de Google Fonts.
const display = localFont({ src: "./fonts/BricolageGrotesque.woff", variable: "--font-display", weight: "200 800", display: "swap" });
const body = localFont({ src: "./fonts/InstrumentSans.woff", variable: "--font-body", weight: "400 700", display: "swap" });

export const metadata: Metadata = {
  title: "CloudCost Optimizer",
  description: "Detecta desperdicio en tu nube y en tus gastos, entiende por qué y propone el cambio con aprobación humana.",
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { themeColor: "#eff5eb", colorScheme: "light" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="es" className={`${display.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
