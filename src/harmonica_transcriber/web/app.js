"use strict";
const $ = id => document.getElementById(id);
const state = { file: null, fileUrl: null, resultFileUrl: null, example: false, audioExample: false, corrected: false, result: null, view: "arranged", playingView: null, recording: null, recordingListener: null, transport: null, transportTimer: null, seeking: false, busy: false, audio: null, timer: null, nodes: new Set() };
const token = document.querySelector('meta[name="bapuluofu-token"]').content;
function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function error(message) { $("error").textContent = message; $("error").hidden = !message; }
let toastTimer;
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 2800); }
function busy(value) {
  state.busy = value;
  document.body.classList.toggle("busy", value);
  for (const id of ["example", "example-audio", "apply-edits", "raise-octave", "file", "reset"]) $(id).disabled = value;
  $("generate").disabled = value || !(state.file || state.example || state.audioExample);
  $("generate").textContent = value ? "正在生成…" : "生成口琴谱 →";
}
function clearFilePreview() {
  $("audio-preview").pause();
  $("audio-preview").removeAttribute("src");
  $("audio-preview").load();
  if (state.fileUrl) URL.revokeObjectURL(state.fileUrl);
  state.fileUrl = null;
  $("audio-preview-wrap").hidden = true;
  $("audio-preview-duration").textContent = "";
}
function selectFile(file) {
  if (!file || state.busy) return;
  if (!/\.(mid|midi|json|mp3|wav|mp4|m4a)$/i.test(file.name)) return error("请选择 MP3、WAV、MP4、M4A、MIDI 或 Score JSON 文件。");
  if (!file.size || file.size > 96 * 1024 * 1024) return error("文件应为 1 字节至 96 MB。");
  stopAudio();
  clearFilePreview();
  state.file = file; state.example = false; state.audioExample = false; state.corrected = false;
  $("sidecar").value = "";
  $("file-name").textContent = file.name;
  $("file-description").textContent = (file.size / 1024).toFixed(1) + " KB · 点击可更换";
  $("midi-fields").hidden = !/\.(mid|midi)$/i.test(file.name);
  const isAudio = /\.(mp3|wav|mp4|m4a)$/i.test(file.name);
  $("audio-fields").hidden = !isAudio;
  if (isAudio) {
    state.fileUrl = URL.createObjectURL(file);
    $("audio-preview").src = state.fileUrl;
    $("audio-preview-wrap").hidden = false;
    $("audio-mode").value = /\.(mp4|m4a)$/i.test(file.name) ? "vocal" : "direct";
    $("audio-full").checked = false; updateAudioRange();
  }
  $("result-state").textContent = state.result ? "新文件待生成 · 下方为上次结果" : "已导入，等待生成";
  error(""); busy(false);
}
$("file").addEventListener("change", e => selectFile(e.target.files[0]));
$("drop-zone").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (!state.busy) $("file").click(); } });
for (const event of ["dragenter", "dragover"]) $("drop-zone").addEventListener(event, e => { e.preventDefault(); $("drop-zone").classList.add("dragover"); });
for (const event of ["dragleave", "drop"]) $("drop-zone").addEventListener(event, e => { e.preventDefault(); $("drop-zone").classList.remove("dragover"); });
$("drop-zone").addEventListener("drop", e => selectFile(e.dataTransfer.files[0]));
$("audio-preview").addEventListener("loadedmetadata", () => {
  const seconds = $("audio-preview").duration;
  if (Number.isFinite(seconds)) {
    $("audio-preview-duration").textContent = `总时长 ${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒；可拖动进度条选择片段。`;
    if (seconds > 60) { $("audio-full").checked = true; updateAudioRange(); }
  }
});
function updateAudioRange() {
  for (const id of ["audio-start", "audio-duration", "use-audio-position"]) $(id).disabled = $("audio-full").checked;
}
$("audio-full").onchange = updateAudioRange;
$("use-audio-position").onclick = () => {
  const preview = $("audio-preview");
  if (!Number.isFinite(preview.currentTime)) return;
  $("audio-start").value = preview.currentTime.toFixed(1);
  toast("已设置片段起点");
};
function transpose(value) {
  $("transpose").value = Math.max(-48, Math.min(48, value));
  document.querySelectorAll("[data-transpose]").forEach(b => b.classList.toggle("selected", Number(b.dataset.transpose) === Number($("transpose").value)));
}
document.querySelectorAll("[data-transpose]").forEach(b => b.addEventListener("click", () => transpose(Number(b.dataset.transpose))));
$("transpose-up").onclick = () => transpose(Number($("transpose").value) + 1);
$("transpose-down").onclick = () => transpose(Number($("transpose").value) - 1);
$("transpose").oninput = () => document.querySelectorAll("[data-transpose]").forEach(b => b.classList.toggle("selected", Number(b.dataset.transpose) === Number($("transpose").value)));
function settings() {
  const result = {};
  for (const [key, id] of Object.entries({transpose:"transpose", midi_origin:"midi-origin", track:"track", channel:"channel", key:"key", do_midi:"do-midi"})) result[key] = $(id).value;
  if (result.transpose === "" || !Number.isInteger(Number(result.transpose)) || Number(result.transpose) < -48 || Number(result.transpose) > 48) throw new Error("移调必须是 -48 至 48 之间的整数。");
  for (const key of ["transpose", "track", "channel", "do_midi"]) if (result[key] !== "") result[key] = Number(result[key]);
  if (state.audioExample || (state.file && /\.(mp3|wav|mp4|m4a)$/i.test(state.file.name))) {
    for (const [key, id] of Object.entries({audio_start:"audio-start", audio_duration:"audio-duration", audio_bpm:"audio-bpm"})) {
      const value = Number($(id).value);
      if ($(id).value === "" || !Number.isFinite(value)) throw new Error("音频片段和 BPM 需要填写有效数字。");
      result[key] = value;
    }
    result.audio_key = $("audio-key").value;
    result.audio_mode = $("audio-mode").value;
    result.audio_full = $("audio-full").checked;
  }
  return result;
}
function upload(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 96 * 1024 * 1024) return reject(new Error("每个文件最多 96 MB。"));
    const reader = new FileReader();
    reader.onload = () => resolve({name:file.name, data:reader.result.split(",")[1]});
    reader.onerror = () => reject(new Error("无法读取文件，请重新选择。"));
    reader.readAsDataURL(file);
  });
}
function showProgress(phase) {
  $("processing-progress").hidden = false;
  $("progress-phase").textContent = phase;
  $("progress-count").textContent = "";
  $("progress-bar").removeAttribute("value");
  $("result-state").textContent = phase;
}
function updateProgress(data) {
  if (!data || data.status === "failed") return;
  $("progress-phase").textContent = data.phase;
  const total = Number(data.total), completed = Number(data.completed);
  if (Number.isFinite(total) && total > 0 && Number.isFinite(completed)) {
    $("progress-bar").max = total;
    $("progress-bar").value = Math.min(total, Math.max(0, completed));
  }
  const chunks = Number(data.chunks_total), finished = Number(data.chunks_completed);
  $("progress-count").textContent = Number.isFinite(chunks) && chunks > 0 ? `${finished}/${chunks} 段` : "";
  $("result-state").textContent = data.phase + ($("progress-count").textContent ? ` · ${$("progress-count").textContent}` : "");
}
async function generate(edits) {
  if (state.busy) return;
  stopAudio(); error(""); busy(true);
  showProgress("正在读取文件");
  const progressId = crypto.randomUUID().replaceAll("-", "");
  let progressTimer = null, progressActive = true, polling = false;
  const poll = async () => {
    if (!progressActive || polling) return;
    polling = true;
    try {
      const response = await fetch(`/api/progress/${progressId}`, {headers:{"X-Bapuluofu-Token":token}});
      if (response.ok) {
        const data = await response.json();
        if (progressActive) updateProgress(data);
      }
    } catch (_) { /* The generate request reports a connection failure. */ }
    finally { polling = false; }
  };
  try {
    const newSource = !edits && !state.corrected ? state.file : null;
    const payload = {settings:settings(), progress_id:progressId};
    if (edits) { payload.score = state.result.score; payload.edits = edits; payload.parent_job_id = state.result.job_id; }
    else if (state.example) payload.example = true;
    else if (state.audioExample) payload.example_audio = true;
    else if (state.corrected) { payload.score = state.result.score; payload.edits = []; payload.parent_job_id = state.result.job_id; }
    else if (state.file) payload.input = await upload(state.file);
    else throw new Error("请先选择文件。");
    if (!edits && !state.example && $("sidecar").files[0]) payload.sidecar = await upload($("sidecar").files[0]);
    if ($("profile").files[0]) payload.profile = await upload($("profile").files[0]);
    showProgress("正在上传并启动分析");
    progressTimer = setInterval(poll, 800);
    const response = await fetch("/api/generate", {method:"POST", headers:{"Content-Type":"application/json", "X-Bapuluofu-Token":token}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "生成失败");
    if (!edits && !state.corrected) {
      $("result-source-audio").removeAttribute("src");
      if (state.resultFileUrl) URL.revokeObjectURL(state.resultFileUrl);
      state.resultFileUrl = newSource && /\.(mp3|wav|mp4|m4a)$/i.test(newSource.name) ? URL.createObjectURL(newSource) : null;
    }
    state.result = data;
    // Future setting changes must retain manual corrections.
    if (edits && edits.length) { state.file = new File([JSON.stringify(data.score)], "校正后的乐谱.json", {type:"application/json"}); state.example = false; state.audioExample = false; state.corrected = true; $("file-name").textContent = state.file.name; $("file-description").textContent = "已保留校正 · 可继续调整移调"; $("midi-fields").hidden = true; $("audio-fields").hidden = true; $("sidecar").value = ""; }
    renderResult(); toast(edits && edits.length ? "音高校正已保存为新版本" : "乐谱已生成并保存在本机");
  } catch (exc) { error(exc.message === "Failed to fetch" ? "无法连接本机服务，请双击启动入口重新打开。" : exc.message); $("result-state").textContent = "生成失败 · 请查看提示"; }
  finally { progressActive = false; if (progressTimer) clearInterval(progressTimer); $("processing-progress").hidden = true; busy(false); }
}
$("generate").onclick = () => generate();
$("example").onclick = () => {
  clearFilePreview();
  state.file = null; state.example = true; state.audioExample = false; state.corrected = false; $("file").value = ""; $("sidecar").value = "";
  $("file-name").textContent = "G 大调 · 四音示例"; $("file-description").textContent = "D5 · E5 · G5 · F♯5"; $("midi-fields").hidden = true; $("audio-fields").hidden = true;
  generate();
};
$("example-audio").onclick = () => {
  clearFilePreview();
  state.file = null; state.example = false; state.audioExample = true; state.corrected = false; $("file").value = ""; $("sidecar").value = "";
  $("file-name").textContent = "C 大调 · WAV 四音示例"; $("file-description").textContent = "C5 · D5 · E5 · F5";
  $("midi-fields").hidden = true; $("audio-fields").hidden = false;
  $("audio-start").value = 0; $("audio-duration").value = 3; $("audio-bpm").value = 120; $("audio-key").value = "C major";
  $("audio-full").checked = false; updateAudioRange();
  $("audio-mode").value = "direct";
  generate();
};
function pitchName(pitch) { return ["C","C♯","D","D♯","E","F","F♯","G","G♯","A","A♯","B"][pitch % 12] + (Math.floor(pitch / 12) - 1); }
function renderNotation() {
  const original = state.view === "original";
  const accompaniment = state.view === "accompaniment" && state.result.accompaniment_score;
  const raw = original || accompaniment;
  const displayedScore = accompaniment || state.result.score;
  const key = (raw ? displayedScore : state.result.arrangement).key_map[0];
  const count = displayedScore.events.filter(e => e.kind === "note").length;
  const inferredCount = displayedScore.events.filter(e => e.kind === "note" && e.review_required).length;
  const shift = raw ? 0 : state.result.arrangement.global_transpose;
  $("score-meta").textContent = key.tonic_spelling + (key.mode === "major" ? " 大调" : " 小调") + " · 1=" + pitchName(key.do_midi) + " · " + count + " 个音符" + (inferredCount ? `（其中 ${inferredCount} 个 * 待校对）` : "") + " · 移调 " + (shift > 0 ? "+" : "") + shift + " 半音" + (state.result.report.audio?.full_song ? ` · 自动拼接 ${state.result.report.audio.chunks.length} 段` : "");
  $("tab-original").setAttribute("aria-selected", original);
  $("tab-arranged").setAttribute("aria-selected", !raw);
  $("tab-accompaniment").setAttribute("aria-selected", Boolean(accompaniment));
  const bars = accompaniment ? state.result.accompaniment_bars : original ? state.result.original_bars : state.result.bars;
  const fragment = document.createDocumentFragment();
  bars.forEach((parts, index) => {
    const bar = element("div", "bar");
    bar.append(element("span", "bar-index", String(index + 1).padStart(2, "0")));
    parts.forEach(part => {
      const note = element("div", "note" + (part.kind === "unknown" ? " unknown" : "") + (part.review_required ? " candidate" : "") + (part.tab && part.tab.startsWith("X[") ? " unplayable" : ""));
      note.dataset.eventId = part.id;
      note.title = (part.pitch_name || part.kind) + " · " + part.duration_q + " 拍" + (part.review_required ? " · 自动补全，需试听校对" : "") + (part.tuplet_group_id ? " · 连音组 " + part.tuplet_group_id : "");
      const symbol = element("div", "note-symbol");
      const match = (part.jianpu || "").match(/^([#b]*)([1-7])([',]*)$/);
      if (part.kind === "rest") symbol.textContent = "0";
      else if (part.kind === "unknown") symbol.textContent = "?";
      else if (match) {
        symbol.append(document.createTextNode(match[1].replaceAll("#","♯").replaceAll("b","♭") + match[2]));
        if (match[3]) {
          const octave = element("span", "octave " + (match[3][0] === "'" ? "up" : "down"));
          for (let i = 0; i < match[3].length; i++) octave.append(element("span", "octave-dot", "•"));
          symbol.append(octave);
          if (match[3][0] === "," && match[3].length > 2) {
            note.style.setProperty("--extra-low-space", 7 * (match[3].length - 2) + "px");
          }
        }
      } else symbol.textContent = part.jianpu || "–";
      if (part.review_required) symbol.append(element("span", "candidate-mark", "*"));
      note.append(symbol, element("span", "note-tab", raw ? (part.pitch_name || "—") : (part.tab || "—")), element("span", "duration", part.duration_q + " 拍"));
      if (part.tie_after) note.append(element("span", "tie", "⌒"));
      bar.append(note);
    });
    fragment.append(bar);
  });
  $("notation").replaceChildren(fragment);
  updatePlaybackHighlight();
}
function renderResult() {
  const result = state.result, report = result.report;
  if (state.view === "accompaniment" && !result.accompaniment_score) state.view = "arranged";
  $("empty").hidden = true; $("results").hidden = false;
  $("result-title").textContent = result.name;
  $("result-state").textContent = "已保存 · 可预览与下载";
  $("status-badge").textContent = report.status === "complete" ? "已生成" : "有待核对区段";
  $("status-badge").classList.toggle("partial", report.status !== "complete");
  const lowVocal = report.audio?.analysis_mode === "vocal" && result.arrangement.global_transpose === 0 &&
    result.score.events.some(e => e.kind === "note" && e.pitch_midi < 60);
  $("raise-octave").hidden = !lowVocal;
  $("play-original").textContent = originalPlaybackLabel();
  const playbackHint = result.downloads["vocal_segment.wav"]
    ? "原唱人声播放所选片段的分离音轨；口琴版播放识别草谱的合成音。若两者音高或节奏不同，请校正草谱。"
    : report.audio && state.resultFileUrl
      ? "原录音播放所选音频片段；口琴版播放识别草谱的合成音。"
      : "两种试听均根据谱面合成，不包含原录音。";
  $("playback-hint").textContent = playbackHint + (result.accompaniment_score
    ? " 伴奏谱演奏按钮播放伴奏旋律候选的合成音；下方可单独试听分离后的伴奏原音。" : "");
  if (state.resultFileUrl) $("result-source-audio").src = state.resultFileUrl;
  else $("result-source-audio").removeAttribute("src");
  $("vocal-playback").hidden = !result.downloads["vocal_segment.wav"];
  if (result.downloads["vocal_segment.wav"]) $("vocal-audio").src = result.downloads["vocal_preview.mp3"] || result.downloads["vocal_segment.wav"];
  else $("vocal-audio").removeAttribute("src");
  const hasAccompanimentScore = Boolean(result.accompaniment_score);
  const hasAccompanimentAudio = Boolean(result.downloads["accompaniment_preview.mp3"]);
  $("tab-accompaniment").hidden = !result.accompaniment_score;
  $("play-accompaniment").hidden = !hasAccompanimentScore;
  $("accompaniment-playback").hidden = !hasAccompanimentAudio;
  if (hasAccompanimentAudio) $("accompaniment-audio").src = result.downloads["accompaniment_preview.mp3"];
  else $("accompaniment-audio").removeAttribute("src");
  renderNotation();
  const warnings = [];
  if (report.unknown_event_ids.length) warnings.push("待核对区段：" + report.unknown_event_ids.join("、") + "。试听会保留对应空白。");
  if (report.unplayable_event_ids.length) warnings.push("当前音表无法演奏：" + report.unplayable_event_ids.join("、") + "。可调整移调或校正音高。");
  for (const warning of report.warnings || []) warnings.push(typeof warning === "string" ? warning : JSON.stringify(warning));
  if (lowVocal) warnings.push("部分主唱音低于默认口琴音域；可点“口琴版升高八度”，原始旋律保持原音高。");
  if (report.status !== "complete" && !warnings.length) warnings.push("此乐谱包含尚未确认的信息，请核对诊断报告。");
  $("warnings").hidden = !warnings.length;
  const list = element("ul");
  warnings.forEach(w => list.append(element("li", "", w)));
  $("warnings").replaceChildren(element("strong", "", "练习前，请核对这些位置"), list);
  $("download-zip").href = result.downloads["bapuluofu.zip"];
  $("download-zip").setAttribute("download", "bapuluofu.zip");
  $("downloads").replaceChildren();
  const labels = {"melody_harmonica.mid":"口琴版 MIDI", "melody_original.mid":"原谱 MIDI", "melody_harmonica.md":"数字简谱", "harmonica_tab.md":"口琴 TAB", "original_score.json":"可编辑 JSON", "report.json":"诊断报告"};
  if (result.downloads["pitch_evidence.json"]) { labels["audio_manifest.json"] = "音频分析信息"; labels["pitch_evidence.json"] = "逐帧音高证据"; }
  if (result.downloads["vocal_segment.wav"]) labels["vocal_segment.wav"] = "分离后主唱 WAV";
  if (result.downloads["accompaniment_melody.mid"]) { labels["accompaniment_melody.mid"] = "伴奏旋律候选 MIDI"; labels["accompaniment_melody.md"] = "伴奏旋律候选简谱"; labels["accompaniment_score.json"] = "伴奏旋律候选 JSON"; labels["accompaniment_preview.mp3"] = "分离后伴奏 MP3"; }
  for (const [name,label] of Object.entries(labels)) { const a = element("a", "", label + " ↗"); a.href = result.downloads[name]; a.download = name; $("downloads").append(a); }
  $("edit-rows").replaceChildren();
  for (const note of result.score.events.filter(e => e.kind === "note")) {
    const row = element("tr");
    row.append(element("td", "", note.id), element("td", "", pitchName(note.pitch_midi)));
    const cell = element("td"), input = element("input");
    input.type = "number"; input.min = 0; input.max = 127; input.value = note.pitch_midi; input.dataset.id = note.id;
    input.setAttribute("aria-label", note.id + " 校正 MIDI 音高"); cell.append(input);
    row.append(cell, element("td", "", note.duration_q + " 拍")); $("edit-rows").append(row);
  }
  $("output-path").textContent = result.output_path;
}
for (const view of ["original", "arranged", "accompaniment"]) $("tab-" + view).onclick = () => { stopAudio(); state.view = view; renderNotation(); };
$("apply-edits").onclick = () => {
  const original = new Map(state.result.score.events.map(e => [e.id, e.pitch_midi]));
  const edits = [];
  for (const input of $("edit-rows").querySelectorAll("input")) {
    const pitch = Number(input.value);
    if (input.value === "" || !Number.isInteger(pitch) || pitch < 0 || pitch > 127) { error("校正音高必须是 0 至 127 之间的整数。"); input.focus(); return; }
    if (pitch !== original.get(input.dataset.id)) edits.push({id:input.dataset.id, pitch_midi:pitch});
  }
  if (!edits.length) return toast("音高没有变化");
  generate(edits);
};
function originalPlaybackName() {
  if (state.result?.downloads["vocal_segment.wav"]) return "原始旋律（原唱人声）";
  if (state.result?.report.audio && state.resultFileUrl) return "原始旋律（录音片段）";
  return "原始旋律（谱面合成）";
}
function originalPlaybackLabel() { return "▶ " + originalPlaybackName() + "试听"; }
function stopAudio(exceptMedia = null) {
  if (state.timer) clearInterval(state.timer);
  if (state.transportTimer) clearInterval(state.transportTimer);
  state.timer = null; state.transportTimer = null;
  if (state.recording && state.recordingListener) {
    for (const [event, listener] of state.recordingListener) state.recording.removeEventListener(event, listener);
  }
  state.recording = null; state.recordingListener = null; state.transport = null; state.seeking = false;
  for (const id of ["audio-preview", "vocal-audio", "accompaniment-audio", "result-source-audio"]) if ($(id) !== exceptMedia) $(id).pause();
  for (const oscillator of state.nodes) { try { oscillator.stop(); } catch (_) {} }
  state.nodes.clear(); state.playingView = null;
  $("playback-seek").hidden = true;
  document.querySelectorAll(".note.is-playing").forEach(note => note.classList.remove("is-playing"));
  $("play-original").textContent = originalPlaybackLabel();
  $("play-arranged").textContent = "▶ 口琴版试听";
  $("play-accompaniment").textContent = "▶ 伴奏谱演奏";
  $("play-original").setAttribute("aria-pressed", "false");
  $("play-arranged").setAttribute("aria-pressed", "false");
  $("play-accompaniment").setAttribute("aria-pressed", "false");
}
function timingForView(view) {
  const score = view === "accompaniment" ? state.result.accompaniment_score
    : view === "arranged" ? state.result.arrangement : state.result.score;
  const events = (score?.events || []).filter(event => event.kind === "note" && event.performed);
  if (!events.length) return {events:[], origin:0, duration:0};
  const origin = Math.min(...events.map(event => event.performed.start_sec));
  const end = Math.max(...events.map(event => event.performed.end_sec));
  return {events, origin, duration:Math.max(0.1, end - origin)};
}
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return Math.floor(value / 60) + ":" + String(value % 60).padStart(2, "0");
}
function updatePlaybackHighlight() {
  document.querySelectorAll("#notation .note").forEach(note => {
    const event = state.transport?.eventsById.get(note.dataset.eventId);
    const position = state.transport?.position ?? -1;
    note.classList.toggle("is-playing", Boolean(event && position >= event.start && position < event.end));
  });
}
function updatePlaybackUI() {
  const transport = state.transport;
  if (!transport) return;
  const position = state.seeking ? transport.position : transport.media
    ? transport.media.currentTime - transport.mediaOffset
    : state.audio && Number.isFinite(transport.startedAt) ? state.audio.currentTime - transport.startedAt : transport.position;
  transport.position = Math.max(0, Math.min(transport.duration, position));
  $("playback-range").max = String(Math.max(0.1, transport.duration));
  $("playback-range").value = String(transport.position);
  $("playback-time").textContent = formatTime(transport.position) + " / " + formatTime(transport.duration);
  updatePlaybackHighlight();
  if (transport.position >= transport.duration && transport.media) stopAudio();
  else if (transport.position >= transport.duration && transport.kind === "synth") stopAudio();
}
function showPlayback(view, label, events, origin, duration, media = null, mediaOffset = 0) {
  const segmentOrigin = state.result.report.audio?.segment_start_sec ?? 0;
  const eventOrigin = media && view !== "arranged" ? segmentOrigin : origin;
  state.transport = {view, label, eventsById:new Map(events.map(event => [event.id, {
    start:event.performed.start_sec - eventOrigin, end:event.performed.end_sec - eventOrigin
  }])), duration, media, mediaOffset, kind:media ? "media" : "synth", position:0};
  $("playback-label").textContent = label;
  $("playback-range").max = String(Math.max(0.1, duration));
  $("playback-range").value = "0";
  $("playback-time").textContent = "0:00 / " + formatTime(duration);
  $("playback-seek").hidden = false;
  state.transportTimer = setInterval(updatePlaybackUI, 60);
  updatePlaybackUI();
}
function activateMediaPlayback(view, media, label, offset, duration) {
  if (state.recording === media && state.transport) return;
  stopAudio(media);
  const timing = timingForView(view);
  const segmentDuration = Number.isFinite(duration) && duration > 0 ? duration : timing.duration;
  showPlayback(view, label, timing.events, timing.origin, segmentDuration, media, offset);
  state.recording = media;
  const finish = () => { if (state.recording === media) stopAudio(); };
  state.recordingListener = [["ended", finish], ["pause", finish]];
  for (const [event, listener] of state.recordingListener) media.addEventListener(event, listener);
  state.playingView = view;
  updatePlaybackButtons(view, true);
}
function updatePlaybackButtons(view, playing) {
  const button = $("play-" + view);
  if (!button) return;
  const label = view === "original" ? originalPlaybackName() : view === "arranged" ? "口琴版" : "伴奏谱";
  button.textContent = playing ? "■ 停止" + label : "▶ " + label + (view === "accompaniment" ? "演奏" : "试听");
  button.setAttribute("aria-pressed", String(playing));
}
function stopScheduledNotes() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  for (const oscillator of state.nodes) { try { oscillator.stop(); } catch (_) {} }
  state.nodes.clear();
}
async function startSynthAt(view, position) {
  const transport = state.transport;
  if (!transport || transport.kind !== "synth") return;
  stopScheduledNotes();
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) throw new Error("当前浏览器不支持合成试听。");
  state.audio ||= new Audio();
  await state.audio.resume();
  const source = timingForView(view).events;
  const origin = transport.origin;
  const startAt = state.audio.currentTime + 0.08;
  transport.startedAt = startAt - position;
  transport.position = position;
  let next = Math.max(0, source.findIndex(note => note.performed.end_sec - origin > position));
  const schedule = () => {
    const now = state.audio.currentTime;
    const current = Math.max(0, now - transport.startedAt);
    while (next < source.length && source[next].performed.start_sec - origin <= current + 0.4) {
      const note = source[next++], noteStart = note.performed.start_sec - origin, noteEnd = note.performed.end_sec - origin;
      if (noteEnd <= current) continue;
      const at = Math.max(now + 0.01, startAt + Math.max(noteStart, current) - position);
      const duration = Math.max(0.015, noteEnd - Math.max(noteStart, current));
      const osc = state.audio.createOscillator(), gain = state.audio.createGain();
      osc.type = "triangle"; osc.frequency.value = 440 * Math.pow(2, ((note.played_pitch_midi ?? note.pitch_midi) - 69) / 12);
      gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(0.12, at + Math.min(0.012, duration / 3)); gain.gain.linearRampToValueAtTime(0, at + duration);
      osc.connect(gain); gain.connect(state.audio.destination); state.nodes.add(osc);
      osc.onended = () => { state.nodes.delete(osc); osc.disconnect(); gain.disconnect(); };
      osc.start(at); osc.stop(at + duration + 0.01);
    }
    updatePlaybackUI();
  };
  state.timer = setInterval(schedule, 80);
  schedule();
}
async function playVersion(view) {
  if (state.playingView === view) return stopAudio();
  stopAudio();
  try {
    state.view = view;
    renderNotation();
    if (view === "original" && state.result.report.audio) {
      const manifest = state.result.report.audio;
      const vocal = Boolean(state.result.downloads["vocal_segment.wav"]);
      const media = vocal ? $("vocal-audio") : state.resultFileUrl ? $("result-source-audio") : null;
      if (media) {
        const begin = vocal ? 0 : Number(manifest.segment_start_sec);
        media.currentTime = begin;
        activateMediaPlayback(view, media, originalPlaybackName(), begin, Number(manifest.decoded_duration_sec));
        await media.play();
        return;
      }
    }
    const timing = timingForView(view);
    if (!timing.events.length) return toast("没有可试听的音符");
    const label = view === "original" ? "原始旋律 · 谱面合成"
      : view === "accompaniment" ? "伴奏旋律候选 · 谱面合成" : "口琴版 · 谱面合成";
    showPlayback(view, label, timing.events, timing.origin, timing.duration);
    state.transport.origin = timing.origin;
    state.playingView = view;
    updatePlaybackButtons(view, true);
    await startSynthAt(view, 0);
  } catch (exc) { stopAudio(); error(exc.message); }
}
$("playback-range").addEventListener("input", () => {
  const transport = state.transport;
  if (!transport) return;
  const position = Number($("playback-range").value);
  if (transport.media) transport.media.currentTime = transport.mediaOffset + position;
  else state.seeking = true;
  transport.position = position;
  $("playback-time").textContent = formatTime(position) + " / " + formatTime(transport.duration);
  updatePlaybackHighlight();
});
$("playback-range").addEventListener("change", () => {
  const transport = state.transport;
  state.seeking = false;
  if (transport && !transport.media) startSynthAt(transport.view, Number($("playback-range").value)).catch(exc => { stopAudio(); error(exc.message); });
});
for (const [id, view, label] of [["vocal-audio","original","分离后主唱 · 对照草谱"],["accompaniment-audio","accompaniment","分离后伴奏 · 伴奏旋律候选"]]) {
  const media = $(id);
  media.addEventListener("play", () => {
    if (state.recording === media) return;
    const manifest = state.result?.report.audio;
    const duration = Math.min(Number.isFinite(media.duration) ? media.duration : Infinity, Number(manifest?.decoded_duration_sec) || media.duration);
    activateMediaPlayback(view, media, label, 0, duration);
    updatePlaybackUI();
  });
  media.addEventListener("timeupdate", () => { if (state.recording === media) updatePlaybackUI(); });
}
$("play-original").onclick = () => playVersion("original");
$("play-arranged").onclick = () => playVersion("arranged");
$("play-accompaniment").onclick = () => playVersion("accompaniment");
$("raise-octave").onclick = () => { transpose(12); generate([]); };
$("copy-path").onclick = async () => { try { await navigator.clipboard.writeText(state.result.output_path); toast("保存路径已复制"); } catch (_) { toast("请选中上方路径手动复制"); } };
$("reset").onclick = () => {
  stopAudio(); clearFilePreview();
  $("result-source-audio").removeAttribute("src");
  if (state.resultFileUrl) URL.revokeObjectURL(state.resultFileUrl);
  state.resultFileUrl = null;
  state.file = null; state.example = false; state.audioExample = false; state.corrected = false; state.result = null; state.view = "arranged";
  $("audio-full").checked = false; updateAudioRange();
  for (const id of ["file","profile","sidecar","track","channel","key","do-midi","midi-origin"]) $(id).value = "";
  transpose(0); $("file-name").textContent = "拖入文件，或点此选择"; $("file-description").textContent = "MP3 / WAV / MP4 / M4A / MIDI / JSON · 最多 96 MB";
  $("midi-fields").hidden = true; $("audio-fields").hidden = true; $("results").hidden = true; $("empty").hidden = false; $("advanced").open = false;
  $("result-state").textContent = "等待导入"; error(""); busy(false);
};
window.addEventListener("pagehide", stopAudio);
