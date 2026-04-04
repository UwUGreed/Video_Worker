const fs = require("fs");
const path = require("path");
const { execFileSync, spawnSync } = require("child_process");

const CANVAS_WIDTH = 1080;
const MIN_HEIGHT = 1920;
const MAX_HEIGHT = 60000;
const PAGE_BG = "#ffffee";
const POST_BG = "#f0e0d6";
const POST_BORDER = "#d9bfb7";
const NAME_COLOR = "#117743";
const META_COLOR = "#707070";
const TITLE_COLOR = "#0f0c5d";
const TEXT_COLOR = "#789922";
const FILE_COLOR = "#1f2937";
const PLACEHOLDER_FG = "#6b4f3c";
const FONT_FAMILY = "Liberation Sans, Arial, sans-serif";

function resolveMagick() {
  for (const candidate of ["magick", "convert"]) {
    const result = spawnSync("bash", ["-lc", `command -v ${candidate}`], { encoding: "utf8" });
    const resolved = (result.stdout || "").trim();
    if (result.status === 0 && resolved) return resolved;
  }
  throw new Error("ImageMagick not found. Install `magick` or `convert` and ensure it is on PATH.");
}

function escapeXml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function wrapText(text, maxChars) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  if (!words.length) return [""];

  const lines = [];
  let current = words[0];

  for (const word of words.slice(1)) {
    const candidate = `${current} ${word}`;
    if (candidate.length <= maxChars) {
      current = candidate;
    } else {
      lines.push(current);
      current = word;
    }
  }

  lines.push(current);
  return lines;
}

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

function buildImageHref(imagePath) {
  if (!imagePath || !fs.existsSync(imagePath)) return "";
  const ext = path.extname(imagePath).toLowerCase();
  const mimeByExt = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif"
  };
  const mime = mimeByExt[ext] || "image/png";
  const b64 = fs.readFileSync(imagePath).toString("base64");
  return `data:${mime};base64,${b64}`;
}

