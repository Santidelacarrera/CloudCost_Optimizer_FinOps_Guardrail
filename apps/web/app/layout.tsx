import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { headers } from "next/headers";
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

// Render dinámico en cada petición: el nonce de la CSP cambia con cada una y Next lo inserta en sus scripts al renderizar.
// Leer las cabeceras (y marcarlo explícitamente) saca a todas las páginas de la generación estática.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: ReactNode }) {
  await headers();
  return (
    <html lang="es" className={`${display.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
