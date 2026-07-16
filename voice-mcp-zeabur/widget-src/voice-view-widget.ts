import { App } from "@modelcontextprotocol/ext-apps";

/**
 * 祈牌语音条 widget — a full-width "voice skin" card: a themed background fills the
 * host's iframe (killing the white gap), with a compact voice bubble on top whose
 * width grows with the clip duration (WeChat-style).
 * Data arrives via structuredContent (Claude: `toolresult` / ChatGPT: window.openai.toolOutput).
 */

interface VoiceData {
  audioUrl: string;          // data:audio/mpeg;base64,... or https URL
  duration: number;          // seconds (estimated)
  senderName: string;
  colorPrimary: string;
  colorSecondary: string;
  colorBg: string;
  colorBgEnd: string;
  barCount: number;
  bgImage: string;           // optional skin background image URL ("" = default gradient skin)
  customCss: string;         // optional user CSS injected into the widget (data-driven, live)
  bars: number[];            // real waveform peaks (0-1) from the audio; [] = default shape
}

declare global {
  interface Window {
    openai?: { toolOutput?: unknown;[k: string]: unknown };
  }
}

function coerce(data: unknown): VoiceData | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.audioUrl !== "string" || !d.audioUrl) return null;
  const str = (v: unknown, fb: string) => (typeof v === "string" && v ? v : fb);
  const num = (v: unknown, fb: number) => (typeof v === "number" && isFinite(v) ? v : fb);
  return {
    audioUrl: d.audioUrl,
    duration: num(d.duration, 1),
    senderName: str(d.senderName, "祈"),
    colorPrimary: str(d.colorPrimary, "#f59e0b"),
    colorSecondary: str(d.colorSecondary, "#ea580c"),
    colorBg: str(d.colorBg, "#1e1b18"),
    colorBgEnd: str(d.colorBgEnd, "#2a2520"),
    barCount: num(d.barCount, 28),
    bgImage: str(d.bgImage, ""),
    customCss: str(d.customCss, ""),
    bars: Array.isArray(d.bars) ? (d.bars as unknown[]).map((x) => (typeof x === "number" ? x : 0)) : []
  };
}

