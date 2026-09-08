import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ZUMVIA",
  icons: { icon: "/favicon.svg", apple: "/logo-full.svg" },
  description: "Zero-Hallucination Neuro-Symbolic Quantitative Trading Platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="tr">
      <body>{children}</body>
    </html>
  );
}
