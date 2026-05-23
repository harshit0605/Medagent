import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Medagent Ops Console",
  description: "Internal operations dashboard for the Medagent care concierge.",
};

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/inbox", label: "Inbox" },
  { href: "/clinical-alerts", label: "Alerts" },
  { href: "/patients", label: "Patients" },
  { href: "/prescriptions", label: "Prescriptions" },
  { href: "/tickets", label: "Tickets" },
  { href: "/doctors", label: "Doctors" },
  { href: "/care-plans", label: "Care plans" },
  { href: "/cohort-tags", label: "Cohort tags" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/analytics", label: "Analytics" },
  { href: "/health", label: "Health" },
  { href: "/audit-search", label: "Audit" },
  { href: "/route-tester", label: "Route Tester" },
];

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        <header className="border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900">
          <div className="max-w-6xl mx-auto px-6 py-3 flex items-center gap-6">
            <Link href="/" className="font-semibold tracking-tight">
              Medagent <span className="text-zinc-400">/ ops</span>
            </Link>
            <nav className="flex gap-4 text-sm">
              {NAV.map(({ href, label }) => (
                <Link
                  key={href}
                  href={href}
                  className="text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
                >
                  {label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">{children}</main>
        <footer className="border-t border-zinc-200 dark:border-zinc-800 text-xs text-zinc-500 dark:text-zinc-500">
          <div className="max-w-6xl mx-auto px-6 py-3">
            Internal tool · server-rendered via Next.js
          </div>
        </footer>
      </body>
    </html>
  );
}
