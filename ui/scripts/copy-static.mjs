// Copies index.html, styles and the one runtime dependency into dist/ (no bundler needed:
// the browser loads native ES modules; an import map in index.html resolves "lightweight-charts").
import { cpSync, mkdirSync, existsSync } from "node:fs";

mkdirSync("dist/vendor", { recursive: true });
cpSync("index.html", "dist/index.html");
if (existsSync("styles")) cpSync("styles", "dist/styles", { recursive: true });
cpSync("node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.mjs",
       "dist/vendor/lightweight-charts.mjs");
console.log("static files copied to dist/");
