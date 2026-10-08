import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kairox — time-travel debugging & recovery",
  description: "Detect, trace, replay and recover from failures in a controlled multi-service system.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
