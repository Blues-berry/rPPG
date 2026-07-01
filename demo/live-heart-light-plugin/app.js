const CAPTURE_API = "/api/capture";
const MODEL_API = "/api/model";
const OVERLAY_API = "/api/overlay";
const OUTPUT_SQI_THRESHOLD = 0.38;
const PREVIEW_SQI_THRESHOLD = 0.2;
const POLL_INTERVAL_MS = 500;
const HISTORY_SECONDS = 90;

const ui = {
  startBtn: document.getElementById("startBtn"),
  stopBtn: document.getElementById("stopBtn"),
  cameraSourceBtn: document.getElementById("cameraSourceBtn"),
  resetBtn: document.getElementById("resetBtn"),
  copyOverlayBtn: document.getElementById("copyOverlayBtn"),
  captureHint: document.getElementById("captureHint"),
  captureState: document.getElementById("captureState"),
  roiLabel: document.getElementById("roiLabel"),
  bpmValue: document.getElementById("bpmValue"),
  heartTitle: document.getElementById("heartTitle"),
  heartDescription: document.getElementById("heartDescription"),
  confidenceText: document.getElementById("confidenceText"),
  windowText: document.getElementById("windowText"),
  trackerText: document.getElementById("trackerText"),
  modelText: document.getElementById("modelText"),
  outputText: document.getElementById("outputText"),
  heartOrb: document.getElementById("heartOrb"),
  lightAdvice: document.getElementById("lightAdvice"),
  brightnessInput: document.getElementById("brightnessInput"),
  temperatureInput: document.getElementById("temperatureInput"),
  pulseToggle: document.getElementById("pulseToggle"),
  brightnessMeter: document.getElementById("brightnessMeter"),
  brightnessText: document.getElementById("brightnessText"),
  motionMeter: document.getElementById("motionMeter"),
  motionText: document.getElementById("motionText"),
  peakMeter: document.getElementById("peakMeter"),
  cameraFpsText: document.getElementById("cameraFpsText"),
  detectMsText: document.getElementById("detectMsText"),
  cropMsText: document.getElementById("cropMsText"),
  requestMsText: document.getElementById("requestMsText"),
  modelInputFpsText: document.getElementById("modelInputFpsText"),
  modelUpdateMsText: document.getElementById("modelUpdateMsText"),
  modelMetricMsText: document.getElementById("modelMetricMsText"),
  dropText: document.getElementById("dropText"),
  waveCanvas: document.getElementById("waveCanvas"),
};

const waveCtx = ui.waveCanvas.getContext("2d");

const state = {
  overlay: null,
  history: [],
  lastPollAt: 0,
};

function clamp(value, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value));
}

function formatMs(value) {
  return Number.isFinite(value) ? `${value.toFixed(value >= 10 ? 0 : 1)}ms` : "--";
}

function formatFps(value) {
  return Number.isFinite(value) ? `${value.toFixed(value >= 10 ? 0 : 1)}fps` : "--";
}

function formatCount(value) {
  return Number.isFinite(value) ? String(Math.round(value)) : "--";
}

