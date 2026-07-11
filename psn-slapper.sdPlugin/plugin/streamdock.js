const WebSocket = require("ws");

class StreamDock {
  constructor() {
    this.ws = null;
    this.port = null;
    this.pluginUUID = null;
    this.registerEvent = null;
    this.handlers = {};
  }

  on(event, fn) { this.handlers[event] = fn; return this; }

  connect() {
    const args = process.argv.slice(2);
    for (let i = 0; i < args.length; i++) {
      if (args[i] === "-port") this.port = args[++i];
      else if (args[i] === "-pluginUUID") this.pluginUUID = args[++i];
      else if (args[i] === "-registerEvent") this.registerEvent = args[++i];
      else if (args[i] === "-info") { try { JSON.parse(args[++i]); } catch { } }
    }
    if (!this.port) { console.error("[streamdock] no -port argument"); return; }

    this.ws = new WebSocket(`ws://127.0.0.1:${this.port}`);
    this.ws.on("open", () => {
      this.send({ event: this.registerEvent, uuid: this.pluginUUID });
      this._emit("connected", {});
    });
    this.ws.on("message", (data) => {
      let msg; try { msg = JSON.parse(data.toString()); } catch { return; }
      this._emit(msg.event, msg);
    });
    this.ws.on("error", (err) => console.error("[streamdock] ws error:", err.message));
    this.ws.on("close", () => this._emit("disconnected", {}));
  }

  _emit(event, msg) {
    const fn = this.handlers[event];
    if (!fn) return;
    try { const r = fn(msg); if (r && typeof r.catch === "function") r.catch((e) => console.error(`[streamdock] ${event}:`, e)); }
    catch (e) { console.error(`[streamdock] handler ${event} failed:`, e); }
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  setImage(context, image, state = 0, target = 0) {
    this.send({ event: "setImage", context, payload: { image, target, state } });
  }
  setTitle(context, title = "", state = 0, target = 0) {
    this.send({ event: "setTitle", context, payload: { title, target, state } });
  }
  showAlert(context) { this.send({ event: "showAlert", context }); }
  showOk(context) { this.send({ event: "showOk", context }); }
}

module.exports = { StreamDock };
