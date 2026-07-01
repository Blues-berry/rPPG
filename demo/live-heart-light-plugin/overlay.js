const OVERLAY_STATE_URL = "/api/overlay/state";

const ui = {
  widget: document.getElementById("overlayWidget"),
  pulse: document.getElementById("overlayPulse"),
  bpm: document.getElementById("overlayBpm"),
  status: document.getElementById("overlayStatus"),
};

function clamp(value, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value));
}

function statusText(output) {
  if (!output || output.status === "waiting") return "等待采样";
  if (output.status === "stable" && output.bpm) return "Open-rppg 模型";
  if (output.status === "preview") return "低置信预览";
  if (output.status === "low_sqi") return "SQI 偏低";
  if (output.status === "no_face") return "等待人脸";
  if (output.status === "warming") return "正在校准";
  return "等待采样";
}

async function readState() {
  const response = await fetch(OVERLAY_STATE_URL, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function renderState(state) {
  const output = state.output || {};
  const settings = state.settings || {};
  const bpm = output.bpm;
  const confidence = clamp(Number(output.confidence || 0));
  const arc = confidence * 360;

  ui.bpm.textContent = bpm || "--";
  ui.status.textContent = statusText(output);
  ui.pulse.style.background =
    `radial-gradient(circle at center, rgba(255,255,255,0.92) 0 15%, transparent 16%), conic-gradient(var(--rose) ${arc}deg, rgba(255,255,255,0.18) 0deg)`;

  ui.widget.classList.toggle("pulsing", Boolean(settings.pulse && bpm && confidence > 0.45));
  ui.widget.style.opacity = bpm ? "1" : "0.72";

  const interval = bpm ? Math.max(430, Math.min(1200, 60000 / bpm)) : 900;
  ui.pulse.style.animationDuration = `${interval}ms`;
}

async function render() {
  try {
    renderState(await readState());
  } catch (error) {
    ui.bpm.textContent = "--";
    ui.status.textContent = "服务离线";
    ui.widget.classList.remove("pulsing");
    ui.widget.style.opacity = "0.62";
  }
}

window.setInterval(render, 500);
render();
