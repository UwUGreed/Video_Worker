// Playwright records the scroll directly; ffmpeg only muxes the final audio track.
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");

function splitStory(input) {
  const source = String(input || "");
  const body = source.trim();
  const bodyLines = [];

  for (const rawLine of body.split(/\r?\n/)) {
    const cleaned = rawLine.replace(/^\s*>\s?/, "").trim();
    if (!cleaned) {
      bodyLines.push(">");
      continue;
    }

    const sentences = cleaned
      .split(/(?<=[.!?])\s+/)
      .map((part) => part.trim())
      .filter(Boolean);

    for (const sentence of (sentences.length ? sentences : [cleaned])) {
      bodyLines.push(`>${sentence}`);
    }
  }

  return { bodyLines };
}

async function waitForMediaReady(page) {
  await page.waitForLoadState("load");
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }

    const images = Array.from(document.images || []);
    await Promise.all(images.map(async (img) => {
      if (!img.currentSrc && !img.src) return;
      if (img.complete) return;
      await new Promise((resolve) => {
        const done = () => resolve();
        img.addEventListener("load", done, { once: true });
        img.addEventListener("error", done, { once: true });
      });
    }));
  });
}

async function scrollPage(page, durationSeconds) {
  await page.evaluate(async (durationMs) => {
    window.scrollTo(0, 0);
    const maxScroll = Math.max(document.body.scrollHeight - window.innerHeight, 0);
    const start = performance.now();

    await new Promise((resolve) => {
      function step(now) {
        const elapsed = now - start;
        const progress = durationMs <= 0 ? 1 : Math.min(elapsed / durationMs, 1);
        window.scrollTo(0, maxScroll * progress);
        if (progress >= 1) {
          resolve();
          return;
        }
        requestAnimationFrame(step);
      }

      requestAnimationFrame(step);
    });
  }, Math.max(durationSeconds, 0) * 1000);
}

(async () => {
  const outDir = process.argv[2];
  const text = process.argv[3] || "";
  const audioDurationSeconds = Number(process.argv[4] || "0");
  const imageArg = process.argv[5] || "";

  if (!outDir) {
    console.error("Usage: node render.js <outDir> <text> <audioDurationSeconds> [imagePath]");
    process.exit(1);
  }

  fs.mkdirSync(outDir, { recursive: true });

  const templateUrl = pathToFileURL(path.join(__dirname, "template.html")).href;
  const imageUrl = imageArg && fs.existsSync(imageArg) ? pathToFileURL(path.resolve(imageArg)).href : "";
  const { bodyLines } = splitStory(text);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1080, height: 720 },
    recordVideo: {
      dir: outDir,
      size: { width: 1080, height: 720 }
    }
  });

  const page = await context.newPage();

  try {
    await page.goto(templateUrl, { waitUntil: "load" });
    await page.evaluate(({ lines, imageSrc }) => {
      const body = document.getElementById("body");
      const postImage = document.getElementById("post-image");
      const placeholder = document.getElementById("image-placeholder");

      body.innerHTML = "";
      for (const line of lines) {
        const span = document.createElement("span");
        span.className = "line";
        span.textContent = line;
        body.appendChild(span);
      }

      if (imageSrc && postImage) {
        postImage.src = imageSrc;
        postImage.style.display = "block";
        if (placeholder) placeholder.style.display = "none";
      }
    }, { lines: bodyLines, imageSrc: imageUrl });

    await waitForMediaReady(page);
    await scrollPage(page, audioDurationSeconds);
    await page.close();
    await context.close();
    await browser.close();
  } catch (error) {
    await browser.close().catch(() => {});
    throw error;
  }

  const recordedFile = fs.readdirSync(outDir)
    .filter((name) => name.toLowerCase().endsWith(".webm"))
    .sort()
    .map((name) => path.join(outDir, name))[0];

  if (!recordedFile) {
    console.error("scroll video not created");
    process.exit(2);
  }

  const finalPath = path.join(outDir, "scroll.webm");
  if (path.resolve(recordedFile) !== path.resolve(finalPath)) {
    if (fs.existsSync(finalPath)) fs.unlinkSync(finalPath);
    fs.renameSync(recordedFile, finalPath);
  }

  console.log(finalPath);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
