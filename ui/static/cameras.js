(function () {
  var cameras = [];
  var disks = [];
  var episodes = [];
  var status = {};
  var selected = "";
  var cameraTab = "camera";
  var usage = { used_bytes: 0, motion_bytes: 0, quota_bytes: 0 };
  var watching = false;
  var lastLease = 0;
  var logEntries = [];

  function csrf() {
    var row = document.cookie.split("; ").find(function (item) { return item.trim().indexOf("bb_csrf=") === 0; });
    return row ? decodeURIComponent(row.split("=").slice(1).join("=")) : "";
  }

  function field(name) {
    return document.querySelector("[data-field='" + name + "']");
  }

  function blank(id, index) {
    return {
      id: id,
      name: "Камера " + (index + 1),
      enabled: true,
      url: "",
      stream: "main",
      rtsp_transport: "tcp",
      resolution: "source",
      width: null,
      height: null,
      fps: null,
      codec: "libx264",
      profile: "high",
      preset: "ultrafast",
      bitrate_kbps: 2000,
      gop_sec: 2,
      audio: "none",
      audio_bitrate_kbps: 128,
      container: "mp4",
      segment_sec: 60,
      motion: false,
      motion_noise: 32,
      motion_threshold: 2,
      motion_min_frames: 2,
      motion_gap_sec: 10,
    };
  }

  function nextId(items) {
    var used = {};
    items.forEach(function (camera) { used[camera.id] = true; });
    var n = items.length + 1;
    while (used["cam" + n]) n += 1;
    return "cam" + n;
  }

  function current() {
    return cameras.find(function (camera) { return camera.id === selected; }) || null;
  }

  function numberOrNull(value) {
    if (value === "" || value === null || value === undefined) return null;
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function readForm() {
    var camera = current();
    if (!camera) return;
    camera.name = field("name").value.trim();
    camera.enabled = field("enabled").checked;
    camera.url = field("url").value.trim();
    camera.stream = field("stream").value;
    camera.rtsp_transport = field("rtsp_transport").value;
    camera.resolution = field("resolution").value;
    camera.width = numberOrNull(field("width").value);
    camera.height = numberOrNull(field("height").value);
    camera.fps = numberOrNull(field("fps").value);
    camera.codec = field("codec").value;
    camera.profile = field("profile").value;
    camera.preset = field("preset").value;
    camera.bitrate_kbps = Number(field("bitrate_kbps").value);
    camera.gop_sec = Number(field("gop_sec").value);
    camera.audio = field("audio").value;
    camera.audio_bitrate_kbps = Number(field("audio_bitrate_kbps").value);
    camera.container = field("container").value;
    camera.segment_sec = Number(field("segment_sec").value);
    camera.motion = field("motion").checked;
    camera.motion_noise = Number(field("motion_noise").value);
    camera.motion_threshold = Number(field("motion_threshold").value);
    camera.motion_min_frames = Number(field("motion_min_frames").value);
    camera.motion_gap_sec = Number(field("motion_gap_sec").value);
  }

  function writeForm() {
    var camera = current();
    var steps = document.getElementById("camera-steps");
    var empty = document.getElementById("camera-empty");
    var live = document.getElementById("camera-live");
    var has = !!camera;
    steps.hidden = !has;
    empty.hidden = has;
    live.hidden = !has;
    document.getElementById("camera-journal-wrap").hidden = !has;
    if (!has) return;
    field("name").value = camera.name || "";
    field("id").value = camera.id;
    field("enabled").checked = !!camera.enabled;
    field("url").value = camera.url || "";
    field("stream").value = camera.stream || "main";
    field("rtsp_transport").value = camera.rtsp_transport || "tcp";
    field("resolution").value = camera.resolution || "source";
    field("width").value = camera.width || "";
    field("height").value = camera.height || "";
    field("fps").value = camera.fps || "";
    field("codec").value = camera.codec || "libx264";
    field("profile").value = camera.profile || "high";
    field("preset").value = camera.preset || "ultrafast";
    field("bitrate_kbps").value = camera.bitrate_kbps;
    field("gop_sec").value = camera.gop_sec;
    field("audio").value = camera.audio || "none";
    field("audio_bitrate_kbps").value = camera.audio_bitrate_kbps;
    field("container").value = camera.container || "mp4";
    field("segment_sec").value = camera.segment_sec;
    field("motion").checked = !!camera.motion;
    field("motion_noise").value = camera.motion_noise || 32;
    field("motion_threshold").value = camera.motion_threshold || 2;
    field("motion_min_frames").value = camera.motion_min_frames || 2;
    field("motion_gap_sec").value = camera.motion_gap_sec ?? 10;
    syncPicture();
    syncMotion();
    showCameraTab(cameraTab);
    renderEstimate();
    renderEpisodes();
    renderLive();
  }

  function showCameraTab(name) {
    cameraTab = name || "camera";
    document.querySelectorAll("[data-camera-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-camera-panel") !== cameraTab;
    });
    document.querySelectorAll("[data-camera-tab]").forEach(function (button) {
      var on = button.getAttribute("data-camera-tab") === cameraTab;
      button.classList.toggle("is-on", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function syncMotion() {
    var box = document.querySelector("[data-motion-fields]");
    if (box) box.hidden = !field("motion").checked;
  }

  function fillDisks(items, preferred) {
    disks = items || [];
    var select = field("storage_resource_id");
    var keep = preferred || select.value || "storage:data";
    select.replaceChildren();
    disks.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.resource_id;
      option.textContent = item.name + (item.path ? " · " + item.path : "");
      option.setAttribute("data-path", item.path || "");
      select.appendChild(option);
    });
    if (!select.options.length) {
      var fallback = document.createElement("option");
      fallback.value = "storage:data";
      fallback.textContent = "Внутренний диск Hub";
      fallback.setAttribute("data-path", "/data");
      select.appendChild(fallback);
    }
    if (Array.prototype.some.call(select.options, function (option) { return option.value === keep; })) {
      select.value = keep;
    }
  }

  function folder() {
    var select = field("storage_resource_id");
    var option = select.options[select.selectedIndex];
    var base = ((option && option.getAttribute("data-path")) || "/data").replace(/[\\/]+$/, "");
    var sub = (field("video_subdir").value || "video").trim().replace(/^[\\/]+/, "") || "video";
    return base + "/" + sub;
  }

  function estimateMib(videoKbps, audioKbps, seconds) {
    return (Number(videoKbps) + Number(audioKbps)) * Number(seconds) / 8 / 1024;
  }

  function label(mib) {
    if (mib >= 1024) return (mib / 1024).toFixed(1) + " ГиБ";
    return mib.toFixed(1) + " МиБ";
  }

  function audioKbps(camera) {
    return camera.audio === "none" ? 0 : Number(camera.audio_bitrate_kbps) || 0;
  }

  function syncPicture() {
    var custom = field("resolution").value === "custom";
    document.querySelectorAll("[data-custom]").forEach(function (node) { node.hidden = !custom; });
    if (custom) {
      if (!field("width").value) field("width").value = "1920";
      if (!field("height").value) field("height").value = "1080";
    }
    var camera = current();
    var codec = field("codec").value;
    var copy = camera && (codec === "copy" || field("audio").value === "copy");
    document.getElementById("camera-copy-note").hidden = !copy;
    var profile = document.querySelector("[data-h264-profile]");
    var preset = document.querySelector("[data-encoder-preset]");
    var audioRate = document.querySelector("[data-audio-bitrate]");
    if (profile) profile.hidden = codec !== "libx264";
    if (preset) preset.hidden = codec !== "libx264" && codec !== "libx265";
    if (audioRate) audioRate.hidden = field("audio").value !== "aac";
  }

  function formatBytes(bytes) {
    var value = Number(bytes) || 0;
    var gb = value / (1024 * 1024 * 1024);
    if (gb >= 10) return gb.toFixed(1) + " ГБ";
    if (gb >= 0.1) return gb.toFixed(2) + " ГБ";
    return Math.max(0, Math.round(value / (1024 * 1024))) + " МБ";
  }

  function renderUsage() {
    var node = document.getElementById("camera-usage");
    var bar = document.getElementById("camera-quota-bar");
    var fill = document.getElementById("camera-quota-fill");
    if (!node || !bar || !fill) return;
    var quotaGb = numberOrNull(field("motion_quota_gb").value);
    var quota = quotaGb === null ? Number(usage.quota_bytes) || 0 : quotaGb * 1024 * 1024 * 1024;
    var used = Number(usage.used_bytes) || 0;
    var motionBytes = Number(usage.motion_bytes) || 0;
    if (!quota) {
      bar.hidden = true;
      node.textContent = "Автоочистка выключена. Записи движения и инцидентов остаются на диске.";
      return;
    }
    bar.hidden = false;
    fill.style.width = Math.max(0, Math.min(100, (used / quota) * 100)) + "%";
    bar.classList.toggle("is-over", used > quota);
    var text = "Занято " + formatBytes(used) + " из " + formatBytes(quota) + ". Записи движения: " + formatBytes(motionBytes) + ".";
    if (used > quota && motionBytes === 0) text += " Лимит превышен записями, которые очистка не трогает.";
    else if (used > quota) text += " Старые записи движения будут удалены.";
    node.textContent = text;
  }

  function renderEstimate() {
    readForm();
    var camera = current();
    var node = document.getElementById("camera-estimate");
    var path = document.getElementById("camera-path");
    if (!camera) {
      node.textContent = "";
      path.textContent = "";
      return;
    }
    var extra = audioKbps(camera);
    var episode = estimateMib(camera.bitrate_kbps, extra, camera.segment_sec);
    var hour = estimateMib(camera.bitrate_kbps, extra, 3600);
    var day = estimateMib(camera.bitrate_kbps, extra, 86400);
    node.textContent = "Один эпизод: " + label(episode) + ". Если записывать без пауз: час " + label(hour) + ", сутки " + label(day) + ".";
    path.textContent = folder() + "/" + camera.id + "/ГГГГММДД_ЧЧММСС_эпизод." + (camera.container === "mkv" ? "mkv" : "mp4");
  }

  function openEpisode(id) {
    return episodes.find(function (row) {
      return row.camera_id === id && (row.state === "queued" || row.state === "recording" || row.state === "stopping");
    }) || null;
  }

  function activity(id, camera) {
    var row = openEpisode(id);
    if (row && row.incident_id) return { kind: "incident", text: "Запись идёт: авария" };
    if (row && row.capture_from) return { kind: "motion", text: "Движение есть, запись идёт" };
    if (row) return { kind: "manual", text: "Запись идёт: ручной старт" };
    if (camera && camera.motion) return { kind: "idle", text: "Движения нет" };
    return null;
  }

  function statusText(id) {
    var camera = cameras.find(function (item) { return item.id === id; });
    var live = activity(id, camera);
    if (live && live.kind !== "idle") return live.text;
    var row = status[id];
    if (!row || row.state === "stopped") return live ? live.text : "готова";
    if (row.state === "buffering") return "буфер активен";
    if (row.state === "recording") return live ? live.text : "идёт запись";
    if (row.state === "preview") return "просмотр";
    return "ошибка";
  }

  function renderNav() {
    var nav = document.getElementById("camera-nav");
    nav.replaceChildren();
    if (!cameras.length) {
      var empty = document.createElement("p");
      empty.className = "bb-muted";
      empty.textContent = "Камер пока нет.";
      nav.appendChild(empty);
      return;
    }
    cameras.forEach(function (camera) {
      var button = document.createElement("button");
      button.type = "button";
      var live = activity(camera.id, camera);
      button.className = "bb-camera-pick" + (camera.id === selected ? " is-selected" : "") + (camera.enabled ? "" : " is-off") + (live && live.kind === "motion" ? " is-motion" : "");
      var name = document.createElement("strong");
      name.textContent = camera.name || camera.id;
      var meta = document.createElement("span");
      meta.textContent = [camera.id, statusText(camera.id)].filter(Boolean).join(" · ");
      button.appendChild(name);
      button.appendChild(meta);
      button.addEventListener("click", function () { select(camera.id); });
      nav.appendChild(button);
    });
    document.getElementById("camera-add").disabled = cameras.length >= 16;
  }

  function when(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString("ru-RU");
  }

  function episodeTitle(row) {
    var motionClip = row.capture_from && !row.incident_id;
    if (row.state === "finished") return motionClip ? "Движение записано" : "Эпизод закончен";
    if (row.state === "error") return "Ошибка эпизода";
    if (row.state === "recording" || row.state === "queued") return motionClip ? "Запись по движению" : (row.state === "recording" ? "Идёт запись" : "В очереди");
    return "В очереди";
  }

  function renderEpisodes() {
    var list = document.getElementById("camera-episodes");
    if (!list) return;
    list.replaceChildren();
    var rows = episodes.filter(function (row) { return row.camera_id === selected; }).slice(0, 8);
    if (!rows.length) {
      var empty = document.createElement("li");
      empty.className = "bb-muted";
      empty.textContent = "Событий ещё не было.";
      list.appendChild(empty);
      return;
    }
    rows.forEach(function (row) {
      var item = document.createElement("li");
      var kind = document.createElement("span");
      kind.className = "bb-episode-kind";
      kind.textContent = (row.capture_from && !row.incident_id) ? "движение" : (row.incident_id ? "инцидент" : "эпизод");
      var title = document.createElement("strong");
      title.textContent = episodeTitle(row);
      item.appendChild(kind);
      item.appendChild(title);
      var meta = document.createElement("span");
      meta.textContent = [when(row.ended_at || row.started_at), fileLabel(row), row.message].filter(Boolean).join(" · ");
      item.appendChild(meta);
      list.appendChild(item);
    });
  }

  function fileLabel(row) {
    var path = (row.paths && row.paths[0]) || row.path || "";
    if (!path) return "";
    var parts = String(path).split(/[\\/]/);
    var name = parts[parts.length - 1];
    if (row.paths && row.paths.length > 1) name += " · фрагментов " + row.paths.length;
    return name;
  }

  function renderLive() {
    var camera = current();
    var state = camera ? statusText(camera.id) : "";
    var node = document.getElementById("camera-live-status");
    var watch = document.getElementById("camera-watch");
    var record = document.getElementById("camera-record");
    if (!camera) return;
    var row = status[camera.id];
    var live = activity(camera.id, camera);
    if (row && row.state === "error" && row.message) state = row.message;
    node.textContent = watching ? ("Просмотр · " + state) : state;
    var signal = document.getElementById("camera-signal");
    var frame = document.getElementById("camera-preview-frame");
    if (signal && frame) {
      signal.hidden = !live;
      signal.className = "bb-signal" + (live ? " is-" + live.kind : "");
      document.getElementById("camera-signal-text").textContent = live ? live.text : "";
      frame.classList.toggle("is-motion", !!(live && live.kind === "motion"));
    }
    watch.innerHTML = watching ? "<i class=\"bi bi-stop-circle\"></i> Остановить просмотр" : "<i class=\"bi bi-eye\"></i> Смотреть";
    var busy = row && row.state === "recording";
    if (busy && watching) {
      watching = false;
      hidePreview();
    }
    record.disabled = busy;
    record.innerHTML = busy ? "<i class=\"bi bi-record-circle\"></i> Идёт запись" : "<i class=\"bi bi-record-circle\"></i> Начать запись";
  }

  function select(id) {
    var previous = selected;
    readForm();
    if (watching && previous && previous !== id) sendPreview(false, previous);
    watching = false;
    selected = id;
    hidePreview();
    writeForm();
    renderNav();
    loadLogs();
  }

  function payload() {
    readForm();
    return {
      storage_resource_id: field("storage_resource_id").value || "storage:data",
      video_subdir: (field("video_subdir").value || "video").trim() || "video",
      incident_pre_sec: numberOrNull(field("incident_pre_sec").value) ?? 10,
      incident_post_sec: numberOrNull(field("incident_post_sec").value) ?? 15,
      motion_quota_gb: numberOrNull(field("motion_quota_gb").value) ?? 20,
      cameras: cameras.map(function (camera) {
        return {
          id: camera.id,
          name: camera.name,
          enabled: !!camera.enabled,
          url: camera.url,
          stream: camera.stream,
          rtsp_transport: camera.rtsp_transport,
          resolution: camera.resolution,
          width: camera.resolution === "custom" ? camera.width : null,
          height: camera.resolution === "custom" ? camera.height : null,
          fps: camera.fps,
          codec: camera.codec,
          profile: camera.profile,
          preset: camera.preset,
          bitrate_kbps: camera.bitrate_kbps,
          gop_sec: camera.gop_sec,
          audio: camera.audio,
          audio_bitrate_kbps: camera.audio_bitrate_kbps,
          container: camera.container,
          segment_sec: camera.segment_sec,
          motion: !!camera.motion,
          motion_noise: Number(camera.motion_noise) || 32,
          motion_threshold: Number(camera.motion_threshold) || 2,
          motion_min_frames: Number(camera.motion_min_frames) || 2,
          motion_gap_sec: camera.motion_gap_sec ?? 10,
        };
      }),
    };
  }

  function errorText(body) {
    var details = body && body.details;
    if (Array.isArray(details) && details.length && details[0].msg) return String(details[0].msg);
    return (body && body.message) || "Не удалось сохранить";
  }

  function clock(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("ru-RU");
  }

  function renderLogs() {
    var box = document.getElementById("camera-journal");
    if (!box) return;
    box.replaceChildren();
    if (!logEntries.length) {
      var empty = document.createElement("p");
      empty.className = "bb-journal-empty";
      empty.textContent = "Журнал пуст. Начните просмотр или запись, чтобы увидеть ffmpeg.";
      box.appendChild(empty);
      return;
    }
    logEntries.forEach(function (entry) {
      var row = document.createElement("div");
      row.className = "bb-log-row" + (entry.level === "error" ? " is-error" : "") + (entry.source === "hub" ? " is-lifecycle" : "") + (String(entry.line || "").indexOf("Причина:") >= 0 ? " is-reason" : "");
      var time = document.createElement("span");
      time.className = "bb-log-time";
      time.textContent = clock(entry.timestamp);
      var source = document.createElement("span");
      source.className = "bb-log-src";
      source.textContent = entry.source || "ffmpeg";
      var message = document.createElement("span");
      message.className = "bb-log-msg";
      message.textContent = entry.line || "";
      row.appendChild(time);
      row.appendChild(source);
      row.appendChild(message);
      box.appendChild(row);
    });
    box.scrollTop = box.scrollHeight;
  }

  function loadLogs() {
    if (!selected) {
      logEntries = [];
      renderLogs();
      return;
    }
    var cameraId = selected;
    fetch("/api/v1/cameras/" + encodeURIComponent(cameraId) + "/logs?tail=500", { credentials: "same-origin" }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (body) {
      if (!body || cameraId !== selected) return;
      logEntries = body.entries || [];
      renderLogs();
    }).catch(function () { return null; });
  }

  function apply(body, replace) {
    status = body.status || {};
    episodes = body.episodes || [];
    usage = body.storage_usage || usage;
    if (replace) {
      cameras = (body.config && body.config.cameras) || [];
      fillDisks(body.storage_resources, (body.config && body.config.storage_resource_id) || "storage:data");
      field("video_subdir").value = (body.config && body.config.video_subdir) || "video";
      field("incident_pre_sec").value = (body.config && body.config.incident_pre_sec) ?? 10;
      field("incident_post_sec").value = (body.config && body.config.incident_post_sec) ?? 15;
      field("motion_quota_gb").value = (body.config && body.config.motion_quota_gb) ?? 20;
      if (!cameras.some(function (camera) { return camera.id === selected; })) {
        selected = cameras.length ? cameras[0].id : "";
      }
      writeForm();
      loadLogs();
    } else {
      fillDisks(body.storage_resources, field("storage_resource_id").value);
      renderEpisodes();
      renderLive();
    }
    renderNav();
    renderUsage();
  }

  function save() {
    var message = document.getElementById("camera-message");
    message.textContent = "Сохранение…";
    return fetch("/api/v1/cameras", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(payload()),
    }).then(function (response) {
      return response.json().then(function (body) { return { ok: response.ok, body: body }; });
    }).then(function (result) {
      if (!result.ok) {
        message.textContent = errorText(result.body);
        return false;
      }
      apply(result.body, true);
      message.textContent = "Сохранено";
      return true;
    }).catch(function () {
      message.textContent = "Не удалось сохранить";
      return false;
    });
  }

  function cameraBody(id) {
    readForm();
    var camera = cameras.find(function (item) { return item.id === id; });
    if (!camera) return null;
    return {
      id: camera.id,
      name: camera.name,
      enabled: !!camera.enabled,
      url: camera.url,
      stream: camera.stream,
      rtsp_transport: camera.rtsp_transport,
      resolution: camera.resolution,
      width: camera.resolution === "custom" ? camera.width : null,
      height: camera.resolution === "custom" ? camera.height : null,
      fps: camera.fps,
      codec: camera.codec,
      profile: camera.profile,
      preset: camera.preset,
      bitrate_kbps: camera.bitrate_kbps,
      gop_sec: camera.gop_sec,
      audio: camera.audio,
      audio_bitrate_kbps: camera.audio_bitrate_kbps,
      container: camera.container,
      segment_sec: camera.segment_sec,
    };
  }

  function hidePreview() {
    var image = document.getElementById("camera-preview");
    image.hidden = true;
    image.removeAttribute("src");
    document.getElementById("camera-preview-empty").hidden = false;
  }

  function refreshPreviewImage() {
    if (!watching || !selected) return;
    var image = document.getElementById("camera-preview");
    image.src = "/api/v1/cameras/" + encodeURIComponent(selected) + "/preview.jpg?t=" + Date.now();
  }

  function sendPreview(active, id) {
    var cameraId = id || selected;
    if (!cameraId) return Promise.resolve();
    var body = { active: !!active };
    if (active) {
      var camera = cameraBody(cameraId);
      if (!camera || !camera.url) {
        document.getElementById("camera-live-status").textContent = "Укажите RTSP URL";
        watching = false;
        renderLive();
        return Promise.resolve();
      }
      body.camera = camera;
    }
    return fetch("/api/v1/cameras/" + encodeURIComponent(cameraId) + "/preview", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (payload) {
          document.getElementById("camera-live-status").textContent = errorText(payload);
          watching = false;
          renderLive();
        });
      }
      return null;
    }).catch(function () {
      document.getElementById("camera-live-status").textContent = "Просмотр недоступен";
      watching = false;
      renderLive();
    });
  }

  document.getElementById("camera-add").addEventListener("click", function () {
    readForm();
    if (cameras.length >= 16) return;
    var camera = blank(nextId(cameras), cameras.length);
    cameras.push(camera);
    selected = camera.id;
    watching = false;
    hidePreview();
    writeForm();
    renderNav();
  });

  document.getElementById("camera-delete").addEventListener("click", function () {
    if (!selected) return;
    if (watching) sendPreview(false, selected);
    watching = false;
    cameras = cameras.filter(function (camera) { return camera.id !== selected; });
    selected = cameras.length ? cameras[0].id : "";
    hidePreview();
    writeForm();
    renderNav();
    document.getElementById("camera-message").textContent = "Удаление применится после сохранения";
  });

  document.getElementById("cameras-form").addEventListener("submit", function (event) {
    event.preventDefault();
    save();
  });

  document.getElementById("cameras-form").addEventListener("input", function () {
    syncPicture();
    syncMotion();
    renderEstimate();
    renderUsage();
  });
  document.getElementById("cameras-form").addEventListener("change", function () {
    syncPicture();
    syncMotion();
    renderEstimate();
    renderUsage();
    renderNav();
  });
  document.getElementById("camera-tabs").addEventListener("click", function (event) {
    var button = event.target.closest("[data-camera-tab]");
    if (!button) return;
    showCameraTab(button.getAttribute("data-camera-tab"));
  });

  document.getElementById("camera-watch").addEventListener("click", function () {
    if (!selected) return;
    watching = !watching;
    if (!watching) {
      hidePreview();
      sendPreview(false);
    } else {
      lastLease = Date.now();
      document.getElementById("camera-preview-empty").textContent = "Ждём кадр от камеры…";
      document.getElementById("camera-preview-empty").hidden = false;
      sendPreview(true);
    }
    renderLive();
  });

  document.getElementById("camera-preview").addEventListener("load", function () {
    if (!watching) return;
    document.getElementById("camera-preview").hidden = false;
    document.getElementById("camera-preview-empty").hidden = true;
  });

  document.getElementById("camera-preview").addEventListener("error", function () {
    if (!watching) return;
    document.getElementById("camera-preview").hidden = true;
    document.getElementById("camera-preview-empty").hidden = false;
    document.getElementById("camera-preview-empty").textContent = "Кадр ещё не готов. Проверьте URL и что сервис записи запущен.";
  });

  document.getElementById("camera-record").addEventListener("click", function () {
    if (!selected) return;
    var message = document.getElementById("camera-message");
    save().then(function (ok) {
      if (!ok) return;
      message.textContent = "Старт эпизода…";
      return fetch("/api/v1/cameras/" + encodeURIComponent(selected) + "/episodes", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf() },
      }).then(function (response) {
        return response.json().then(function (body) { return { ok: response.ok, body: body }; });
      }).then(function (result) {
        if (!result.ok) {
          message.textContent = errorText(result.body);
          return;
        }
        message.textContent = "Эпизод поставлен в очередь";
        return fetch("/api/v1/cameras", { credentials: "same-origin" }).then(function (response) {
          return response.ok ? response.json() : null;
        }).then(function (body) {
          if (body) apply(body, false);
        });
      });
    }).catch(function () {
      message.textContent = "Не удалось начать запись";
    });
  });

  fetch("/api/v1/cameras", { credentials: "same-origin" }).then(function (response) {
    if (!response.ok) throw new Error("load");
    return response.json();
  }).then(function (body) {
    apply(body, true);
  }).catch(function () {
    document.getElementById("camera-message").textContent = "Не удалось загрузить камеры";
  });

  window.setInterval(function () {
    fetch("/api/v1/cameras", { credentials: "same-origin" }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (body) {
      if (body) apply(body, false);
      loadLogs();
    }).catch(function () { return null; });
  }, 3000);

  window.addEventListener("bb-hub-event", function (event) {
    var message = event.detail || {};
    if (message.topic !== "logs" || !selected) return;
    var payload = message.payload || {};
    if (payload.vm_id !== "camera:" + selected || !payload.line) return;
    logEntries.push({
      level: payload.level || "info",
      source: "ffmpeg",
      line: payload.line,
      timestamp: payload.timestamp,
    });
    logEntries = logEntries.slice(-500);
    renderLogs();
  });

  window.setInterval(function () {
    if (!watching || !selected) return;
    refreshPreviewImage();
    if (Date.now() - lastLease > 2000) {
      lastLease = Date.now();
      sendPreview(true);
    }
  }, 1000);
})();
