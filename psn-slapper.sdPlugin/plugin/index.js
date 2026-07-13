const http = require("http");
const { StreamDock } = require("./streamdock");
const { makeCanvas, text, rect, pngDataUri } = require("./canvas");
const IDLE_ICON = require("./icon");

const PSN_API_URL = process.env.PSN_MESSENGER_URL || "http://192.168.5.54:3021";

const MESSAGES = {
  "com.psn.slapper.haveyouever": "Have you ever? 👉👌",
  "com.psn.slapper.icedcap": "🧊☕ Iced Cap STORRYYY! 📖✨",
  "com.psn.slapper.waterbreak": "💧 Water break! 🚰💦",
  "com.psn.slapper.never": "Never 🙅‍♂️❌",
};

const W = 126, H = 126;
const sd = new StreamDock();
const actionMap = new Map(); // context -> action UUID

function renderIdle() {
  return IDLE_ICON;
}

async function renderIdleIcedCap() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#1a3a5c");
  text(ctx, "🧊☕", 63, 45, 16, "#ffffff", "center");
  text(ctx, "Iced Cap", 63, 75, 10, "#ffffff", "center", "DeckBold");
  text(ctx, "STORRYYY!", 63, 95, 9, "#FFC107", "center", "DeckBold");
  return pngDataUri(img);
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

function sendPsnMessage(message) {
  return new Promise((resolve, reject) => {
    const url = new URL("/v2/send", PSN_API_URL);
    const body = JSON.stringify({ message });
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

async function renderIdleWaterBreak() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#0d47a1");
  text(ctx, "💧🚰", 63, 45, 16, "#ffffff", "center");
  text(ctx, "Water", 63, 75, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "Break!", 63, 95, 11, "#81D4FA", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderIdleNever() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#4a0000");
  text(ctx, "🙅‍♂️", 63, 50, 18, "#ffffff", "center");
  text(ctx, "Never", 63, 90, 14, "#ff5252", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderIdleRoastStart() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#b71c1c");
  text(ctx, "🔥", 63, 45, 20, "#ffffff", "center");
  text(ctx, "START", 63, 78, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "ROAST", 63, 98, 11, "#FFC107", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderIdleRoastStop() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#1a1a2e");
  text(ctx, "🛑", 63, 45, 20, "#ffffff", "center");
  text(ctx, "STOP", 63, 78, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "ROAST", 63, 98, 11, "#888888", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderIdleRoastNow() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#ff6f00");
  text(ctx, "💀", 63, 45, 20, "#ffffff", "center");
  text(ctx, "ROAST", 63, 78, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "NOW", 63, 98, 11, "#ffffff", "center", "DeckBold");
  return pngDataUri(img);
}

async function getIdleImage(action) {
  if (action === "com.psn.slapper.icedcap") return renderIdleIcedCap();
  if (action === "com.psn.slapper.waterbreak") return renderIdleWaterBreak();
  if (action === "com.psn.slapper.never") return renderIdleNever();
  if (action === "com.psn.slapper.roaststart") return renderIdleRoastStart();
  if (action === "com.psn.slapper.roaststop") return renderIdleRoastStop();
  if (action === "com.psn.slapper.roastnow") return renderIdleRoastNow();
  return renderIdle();
}

function callEndpoint(path) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, PSN_API_URL);
    const req = http.request(url, { method: "POST", timeout: 10000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => data += chunk);
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(data);
        else reject(new Error(`HTTP ${res.statusCode}: ${data}`));
      });
    });
    req.on("error", reject);
    req.end();
  });
}

async function handlePress(context) {
  const action = actionMap.get(context) || "com.psn.slapper.haveyouever";

  // Roast buttons call different endpoints
  if (action === "com.psn.slapper.roaststart") {
    sd.setImage(context, await renderSending());
    try {
      await callEndpoint("/roast/start");
      sd.setImage(context, await renderSent());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 2000);
    } catch (e) {
      sd.setImage(context, await renderError());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 3000);
    }
    return;
  }

  if (action === "com.psn.slapper.roaststop") {
    sd.setImage(context, await renderSending());
    try {
      await callEndpoint("/roast/stop");
      sd.setImage(context, await renderSent());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 2000);
    } catch (e) {
      sd.setImage(context, await renderError());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 3000);
    }
    return;
  }

  if (action === "com.psn.slapper.roastnow") {
    sd.setImage(context, await renderSending());
    try {
      await callEndpoint("/roast/once");
      sd.setImage(context, await renderSent());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 2000);
    } catch (e) {
      sd.setImage(context, await renderError());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 3000);
    }
    return;
  }

  // Regular message buttons
  const message = MESSAGES[action] || MESSAGES["com.psn.slapper.haveyouever"];
  sd.setImage(context, await renderSending());

  try {
    await sendPsnMessage(message);
    sd.setImage(context, await renderSent());
    setTimeout(async () => {
      sd.setImage(context, await getIdleImage(action));
    }, 2000);
  } catch (e) {
    console.error("PSN send failed:", e.message);
    sd.setImage(context, await renderError());
    setTimeout(async () => {
      sd.setImage(context, await getIdleImage(action));
    }, 3000);
  }
}

sd.on("willAppear", async (msg) => {
  actionMap.set(msg.context, msg.action);
  sd.setImage(msg.context, await getIdleImage(msg.action));
  sd.setTitle(msg.context, "");
});

sd.on("willDisappear", (msg) => {
  actionMap.delete(msg.context);
});

sd.on("keyDown", (msg) => {
  handlePress(msg.context);
});

if (require.main === module) sd.connect();