function buildSvg({ bodyLines, imageHref }) {
  const pagePaddingX = 28;
  const pagePaddingTop = 40;
  const pagePaddingBottom = 120;
  const boardBarHeight = 62;
  const boardBarGap = 18;
  const postX = pagePaddingX;
  const postY = pagePaddingTop + boardBarHeight + boardBarGap;
  const postWidth = CANVAS_WIDTH - (pagePaddingX * 2);
  const postPadX = 22;
  const postPadTop = 22;
  const postPadBottom = 30;
  const metaHeight = 34;
  const mediaWidth = 240;
  const mediaGap = 22;
  const fileLineHeight = 28;
  const thumbSize = 240;
  const bodyFontSize = 34;
  const bodyLineHeight = 46;
  const bodyStartY = postY + postPadTop + metaHeight + 6;
  const mediaBottom = bodyStartY + fileLineHeight + 8 + thumbSize;
  const textX = postX + postPadX;
  const textWidthFull = postWidth - (postPadX * 2);
  const textWidthNarrow = textWidthFull - mediaWidth - mediaGap;
  const narrowChars = 38;
  const fullChars = 48;

  const laidOutLines = [];
  let cursorY = bodyStartY + bodyFontSize;

  for (const line of bodyLines) {
    const useNarrow = cursorY <= mediaBottom;
    const wrapped = wrapText(line, useNarrow ? narrowChars : fullChars);
    for (const wrappedLine of wrapped) {
      laidOutLines.push({
        x: useNarrow ? textX + mediaWidth + mediaGap : textX,
        y: cursorY,
        text: wrappedLine
      });
      cursorY += bodyLineHeight;
    }
  }

  const contentBottom = Math.max(cursorY, mediaBottom + 16);
  const rawHeight = contentBottom - postY + postPadBottom + pagePaddingBottom;
  const baseHeight = Math.max(MIN_HEIGHT, rawHeight);
  const outputHeight = Math.min(baseHeight, MAX_HEIGHT);

  const lineNodes = laidOutLines.map((line) => (
    `<text x="${line.x}" y="${line.y}" font-size="${bodyFontSize}" fill="${TEXT_COLOR}">${escapeXml(line.text)}</text>`
  )).join("");

  const imageNodes = imageHref ? (
    `<image x="${textX}" y="${bodyStartY + fileLineHeight + 8}" width="${thumbSize}" height="${thumbSize}" href="${imageHref}" preserveAspectRatio="xMidYMid slice" />`
  ) : (
    `<rect x="${textX}" y="${bodyStartY + fileLineHeight + 8}" width="${thumbSize}" height="${thumbSize}" fill="#edd9c8" />
     <text x="${textX + (thumbSize / 2)}" y="${bodyStartY + fileLineHeight + 130}" text-anchor="middle" font-size="22" font-weight="700" fill="${PLACEHOLDER_FG}">IMAGE</text>
     <text x="${textX + (thumbSize / 2)}" y="${bodyStartY + fileLineHeight + 160}" text-anchor="middle" font-size="22" font-weight="700" fill="${PLACEHOLDER_FG}">PLACEHOLDER</text>`
  );

  return `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${CANVAS_WIDTH}" height="${outputHeight}" viewBox="0 0 ${CANVAS_WIDTH} ${outputHeight}">
  <rect width="100%" height="100%" fill="${PAGE_BG}" />
  <g>
    <rect x="${pagePaddingX}" y="${pagePaddingTop}" width="${postWidth}" height="${boardBarHeight}" fill="#f0ddcb" stroke="#c5cedf" />
    <text x="${pagePaddingX + 20}" y="${pagePaddingTop + 40}" font-family="${FONT_FAMILY}" font-size="30" font-weight="700" fill="#800000">/x/ - auto thread capture</text>

    <rect x="${postX}" y="${postY}" width="${postWidth}" height="${outputHeight - postY - pagePaddingBottom}" fill="${POST_BG}" stroke="${POST_BORDER}" />

    <text x="${postX + postPadX}" y="${postY + 32}" font-family="${FONT_FAMILY}" font-size="28" font-weight="700" fill="${NAME_COLOR}">Anonymous</text>
    <text x="${postX + postPadX + 530}" y="${postY + 32}" font-family="${FONT_FAMILY}" font-size="28" fill="${META_COLOR}">04/02/26(Fri)12:00:00 No.427001337</text>

    <text x="${textX}" y="${bodyStartY + 20}" font-family="${FONT_FAMILY}" font-size="20" fill="${FILE_COLOR}">File: ${imageHref ? "upload" : "placeholder"}.jpg (280 KB, 280x280)</text>
    <rect x="${textX}" y="${bodyStartY + fileLineHeight + 8}" width="${thumbSize}" height="${thumbSize}" fill="none" stroke="#b99987" />
    ${imageNodes}

    <g font-family="${FONT_FAMILY}" font-weight="400">
      ${lineNodes}
    </g>
  </g>
</svg>`;
}

(async () => {
  const outDir = process.argv[2];
  const text = process.argv[3] || "";
  const imageArg = process.argv[4] || "";

  if (!outDir) {
    console.error("Usage: node render.js <outDir> <text> [imagePath]");
    process.exit(1);
  }

  fs.mkdirSync(outDir, { recursive: true });

  const outPng = path.join(outDir, "story.png");
  const tmpSvg = path.join(outDir, "story.svg");
  const magickBin = resolveMagick();
  const imageHref = buildImageHref(imageArg);
  const { bodyLines } = splitStory(text);
  const svg = buildSvg({ bodyLines, imageHref });

  fs.writeFileSync(tmpSvg, svg, "utf8");

  try {
    execFileSync(magickBin, [tmpSvg, "PNG32:" + outPng], { stdio: "pipe" });
  } finally {
    if (fs.existsSync(tmpSvg)) fs.unlinkSync(tmpSvg);
  }

  if (!fs.existsSync(outPng)) {
    console.error("story.png not created");
    process.exit(2);
  }

  console.log(outPng);
})();
