// Build the voice-view widget into an IIFE bundle (ext-apps App + render logic).
// Run from a directory that has tsup + @modelcontextprotocol/ext-apps in node_modules
// (e.g. the sticker-mcp project). Entry/out use absolute paths so cwd only matters
// for module resolution.
import { build } from "tsup";

const SRC = process.env.VOICE_WIDGET_SRC || "/root/voice-mcp/widget-src/voice-view-widget.ts";
const OUT = process.env.VOICE_WIDGET_OUT || "/root/voice-mcp/dist/widget";

await build({
  entry: [SRC],
  outDir: OUT,
  format: ["iife"],
  globalName: "VoiceViewWidget",
  platform: "browser",
  target: "es2020",
  bundle: true,
  minify: true,
  sourcemap: false,
  clean: true,
  dts: false,
  splitting: false,
  outExtension: () => ({ js: ".global.js" })
});

console.log("✓ voice widget built ->", OUT);
