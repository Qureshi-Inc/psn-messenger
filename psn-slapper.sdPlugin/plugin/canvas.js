const path = require("path");
const { Writable } = require("stream");
const PImage = require("pureimage");

let _fontsLoaded = false;
function ensureFonts() {
  if (_fontsLoaded) return;
  const reg = PImage.registerFont(path.join(__dirname, "fonts", "DejaVuSans.ttf"), "Deck");
  reg.loadSync();
  try {
    const bold = PImage.registerFont(path.join(__dirname, "fonts", "DejaVuSans-Bold.ttf"), "DeckBold");
    bold.loadSync();
  } catch { }
  _fontsLoaded = true;
}

function makeCanvas(w, h) {
  ensureFonts();
  const img = PImage.make(w, h);
  const ctx = img.getContext("2d");
  return { img, ctx };
}

function text(ctx, str, x, y, ptSize, color, align = "left", family = "Deck") {
  ctx.font = `${ptSize}pt ${family}`;
  ctx.fillStyle = color;
  let tx = x;
  if (align !== "left") {
    const m = ctx.measureText(String(str));
    tx = align === "right" ? x - m.width : x - m.width / 2;
  }
  ctx.fillText(String(str), tx, y);
}

function rect(ctx, x, y, w, h, color) {
  ctx.fillStyle = color;
  ctx.fillRect(x, y, w, h);
}

async function pngDataUri(img) {
  const chunks = [];
  const sink = new Writable({
    write(chunk, _enc, cb) { chunks.push(chunk); cb(); },
  });
  await new Promise((resolve, reject) => {
    sink.on("finish", resolve);
    sink.on("error", reject);
    PImage.encodePNGToStream(img, sink).catch(reject);
  });
  const buf = Buffer.concat(chunks);
  return `data:image/png;base64,${buf.toString("base64")}`;
}

module.exports = { makeCanvas, text, rect, pngDataUri };
