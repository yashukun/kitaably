import type { Metadata } from "next";
import { Fraunces, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

// Fraunces carries the personality: a serif with optical sizing and a soft "wonk"
// axis, bookish without being the high-contrast display face every editorial page
// reaches for. Used only for headings, never for running text.
const fraunces = Fraunces({
  variable: "--font-fraunces",
  subsets: ["latin"],
  axes: ["SOFT", "WONK", "opsz"],
});

// Plex Sans reads well at length, which matters when the main thing on screen is a
// paragraph someone is trying to learn from.
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

// The utility face, for the things that are data rather than prose: join codes,
// page numbers, ingest status, request ids.
const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: {
    default: process.env.NEXT_PUBLIC_APP_NAME ?? "Kitaably",
    template: `%s · ${process.env.NEXT_PUBLIC_APP_NAME ?? "Kitaably"}`,
  },
  description: "Ask the books your class is actually taught from.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${fraunces.variable} ${plexSans.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="relative min-h-full flex flex-col">
        <div className="relative z-10 flex flex-1 flex-col">{children}</div>
      </body>
    </html>
  );
}
