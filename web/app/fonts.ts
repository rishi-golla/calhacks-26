import { Archivo, Doto, Inter, Inter_Tight, JetBrains_Mono } from "next/font/google";

/** Display: condensed, uppercase, industrial (Senra / Edgerun tone). */
export const display = Archivo({
  subsets: ["latin"],
  axes: ["wdth"],
  variable: "--ff-display",
  display: "swap",
});

/** Labels / chips / nav: dot-matrix pixel face Senra uses for labels. */
export const label = Doto({
  subsets: ["latin"],
  weight: "variable",
  variable: "--ff-label",
  display: "swap",
});

/** Numbers and tables (Edgerun uses JetBrains Mono). */
export const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--ff-mono",
  display: "swap",
});

/** Body copy. */
export const body = Inter({
  subsets: ["latin"],
  variable: "--ff-body",
  display: "swap",
});

/** Headline face for the glass theme. */
export const tight = Inter_Tight({
  subsets: ["latin"],
  weight: ["500", "600"],
  variable: "--ff-tight",
  display: "swap",
});

export const fontClassNames = [display.variable, label.variable, mono.variable, body.variable, tight.variable].join(" ");
