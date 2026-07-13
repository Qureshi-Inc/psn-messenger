const http = require("http");
const { StreamDock } = require("./streamdock");
const { makeCanvas, text, rect, pngDataUri } = require("./canvas");
const IDLE_ICON = require("./icon");

const PSN_API_URL = process.env.PSN_MESSENGER_URL || "http://192.168.5.54:3021";
const MESSAGE = "Have you ever?";

const W = 126, H = 126;
const sd = new StreamDock();
const contexts = new Set();

function renderIdle() {
  return IDLE_ICON;
}

async function renderSending() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#1a1a2e");
  text(ctx, "Sending...", 63, 70, 10, "#FFC107", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderSent() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#1b5e20");
  text(ctx, "SENT!", 63, 70, 14, "#ffffff", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderError() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#b71c1c");
  text(ctx, "FAILED", 63, 70, 12, "#ffffff", "center", "DeckBold");
  return pngDataUri(img);
}

function sendPsnMessage() {
  return new Promise((resolve, reject) => {
    const url = new URL("/send", PSN_API_URL);
    const body = JSON.stringify({ message: MESSAGE });
    const req = http.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      timeout: 10000,
    }, (res) => {
      let data = "";
      res.on("data", (chunk) => data += chunk);
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(data);
        else reject(new Error(`HTTP ${res.statusCode}: ${data}`));
      });
    });
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

async function handlePress(context) {
  sd.setImage(context, await renderSending());

  try {
    await sendPsnMessage();
    sd.setImage(context, await renderSent());
    setTimeout(async () => {
      sd.setImage(context, await renderIdle());
    }, 2000);
  } catch (e) {
    console.error("PSN send failed:", e.message);
    sd.setImage(context, await renderError());
    setTimeout(async () => {
      sd.setImage(context, await renderIdle());
    }, 3000);
  }
}

sd.on("willAppear", async (msg) => {
  contexts.add(msg.context);
  sd.setImage(msg.context, await renderIdle());
  sd.setTitle(msg.context, "");
});

sd.on("willDisappear", (msg) => {
  contexts.delete(msg.context);
});

sd.on("keyDown", (msg) => {
  handlePress(msg.context);
});

if (require.main === module) sd.connect();