function setStatePill(label, kind) {
  ui.captureState.textContent = label;
  ui.captureState.className = `state-pill ${kind}`;
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function startCapture() {
  setStatePill("STARTING", "warn");
  ui.captureHint.textContent = "正在启动后端摄像头采集和 Open-rppg 模型。";
  try {
    await apiJson(`${CAPTURE_API}/start`, {
      method: "POST",
      body: JSON.stringify({ device_index: 0, width: 1280, height: 720, fps: 30 }),
    });
    await pollState(true);
  } catch (error) {
    setStatePill("ERROR", "bad");
    ui.captureHint.textContent = `启动失败：${error.message}`;
  }
}

async function stopCapture() {
  setStatePill("STOPPING", "warn");
  ui.captureHint.textContent = "正在停止后端采集并清空直播输出。";
  try {
    await apiJson(`${CAPTURE_API}/stop`, { method: "POST", body: "{}" });
    await pollState(true);
  } catch (error) {
    setStatePill("ERROR", "bad");
    ui.captureHint.textContent = `停止失败：${error.message}`;
  }
}

async function resetModel() {
  try {
    await apiJson(`${MODEL_API}/reset`, { method: "POST", body: "{}" });
    state.history = [];
    await pollState(true);
  } catch (error) {
    ui.captureHint.textContent = `重置失败：${error.message}`;
  }
}

async function pushOverlaySettings() {
  try {
    await apiJson(`${OVERLAY_API}/settings`, {
      method: "POST",
      body: JSON.stringify({
        pulse: ui.pulseToggle.checked,
        brightness: Number(ui.brightnessInput.value),
        temperature: Number(ui.temperatureInput.value),
      }),
    });
  } catch (error) {
    ui.captureHint.textContent = `Overlay 设置未同步：${error.message}`;
  }
}

async function copyUrl(path, button) {
  const url = new URL(path, window.location.href).href;
  await navigator.clipboard.writeText(url);
  const previous = button.textContent;
  button.textContent = "已复制";
  window.setTimeout(() => {
    button.textContent = previous;
  }, 1200);
}

async function pollState(force = false) {
  const now = performance.now();
  if (!force && now - state.lastPollAt < POLL_INTERVAL_MS) return;
  state.lastPollAt = now;

  try {
    state.overlay = await apiJson(`${OVERLAY_API}/state`);
    updateHistory(state.overlay);
    updateUi(state.overlay);
  } catch (error) {
    setStatePill("OFFLINE", "bad");
    ui.heartTitle.textContent = "服务不可用";
    ui.heartDescription.textContent = `无法连接本地模型服务：${error.message}`;
    ui.captureHint.textContent = "请先运行 model_server.py。";
  }
}

function updateHistory(snapshot) {
  const model = snapshot.model || {};
  const output = snapshot.output || {};
  const now = Date.now();
  state.history.push({
    t: now,
    bpm: Number.isFinite(output.bpm) ? output.bpm : Number.isFinite(model.hr) ? model.hr : null,
    sqi: Number.isFinite(model.SQI) ? model.SQI : 0,
    status: output.status,
  });
  const cutoff = now - HISTORY_SECONDS * 1000;
  state.history = state.history.filter((item) => item.t >= cutoff);
}

function updateUi(snapshot) {
  const capture = snapshot.capture || {};
  const model = snapshot.model || {};
  const output = snapshot.output || {};
  const settings = snapshot.settings || {};
  const captureRunning = capture.state === "running";
  const confidence = clamp(Number(output.confidence || 0));
  const confidencePct = Math.round(confidence * 100);
  const bpm = Number.isFinite(output.bpm) ? output.bpm : null;
  const previewBpm = Number.isFinite(model.hr) ? Math.round(model.hr) : null;
  const displayBpm = bpm ?? (output.status === "preview" ? previewBpm : null);
  const windowSeconds =
    Number.isFinite(model.frame_count) && Number.isFinite(model.input_fps) && model.input_fps > 0
      ? model.frame_count / model.input_fps
      : 0;

  ui.bpmValue.textContent = displayBpm == null ? "--" : String(displayBpm);
  ui.confidenceText.textContent = `${confidencePct}%`;
  ui.windowText.textContent = `${Math.round(windowSeconds)}s`;
  ui.trackerText.textContent = captureRunning ? "后端 BlazeFace" : "待机";
  ui.modelText.textContent = model.ready ? model.model || "Open-rppg" : model.state || "loading";
  ui.outputText.textContent = bpm == null ? (output.status === "preview" ? "预览" : "锁定") : "直播中";

  const arc = confidencePct * 3.6;
  ui.heartOrb.style.background =
    `radial-gradient(circle at center, #15191d 58%, transparent 59%), conic-gradient(var(--rose) ${arc}deg, #2b3237 0deg)`;

  updateStateCopy(capture, model, output);
  updatePerfUi(capture, model, output);
  updateQualityUi(capture, model);
  updateLightAdvice(capture, model, output);

  if (typeof settings.pulse === "boolean") ui.pulseToggle.checked = settings.pulse;
  if (Number.isFinite(settings.brightness)) ui.brightnessInput.value = settings.brightness;
  if (Number.isFinite(settings.temperature)) ui.temperatureInput.value = settings.temperature;

  ui.stopBtn.disabled = !captureRunning && capture.state !== "starting";
  ui.startBtn.disabled = captureRunning || capture.state === "starting";
  drawWave();
}

function updateStateCopy(capture, model, output) {
  if (capture.state === "camera_error") {
    ui.heartTitle.textContent = "摄像头不可用";
    ui.heartDescription.textContent = capture.error || "摄像头被占用、断开，或没有权限。";
    ui.captureHint.textContent = "请确认 OBS 没有直接占用摄像头；第一阶段由 Python 服务独占摄像头。";
    setStatePill("CAMERA ERR", "bad");
    return;
  }

  if (capture.state !== "running") {
    ui.heartTitle.textContent = "等待后端采集";
    ui.heartDescription.textContent = "启动后端采集后，页面可关闭，服务仍会持续监测。";
    ui.captureHint.textContent = "OBS 中添加 camera.html 作为摄像头源，overlay.html 作为心率 Overlay。";
    setStatePill("WAITING", "waiting");
    return;
  }

  ui.captureHint.textContent = `后端采集运行中：device ${capture.device_index}，${capture.width}x${capture.height}，输入 ${formatFps(capture.input_fps)}。`;

  if (!model.ready) {
    ui.heartTitle.textContent = "模型加载中";
    ui.heartDescription.textContent = "摄像头已持续采集，Open-rppg 权重加载完成后会自动开始推理。";
    setStatePill("LOADING", "warn");
  } else if (output.status === "stable") {
    ui.heartTitle.textContent = "直播心率输出中";
    ui.heartDescription.textContent = `SQI=${Number(model.SQI).toFixed(2)}，Overlay 正在输出 Open-rppg 心率。`;
    setStatePill("READY", "ready");
  } else if (output.status === "preview") {
    ui.heartTitle.textContent = "低置信预览";
    ui.heartDescription.textContent = `模型已返回 HR，但 SQI=${Number(model.SQI).toFixed(2)}，暂不推送到直播 Overlay。`;
    setStatePill("PREVIEW", "warn");
  } else if (output.status === "no_face") {
    ui.heartTitle.textContent = "等待人脸";
    ui.heartDescription.textContent = "后端仍在持续采集，但没有稳定人脸时不会沿用旧 BPM。";
    setStatePill("NO FACE", "bad");
  } else if (output.status === "low_sqi") {
    ui.heartTitle.textContent = "SQI 偏低，暂不输出";
    ui.heartDescription.textContent = `当前 SQI=${Number(model.SQI || 0).toFixed(2)}，建议稳定补光、减少头动。`;
    setStatePill("LOW SQI", "bad");
  } else {
    ui.heartTitle.textContent = "正在建立窗口";
    ui.heartDescription.textContent = "后端正在持续积累 BVP/SQI 窗口，达标后 Overlay 自动输出。";
    setStatePill("MODEL", "warn");
  }
}

function updatePerfUi(capture, model, output) {
  const perf = model.perf || {};
  ui.cameraFpsText.textContent = formatFps(capture.input_fps);
  ui.detectMsText.textContent = formatMs(capture.read_ms);
  ui.cropMsText.textContent = formatCount(capture.frames_read);
  ui.requestMsText.textContent = output.reason || "--";
  ui.modelInputFpsText.textContent = formatFps(model.input_fps);
  ui.modelUpdateMsText.textContent = formatMs(perf.update_ms);
  ui.modelMetricMsText.textContent = formatMs(perf.metric_ms);
  ui.dropText.textContent = `${model.no_face_count || 0}/${capture.dropped_frames || 0}`;
  ui.roiLabel.textContent = capture.state === "running" ? "OBS CAMERA SOURCE" : "SERVER CAMERA";
}

function updateQualityUi(capture, model) {
  const captureScore = capture.state === "running" && Number.isFinite(capture.input_fps)
    ? clamp(capture.input_fps / Math.max(1, capture.target_fps || 30))
    : 0;
  const faceScore = model.has_face ? 1 : 0;
  const sqiScore = clamp(Number(model.SQI || 0));

  ui.brightnessMeter.value = Math.round(captureScore * 100);
  ui.motionMeter.value = Math.round(faceScore * 100);
  ui.peakMeter.value = Math.round(sqiScore * 100);
  ui.brightnessText.textContent = `${Math.round(captureScore * 100)}%`;
  ui.motionText.textContent = `${Math.round(faceScore * 100)}%`;
  ui.peakText.textContent = `${Math.round(sqiScore * 100)}%`;
}

function updateLightAdvice(capture, model, output) {
  if (capture.state !== "running") {
    ui.lightAdvice.textContent = "建议：先启动后端采集，再在 OBS 添加摄像头源和 Overlay。";
  } else if (!model.has_face) {
    ui.lightAdvice.textContent = "建议：让脸部进入画面中央，避免麦克风、手或头发遮挡。";
  } else if ((model.SQI || 0) < PREVIEW_SQI_THRESHOLD) {
    ui.lightAdvice.textContent = "建议：提高并锁定补光亮度，减少画面自动曝光和快速头动。";
  } else if ((model.SQI || 0) < OUTPUT_SQI_THRESHOLD) {
    ui.lightAdvice.textContent = "建议：当前已有低置信预览，继续保持正脸和稳定补光等待 SQI 提升。";
  } else if (output.status === "stable") {
    ui.lightAdvice.textContent = "建议：保持当前补光，不要让灯光跟随音乐或弹幕快速闪烁。";
  } else {
    ui.lightAdvice.textContent = "建议：保持柔和稳定补光，等待模型窗口稳定。";
  }
}

function drawWave() {
  const width = ui.waveCanvas.width;
  const height = ui.waveCanvas.height;
  waveCtx.clearRect(0, 0, width, height);
  waveCtx.fillStyle = "#11161a";
  waveCtx.fillRect(0, 0, width, height);
  waveCtx.strokeStyle = "rgba(255,255,255,0.08)";
  waveCtx.lineWidth = 1;
  for (let x = 0; x < width; x += 64) {
    waveCtx.beginPath();
    waveCtx.moveTo(x, 0);
    waveCtx.lineTo(x, height);
    waveCtx.stroke();
  }

  const points = state.history.filter((item) => Number.isFinite(item.sqi));
  if (points.length < 2) return;
  const start = points[0].t;
  const end = points[points.length - 1].t;
  const duration = Math.max(1, end - start);

  waveCtx.strokeStyle = "#55d2b0";
  waveCtx.lineWidth = 3;
  waveCtx.beginPath();
  points.forEach((point, index) => {
    const x = ((point.t - start) / duration) * width;
    const y = height - clamp(point.sqi) * height * 0.82 - height * 0.09;
    if (index === 0) waveCtx.moveTo(x, y);
    else waveCtx.lineTo(x, y);
  });
  waveCtx.stroke();

  const bpmPoints = points.filter((point) => Number.isFinite(point.bpm));
  if (bpmPoints.length < 2) return;
  waveCtx.strokeStyle = "#ff6b83";
  waveCtx.lineWidth = 2;
  waveCtx.beginPath();
  bpmPoints.forEach((point, index) => {
    const x = ((point.t - start) / duration) * width;
    const y = height - clamp((point.bpm - 30) / 150) * height * 0.72 - height * 0.14;
    if (index === 0) waveCtx.moveTo(x, y);
    else waveCtx.lineTo(x, y);
  });
  waveCtx.stroke();
}

ui.startBtn.addEventListener("click", startCapture);
ui.stopBtn.addEventListener("click", stopCapture);
ui.resetBtn.addEventListener("click", resetModel);
ui.cameraSourceBtn.addEventListener("click", () => copyUrl("camera.html", ui.cameraSourceBtn));
ui.copyOverlayBtn.addEventListener("click", () => copyUrl("overlay.html", ui.copyOverlayBtn));

[ui.brightnessInput, ui.temperatureInput, ui.pulseToggle].forEach((input) => {
  input.addEventListener("input", pushOverlaySettings);
});

ui.stopBtn.disabled = true;
pollState(true);
window.setInterval(() => pollState(false), POLL_INTERVAL_MS);
