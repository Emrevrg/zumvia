import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ZUMVIA",
  icons: { icon: "/favicon.ico", apple: "/logo-512.png" },
  description: "Zero-Hallucination Neuro-Symbolic Quantitative Trading Platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="tr">
      <body>{children}</body>
    </html>
  );
}