/** Deterministic pseudo-random in [0,1) seeded by i, so the waveform is stable across renders. */
function seeded(i: number): number {
  const x = Math.sin(i * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

let appRef: App | null = null;
let rendered = false;

function fmtTime(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function render(data: VoiceData, platform: "chatgpt" | "claude") {
  rendered = true;
  const root = document.getElementById("root");
  if (!root) return;
  root.innerHTML = "";

  // Live custom CSS (data-driven — editing it in /customize takes effect on the next
  // voice with no widget reload). Users target #vc-card / #vc-bubble / #vc-wave etc.
  let styleEl = document.getElementById("vc-custom-css") as HTMLStyleElement | null;
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = "vc-custom-css";
    document.head.appendChild(styleEl);
  }
  styleEl.textContent = data.customCss || "";

  // ── Full-width skin card (fills the host iframe → no white gap) ──
  const card = document.createElement("div");
  card.id = "vc-card";
  const bg = data.bgImage
    ? `center/cover no-repeat url("${data.bgImage}"), linear-gradient(135deg, ${data.colorBg}, ${data.colorBgEnd})`
    : `radial-gradient(140px 90px at 22% 50%, ${data.colorPrimary}33, transparent 72%),` +
      `radial-gradient(160px 120px at 88% 120%, ${data.colorSecondary}22, transparent 70%),` +
      `linear-gradient(135deg, ${data.colorBg}, ${data.colorBgEnd})`;
  card.style.cssText = `
    position:relative; box-sizing:border-box; width:100%; min-width:280px;
    min-height:92px; display:flex; align-items:center; padding:0 20px;
    background:${bg}; overflow:hidden;
    font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei UI",sans-serif;`;

  // subtle music-note flourish on the right
  const deco = document.createElement("div");
  deco.id = "vc-deco";
  deco.textContent = "♪  ♫";
  deco.style.cssText = `position:absolute; right:20px; top:14px; font-size:15px;
    color:${data.colorPrimary}; opacity:0.35; letter-spacing:2px; pointer-events:none;`;
  card.appendChild(deco);

  // ── Voice bubble; width follows the waveform via width:auto (WeChat-style: longer clip
  //    → more bars → wider bubble). This "length follows duration" is the fixed core logic;
  //    look (bg/border/radius) stays fully overridable via custom_css. ──
  const bubble = document.createElement("div");
  bubble.id = "vc-bubble";
  bubble.style.cssText = `
    position:relative; z-index:1; box-sizing:border-box;
    display:inline-flex; align-items:center; gap:10px; width:auto; max-width:calc(100% - 8px);
    background:linear-gradient(135deg, rgba(0,0,0,0.34), rgba(0,0,0,0.18));
    border:1px solid ${data.colorPrimary}33; border-radius:16px; padding:8px 14px;
    box-shadow:0 3px 12px rgba(0,0,0,0.28); cursor:pointer; user-select:none;
    backdrop-filter:blur(2px);`;

  // play / pause button
  const btn = document.createElement("div");
  btn.id = "vc-play";
  btn.style.cssText = `
    width:30px; height:30px; border-radius:50%; flex-shrink:0;
    background:linear-gradient(135deg, ${data.colorPrimary}, ${data.colorSecondary});
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 2px 6px ${data.colorPrimary}55;`;
  btn.innerHTML =
    `<svg class="i-play" width="13" height="13" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>` +
    `<svg class="i-pause" width="13" height="13" viewBox="0 0 24 24" fill="white" style="display:none"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>`;
  const iPlay = btn.querySelector(".i-play") as SVGElement;
  const iPause = btn.querySelector(".i-pause") as SVGElement;

  // waveform + labels
  const col = document.createElement("div");
  col.id = "vc-col";
  col.style.cssText = "display:flex; flex-direction:column; gap:3px;";
  const wave = document.createElement("div");
  wave.id = "vc-wave";
  wave.style.cssText = "display:flex; align-items:center; gap:2px; height:22px; width:auto;";
  const bars: HTMLDivElement[] = [];
  // Fixed core logic: bar count scales with duration → the fixed-width bars make the wave
  // (and the auto-width bubble around it) grow longer for longer clips.
  // Real waveform from the audio when available (data.bars = 0-1 loudness peaks);
  // otherwise a stable default shape. Bar count follows duration either way.
  const real = data.bars && data.bars.length > 0 ? data.bars : null;
  const n = real ? real.length : Math.max(12, Math.min(60, Math.round(data.duration * 3.2)));
  for (let i = 0; i < n; i++) {
    const bar = document.createElement("div");
    const pos = i / n;
    const env = Math.sin(pos * Math.PI) * 0.55 + 0.45;
    const h = real
      ? Math.max(10, Math.min(100, real[i] * 100))
      : Math.max(14, Math.min(100, (0.28 + seeded(i) * 0.72) * env * 100));
    bar.style.cssText =
      `width:3px; height:${h}%; border-radius:1.5px; flex-shrink:0;` +
      `background:rgba(255,255,255,0.28); transition:background 0.12s;`;
    wave.appendChild(bar);
    bars.push(bar);
  }
  const labels = document.createElement("div");
  labels.id = "vc-labels";
  labels.style.cssText = "display:flex; justify-content:space-between; align-items:center;";
  const timeEl = document.createElement("span");
  timeEl.id = "vc-time";
  timeEl.textContent = "0:00";
  timeEl.style.cssText = `font-size:9px; color:${data.colorPrimary}; font-weight:600;`;
  const durEl = document.createElement("span");
  durEl.id = "vc-dur";
  durEl.textContent = data.duration + '"';
  durEl.style.cssText = "font-size:9px; color:rgba(255,255,255,0.45);";
  labels.append(timeEl, durEl);
  col.append(wave, labels);

  const audio = document.createElement("audio");
  audio.preload = "auto";
  audio.src = data.audioUrl;

  bubble.append(btn, col, audio);
  card.appendChild(bubble);

  // sender name under the bubble, on the skin
  const nameEl = document.createElement("div");
  nameEl.id = "vc-name";
  nameEl.textContent = data.senderName + " · 语音";
  nameEl.style.cssText = `position:absolute; left:22px; bottom:12px; font-size:9px;
    color:rgba(255,255,255,0.4); pointer-events:none;`;
  card.appendChild(nameEl);

  root.appendChild(card);

  // ── Playback ──
  let playing = false;
  let raf = 0;
  const paint = (on: boolean, bar: HTMLDivElement) => {
    if (on) {
      bar.style.background = `linear-gradient(to top, ${data.colorPrimary}, ${data.colorSecondary})`;
      bar.style.boxShadow = `0 0 3px ${data.colorPrimary}44`;
    } else {
      bar.style.background = "rgba(255,255,255,0.28)";
      bar.style.boxShadow = "none";
    }
  };
  const tick = () => {
    if (!playing) return;
    const prog = audio.currentTime / (audio.duration || data.duration || 1);
    const upto = Math.floor(prog * bars.length);
    bars.forEach((b, i) => paint(i < upto, b));
    timeEl.textContent = fmtTime(audio.currentTime);
    raf = requestAnimationFrame(tick);
  };
  const toggle = () => {
    if (playing) {
      audio.pause();
      playing = false;
      iPlay.style.display = "block";
      iPause.style.display = "none";
      cancelAnimationFrame(raf);
    } else {
      audio.play().then(() => {
        playing = true;
        iPlay.style.display = "none";
        iPause.style.display = "block";
        tick();
      }).catch((e) => console.warn("[voice] playback failed:", e));
    }
  };
  bubble.addEventListener("click", toggle);
  audio.addEventListener("ended", () => {
    playing = false;
    iPlay.style.display = "block";
    iPause.style.display = "none";
    cancelAnimationFrame(raf);
    bars.forEach((b) => paint(false, b));
    timeEl.textContent = "0:00";
  });

  // ── Report only HEIGHT to the host (width is fixed by host = full skin width) ──
  if (platform === "claude") {
    const reportH = () => {
      const h = Math.ceil(card.getBoundingClientRect().height);
      if (h <= 0) return;
      document.documentElement.style.height = h + "px";
      document.body.style.height = h + "px";
      if (appRef) {
        try {
          appRef.sendSizeChanged({ width: Math.ceil(window.innerWidth), height: h });
        } catch {
          /* ignore */
        }
      }
    };
    requestAnimationFrame(() => {
      reportH();
      requestAnimationFrame(reportH);
      setTimeout(reportH, 200);
    });
  }
}

function showError(msg: string) {
  if (rendered) return;
  const root = document.getElementById("root");
  if (root) root.innerHTML = `<div style="color:#b8aabb;font-size:13px;padding:10px;">${msg}</div>`;
}

function renderToolResult(
  params: { structuredContent?: unknown; content?: Array<{ type: string; text?: string }> },
  platform: "chatgpt" | "claude"
) {
  let data = coerce(params?.structuredContent);
  if (!data && Array.isArray(params?.content)) {
    for (const block of params.content) {
      if (block.type === "text" && block.text) {
        try {
          data = coerce(JSON.parse(block.text));
        } catch {
          /* not json */
        }
        if (data) break;
      }
    }
  }
  if (data) render(data, platform);
}

function tryChatGpt() {
  if (!window.openai) return;
  const apply = () => {
    const data = coerce(window.openai?.toolOutput);
    if (data) render(data, "chatgpt");
  };
  apply();
  window.addEventListener("openai:set_globals", apply as EventListener);
  window.addEventListener(
    "message",
    (event) => {
      if (event.source !== window.parent) return;
      const message = (event as MessageEvent).data;
      if (!message || message.jsonrpc !== "2.0") return;
      if (message.method !== "ui/notifications/tool-result") return;
      renderToolResult(message.params, "chatgpt");
    },
    { passive: true }
  );
}

async function tryMcpApps() {
  try {
    const app = new App({ name: "voice-mcp", version: "1.0.0" }, {}, { autoResize: false });
    appRef = app;
    app.addEventListener("toolresult", (params: { structuredContent?: unknown; content?: Array<{ type: string; text?: string }> }) => {
      renderToolResult(params, "claude");
    });
    await app.connect();
  } catch (e) {
    console.debug("[voice] MCP Apps connect skipped:", e);
  }
}

function boot() {
  tryChatGpt();
  void tryMcpApps();
  setTimeout(() => showError("等待语音数据…"), 4000);
}

boot();
