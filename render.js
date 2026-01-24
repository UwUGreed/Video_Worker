const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

(async () => {
  const outDir = process.argv[2];
  const text = process.argv.slice(3).join(" ") || "";

  if (!outDir) {
    console.error("Usage: node render.js <outDir> <text...>");
    process.exit(1);
  }

  fs.mkdirSync(outDir, { recursive: true });

  const templatePath = path.join(__dirname, "template.html");
  const html = fs.readFileSync(templatePath, "utf8");

  const outPng = path.join(outDir, "story.png");

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 } });

  await page.setContent(html, { waitUntil: "load" });

  // Title/body logic:
  // - If text contains a newline: first line becomes title, rest becomes body
  // - Else: title stays "Story", whole text is body
  await page.evaluate((t) => {
    const s = String(t || "");
    const hasNL = /\r?\n/.test(s);

    let title = "Story";
    let body = s;

    if (hasNL) {
      const parts = s.split(/\r?\n/);
      const first = (parts.shift() || "").trim();
      title = first || "Story";
      body = parts.join("\n").trim();
    }

    const h = document.querySelector("#title");
    if (h) h.textContent = title;

    const el = document.querySelector("#body");
    if (el) el.textContent = body;
  }, text);

  await page.waitForTimeout(200);

  await page.screenshot({ path: outPng, fullPage: true });

  await browser.close();

  if (!fs.existsSync(outPng)) {
    console.error("story.png not created");
    process.exit(2);
  }

  console.log(outPng);
})();
