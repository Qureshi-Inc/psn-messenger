const http = require("http");
const { StreamDock } = require("./streamdock");
const { makeCanvas, text, rect, pngDataUri } = require("./canvas");
const IDLE_ICON = require("./icon");

// Each service is reachable two ways: opti3's Tailscale IP (works anywhere the
// tailnet is up) and its LAN IP (works at home even if Tailscale is down). We
// try them in order so a Tailscale hiccup doesn't kill the buttons when home.
// Override with a comma-separated list in the env var if the IPs ever change.
const PSN_API_URLS = (process.env.PSN_MESSENGER_URLS ||
  "http://100.123.228.75:3021,http://192.168.5.54:3021").split(",").map(s => s.trim()).filter(Boolean);
const WHATSAPP_API_URLS = (process.env.WHATSAPP_API_URLS ||
  "http://100.123.228.75:3100,http://192.168.5.54:3100").split(",").map(s => s.trim()).filter(Boolean);

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

// POST to `path` against one base URL. Resolves body on 2xx, rejects otherwise.
function postOnce(base, path, bodyObj) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, base);
    const body = bodyObj ? JSON.stringify(bodyObj) : "";
    const opts = { method: "POST", timeout: 8000, headers: {} };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["Content-Length"] = Buffer.byteLength(body);
    }
    const req = http.request(url, opts, (res) => {
      let data = "";
      res.on("data", (chunk) => data += chunk);
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(data);
        else reject(new Error(`HTTP ${res.statusCode}: ${data}`));
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
    if (body) req.write(body);
    req.end();
  });
}

// Try each base URL in order (Tailscale, then LAN); return the first success.
async function postWithFallback(bases, path, bodyObj) {
  let lastErr;
  for (const base of bases) {
    try {
      return await postOnce(base, path, bodyObj);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("all endpoints failed");
}

function sendPsnMessage(message) {
  return postWithFallback(PSN_API_URLS, "/v2/send", { message });
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

async function renderIdleGameTime() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#1b5e20");
  text(ctx, "🎮", 63, 45, 20, "#ffffff", "center");
  text(ctx, "GAME", 63, 78, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "TIME!", 63, 98, 11, "#4CAF50", "center", "DeckBold");
  return pngDataUri(img);
}

async function renderIdleSquadUp() {
  const { img, ctx } = makeCanvas(W, H);
  rect(ctx, 0, 0, W, H, "#3949ab");
  text(ctx, "🎮", 63, 45, 20, "#ffffff", "center");
  text(ctx, "SQUAD", 63, 78, 11, "#ffffff", "center", "DeckBold");
  text(ctx, "UP!", 63, 98, 11, "#90CAF9", "center", "DeckBold");
  return pngDataUri(img);
}

async function getIdleImage(action) {
  if (action === "com.psn.slapper.icedcap") return renderIdleIcedCap();
  if (action === "com.psn.slapper.waterbreak") return renderIdleWaterBreak();
  if (action === "com.psn.slapper.never") return renderIdleNever();
  if (action === "com.psn.slapper.roaststart") return renderIdleRoastStart();
  if (action === "com.psn.slapper.roaststop") return renderIdleRoastStop();
  if (action === "com.psn.slapper.roastnow") return renderIdleRoastNow();
  if (action === "com.psn.slapper.gametime") return renderIdleGameTime();
  if (action === "com.psn.slapper.squadup") return renderIdleSquadUp();
  return renderIdle();
}

function sendWhatsAppMessage(message, mentionAll = true) {
  return postWithFallback(WHATSAPP_API_URLS, "/send", { message, mentionAll });
}

function callEndpoint(path) {
  return postWithFallback(PSN_API_URLS, path, null);
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

  if (action === "com.psn.slapper.gametime") {
    sd.setImage(context, await renderSending());
    try {
      // Send to both WhatsApp and PSN
      await Promise.all([
        sendWhatsAppMessage("🎮🔥 Let's party up y'all. It's GAME TIME! 🕹️💥"),
        sendPsnMessage("🎮🔥 Let's party up y'all. It's GAME TIME! 🕹️💥"),
      ]);
      sd.setImage(context, await renderSent());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 2000);
    } catch (e) {
      console.error("Game Time send failed:", e.message);
      sd.setImage(context, await renderError());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 3000);
    }
    return;
  }

  if (action === "com.psn.slapper.squadup") {
    sd.setImage(context, await renderSending());
    try {
      // Rally the dedicated squad PSN group (everyone except wolfie/IG Juicy).
      await callEndpoint("/v2/squad");
      sd.setImage(context, await renderSent());
      setTimeout(async () => { sd.setImage(context, await getIdleImage(action)); }, 2000);
    } catch (e) {
      console.error("Squad Up send failed:", e.message);
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
