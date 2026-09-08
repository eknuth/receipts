// The only two typefaces in this cut, per the design direction: JetBrains
// Mono for receipt text and captions' index marks, IBM Plex Sans for caption
// text and any full sentence set on a card. No Inter, no Roboto, no
// system-ui anywhere.
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import { loadFont as loadSans } from "@remotion/google-fonts/IBMPlexSans";

export const mono = loadMono("normal", {
  weights: ["400", "500", "600", "700"],
  subsets: ["latin"],
});

export const sans = loadSans("normal", {
  weights: ["400", "500", "600"],
  subsets: ["latin"],
});

export const FONT_MONO = mono.fontFamily;
export const FONT_SANS = sans.fontFamily;
