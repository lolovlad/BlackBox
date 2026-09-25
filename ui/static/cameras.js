(function () {
  var RESOLUTIONS = [
    ["source", "исходное"],
    ["3840x2160", "3840×2160"],
    ["1920x1080", "1920×1080"],
    ["1280x720", "1280×720"],
    ["640x480", "640×480"],
    ["custom", "своё"],
  ];
  var CODECS = [
    ["libx264", "H.264"],
    ["libx265", "H.265"],
    ["mjpeg", "MJPEG"],
    ["copy", "copy"],
    ["h264_v4l2m2m", "H.264 аппаратный Pi"],
  ];
  var PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"];
  var status = {};
  var cameras = [];

  function csrf() {
    var row = document.cookie.split("; ").find(function (item) { return item.trim().indexOf("bb_csrf=") === 0; });
    return row ? decodeURIComponent(row.split("=").slice(1).join("=")) : "";
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
      width: 1920,
      height: 1080,
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
    };
  }

  function nextId(items) {
    var used = {};
    items.forEach(function (camera) { used[camera.id] = true; });
    var n = items.length + 1;
    while (used["cam" + n]) n += 1;
    return "cam" + n;
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

  function field(labelText, control) {
    var wrap = document.createElement("label");
    wrap.appendChild(document.createTextNode(labelText));
    wrap.appendChild(control);
    return wrap;
  }

  function select(value, options) {
    var node = document.createElement("select");
    options.forEach(function (option) {
      var item = document.createElement("option");
      item.value = option[0];
      item.textContent = option[1];
      if (option[0] === value) item.selected = true;
      node.appendChild(item);
    });
    return node;
  }

  function input(type, value, attrs) {
    var node = document.createElement("input");
    node.type = type;
    if (type === "checkbox") node.checked = !!value;
    else if (value !== null && value !== undefined) node.value = value;
    Object.keys(attrs || {}).forEach(function (key) { node[key] = attrs[key]; });
    return node;
  }

  function readRow(card, camera) {
    function value(name) {
      return card.querySelector("[data-field='" + name + "']");
    }
    camera.name = value("name").value.trim();
    camera.enabled = value("enabled").checked;
    camera.url = value("url").value.trim();
    camera.stream = value("stream").value;
    camera.rtsp_transport = value("rtsp_transport").value;
    camera.resolution = value("resolution").value;
    camera.width = Number(value("width").value) || null;
    camera.height = Number(value("height").value) || null;
    var fps = value("fps").value.trim();
    camera.fps = fps === "" ? null : Number(fps);
    camera.codec = value("codec").value;
    camera.profile = value("profile").value;
    camera.preset = value("preset").value;
    camera.bitrate_kbps = Number(value("bitrate_kbps").value);
    camera.gop_sec = Number(value("gop_sec").value);
    camera.audio = value("audio").value;
    camera.audio_bitrate_kbps = Number(value("audio_bitrate_kbps").value);
    camera.container = value("container").value;
    camera.segment_sec = Number(value("segment_sec").value);
  }

  function readAll() {
    document.querySelectorAll("[data-camera-id]").forEach(function (card) {
      var camera = cameras.find(function (item) { return item.id === card.getAttribute("data-camera-id"); });
      if (camera) readRow(card, camera);
    });
  }

  function statusText(id) {
    var row = status[id];
    if (!row || row.state === "stopped") return "остановлена";
    if (row.state === "recording") return "пишет";
    return row.message || "ошибка";
  }

  function renderEstimate() {
    readAll();
    var body = document.getElementById("camera-estimate-body");
    var note = document.getElementById("camera-copy-note");
    body.replaceChildren();
    var totals = { fragment: 0, hour: 0, day: 0, copy: false };
    cameras.forEach(function (camera) {
      if (!camera.enabled) return;
      var extra = audioKbps(camera);
      var fragment = estimateMib(camera.bitrate_kbps, extra, camera.segment_sec);
      var hour = estimateMib(camera.bitrate_kbps, extra, 3600);
      var day = estimateMib(camera.bitrate_kbps, extra, 86400);
      var tr = document.createElement("tr");
      [camera.name || camera.id, label(fragment), label(hour), label(day)].forEach(function (text) {
        var td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
      if (camera.enabled) {
        totals.fragment += fragment;
        totals.hour += hour;
        totals.day += day;
        if (camera.codec === "copy" || camera.audio === "copy") totals.copy = true;
      }
    });
    var total = document.createElement("tr");
    ["Все включённые", label(totals.fragment), label(totals.hour), label(totals.day)].forEach(function (text) {
      var td = document.createElement("td");
      var strong = document.createElement("strong");
      strong.textContent = text;
      td.appendChild(strong);
      total.appendChild(td);
    });
    body.appendChild(total);
    note.hidden = !totals.copy;
  }

  function bind(node, camera, name) {
    node.setAttribute("data-field", name);
    node.addEventListener("input", function () {
      if (name === "resolution") syncCustom(node.closest("[data-camera-id]"), node.value);
      renderEstimate();
    });
    node.addEventListener("change", renderEstimate);
    return node;
  }

  function syncCustom(card, resolution) {
    card.querySelectorAll("[data-custom]").forEach(function (node) {
      node.hidden = resolution !== "custom";
    });
  }

  function render() {
    var root = document.getElementById("camera-rows");
    root.replaceChildren();
    cameras.forEach(function (camera) {
      var card = document.createElement("fieldset");
      card.className = "bb-camera-row";
      card.setAttribute("data-camera-id", camera.id);
      var legend = document.createElement("legend");
      legend.textContent = camera.id;
      card.appendChild(legend);
      var state = document.createElement("p");
      state.className = "bb-camera-status";
      state.setAttribute("data-status", camera.id);
      state.textContent = statusText(camera.id);
      card.appendChild(state);
      var grid = document.createElement("div");
      grid.className = "bb-camera-grid";
      var enabled = bind(input("checkbox", camera.enabled), camera, "enabled");
      var enabledLabel = field("", enabled);
      enabledLabel.className = "bb-checkbox-label";
      enabledLabel.insertBefore(document.createTextNode("Включена"), enabled);
      grid.appendChild(field("Имя", bind(input("text", camera.name, { required: true, maxLength: 128 }), camera, "name")));
      grid.appendChild(enabledLabel);
      grid.appendChild(field("RTSP URL", bind(input("text", camera.url, { placeholder: "rtsp://…" }), camera, "url")));
      grid.appendChild(field("Поток", bind(select(camera.stream, [["main", "main"], ["sub", "sub"]]), camera, "stream")));
      grid.appendChild(field("Транспорт", bind(select(camera.rtsp_transport, [["tcp", "tcp"], ["udp", "udp"]]), camera, "rtsp_transport")));
      grid.appendChild(field("Разрешение", bind(select(camera.resolution, RESOLUTIONS), camera, "resolution")));
      var width = field("Ширина", bind(input("number", camera.width, { min: 160, max: 7680 }), camera, "width"));
      var height = field("Высота", bind(input("number", camera.height, { min: 120, max: 4320 }), camera, "height"));
      width.setAttribute("data-custom", "1");
      height.setAttribute("data-custom", "1");
      grid.appendChild(width);
      grid.appendChild(height);
      grid.appendChild(field("FPS", bind(input("number", camera.fps === null ? "" : camera.fps, { min: 1, max: 60, placeholder: "исходный" }), camera, "fps")));
      grid.appendChild(field("Кодек", bind(select(camera.codec, CODECS), camera, "codec")));
      grid.appendChild(field("Профиль H.264", bind(select(camera.profile, [["baseline", "baseline"], ["main", "main"], ["high", "high"]]), camera, "profile")));
      grid.appendChild(field("Preset", bind(select(camera.preset, PRESETS.map(function (item) { return [item, item]; })), camera, "preset")));
      grid.appendChild(field("Битрейт, кбит/с", bind(input("number", camera.bitrate_kbps, { min: 64, max: 50000, required: true }), camera, "bitrate_kbps")));
      grid.appendChild(field("Ключевой кадр, с", bind(input("number", camera.gop_sec, { min: 1, max: 30, required: true }), camera, "gop_sec")));
      grid.appendChild(field("Звук", bind(select(camera.audio, [["none", "нет"], ["aac", "AAC"], ["copy", "copy"]]), camera, "audio")));
      grid.appendChild(field("Звук, кбит/с", bind(input("number", camera.audio_bitrate_kbps, { min: 32, max: 512 }), camera, "audio_bitrate_kbps")));
      grid.appendChild(field("Контейнер", bind(select(camera.container, [["mp4", "mp4"], ["mkv", "mkv"]]), camera, "container")));
      grid.appendChild(field("Фрагмент, с", bind(input("number", camera.segment_sec, { min: 5, max: 3600, required: true }), camera, "segment_sec")));
      card.appendChild(grid);
      root.appendChild(card);
      syncCustom(card, camera.resolution);
    });
    document.getElementById("camera-count").value = String(cameras.length);
    renderEstimate();
  }

  function resize(count) {
    readAll();
    while (cameras.length < count) cameras.push(blank(nextId(cameras), cameras.length));
    cameras = cameras.slice(0, count);
    render();
  }

  function payload() {
    readAll();
    return {
      cameras: cameras.map(function (camera) {
        return {
          id: camera.id,
          name: camera.name,
          enabled: camera.enabled,
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
      }),
    };
  }

  function applyStatus(next) {
    status = next || {};
    document.querySelectorAll("[data-status]").forEach(function (node) {
      node.textContent = statusText(node.getAttribute("data-status"));
    });
  }

  function load() {
    return fetch("/api/v1/cameras", { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("Не удалось загрузить камеры");
      return response.json();
    }).then(function (body) {
      cameras = (body.config && body.config.cameras) || [];
      applyStatus(body.status);
      render();
    });
  }

  document.getElementById("camera-count").addEventListener("change", function (event) {
    var count = Math.max(0, Math.min(16, Number(event.target.value) || 0));
    resize(count);
  });

  document.getElementById("cameras-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var message = document.getElementById("camera-message");
    message.textContent = "Сохранение…";
    fetch("/api/v1/cameras", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(payload()),
    }).then(function (response) {
      return response.json().then(function (body) { return { ok: response.ok, body: body }; });
    }).then(function (result) {
      if (!result.ok) {
        message.textContent = (result.body && result.body.message) || "Не удалось сохранить";
        return;
      }
      cameras = result.body.config.cameras;
      applyStatus(result.body.status);
      render();
      message.textContent = "Сохранено";
    }).catch(function () {
      message.textContent = "Не удалось сохранить";
    });
  });

  load().catch(function () {
    document.getElementById("camera-message").textContent = "Не удалось загрузить камеры";
  });
  window.setInterval(function () {
    fetch("/api/v1/cameras", { credentials: "same-origin" }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (body) {
      if (body) applyStatus(body.status);
    }).catch(function () { return null; });
  }, 3000);
})();
