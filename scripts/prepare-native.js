const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const out = path.join(root, "www");
const files = [
  "index.html",
  "privacy.html",
  "accepted.html",
  "declined.html",
  "manifest.webmanifest",
  "icons"
];

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

for (const item of files) {
  const source = path.join(root, item);
  if (!fs.existsSync(source)) {
    if (item === "privacy.html" || item === "accepted.html" || item === "declined.html") continue;
    throw new Error("Required web app file is missing: " + item);
  }
  fs.cpSync(source, path.join(out, item), { recursive: true });
}

// A native WebView does not need the website's service worker. Remove only its
// registration snippet from the packaged copy; the hosted website is unchanged.
const indexPath = path.join(out, "index.html");
let html = fs.readFileSync(indexPath, "utf8");
html = html.replace(
  /<script>\s*if\s*\(\s*"serviceWorker"\s+in\s+navigator\s*\)\s*\{[\s\S]*?serviceWorker\.register\([\s\S]*?<\/script>/i,
  ""
);
fs.writeFileSync(indexPath, html, "utf8");
console.log("Prepared Flip Finder web files in ./www for Capacitor.");
console.log("This prepares source files only; Android/iOS builds still require their platform toolchains and device testing.");
