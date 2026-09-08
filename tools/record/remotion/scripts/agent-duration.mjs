#!/usr/bin/env node
// Reads the investigation clip's true duration with ffprobe, the same tool
// build.py's own duration() helper uses, and prints it as a JSON props
// fragment. Remotion's bundler has no Node built-ins (see Root.tsx), so this
// runs outside it, as a plain Node script, and its result is merged into
// --props by render.sh.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const file = path.join(here, "..", "public", "03-agent.mp4");

const out = execFileSync("ffprobe", [
  "-v",
  "error",
  "-show_entries",
  "format=duration",
  "-of",
  "csv=p=0",
  file,
]).toString();

const agentDurationS = Number.parseFloat(out.trim());
if (!Number.isFinite(agentDurationS) || agentDurationS <= 0) {
  console.error(`agent-duration.mjs: could not read a duration for ${file}: ${out}`);
  process.exit(1);
}

process.stdout.write(JSON.stringify({ agentDurationS }));
