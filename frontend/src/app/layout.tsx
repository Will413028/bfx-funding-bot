import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "BFX Funding Bot",
  description: "Bitfinex 自動放貸 SaaS 平台",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
