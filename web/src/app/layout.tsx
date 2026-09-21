import type { Metadata, Viewport } from "next";
import { Be_Vietnam_Pro, IBM_Plex_Mono } from "next/font/google";
import type { ReactNode } from "react";

import { Providers } from "./providers";
import { AppShell } from "@/components/shell/app-shell";
import "./globals.css";

/**
 * Be Vietnam Pro is drawn for Vietnamese. This app is entirely in Vietnamese
 * and its tables are dense, so stacked diacritics (ế ự ỗ ằ) have to stay legible
 * and correctly positioned at 13px - which is where the usual dashboard faces
 * fall down. IBM Plex Mono carries the identifiers: full names, privilege codes
 * and event ids, all of which get compared character by character against
 * Catalog Explorer.
 *
 * next/font self-hosts both at build time, so the deployed app makes no request
 * to a font CDN. That matters in a locked-down workspace where egress to
 * fonts.googleapis.com may simply be blocked.
 */
const sans = Be_Vietnam_Pro({
  subsets: ["latin", "vietnamese"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
  variable: "--font-sans",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Quản trị Unity Catalog",
  description:
    "Tìm tài sản dữ liệu, xem ai có quyền gì và quyền đến từ đâu, rồi thay đổi quyền một cách có kiểm soát.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Never disable zoom: operators read long identifiers on small screens.
  maximumScale: 5,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi" className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        {/*
          Theme is resolved before first paint so a dark-mode user never sees a
          white flash. Kept inline and tiny on purpose; it runs before React.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var s=localStorage.getItem("ucg-theme");var d=s?s==="dark":window.matchMedia("(prefers-color-scheme: dark)").matches;if(d)document.documentElement.classList.add("dark")}catch(e){}})();`,
          }}
        />
      </head>
      <body className="font-sans antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:text-primary-ink"
        >
          Bỏ qua điều hướng
        </a>
        <Providers>
          <AppShell>
            <div id="main">{children}</div>
          </AppShell>
        </Providers>
      </body>
    </html>
  );
}
