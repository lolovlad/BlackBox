(function () {
  var cameras = [];
  var disks = [];
  var episodes = [];
  var status = {};
  var selected = "";
  var watching = false;
  var lastLease = 0;

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
    syncPicture();
    renderEstimate();
    renderEpisodes();
    renderLive();
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
    var copy = camera && (field("codec").value === "copy" || field("audio").value === "copy");
    document.getElementById("camera-copy-note").hidden = !copy;
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

  function statusText(id) {
    var row = status[id];
    if (!row || row.state === "stopped") return "готова";
    if (row.state === "recording") return "идёт запись";
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
      button.className = "bb-camera-pick" + (camera.id === selected ? " is-selected" : "");
      var name = document.createElement("strong");
      name.textContent = camera.name || camera.id;
      var meta = document.createElement("span");
      meta.textContent = camera.id + " · " + statusText(camera.id);
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
    if (row.state === "finished") return "Эпизод закончен";
    if (row.state === "error") return "Ошибка эпизода";
    if (row.state === "recording") return "Идёт запись";
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
      var title = document.createElement("strong");
      title.textContent = episodeTitle(row);
      item.appendChild(title);
      var meta = document.createElement("span");
      meta.textContent = [when(row.ended_at || row.started_at), row.path, row.message].filter(Boolean).join(" · ");
      item.appendChild(meta);
      list.appendChild(item);
    });
  }

  function renderLive() {
    var camera = current();
    var state = camera ? statusText(camera.id) : "";
    var node = document.getElementById("camera-live-status");
    var watch = document.getElementById("camera-watch");
    var record = document.getElementById("camera-record");
    if (!camera) return;
    var row = status[camera.id];
    if (row && row.state === "error" && row.message) state = row.message;
    node.textContent = watching ? ("Просмотр · " + state) : state;
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
  }

  function payload() {
    readForm();
    return {
      storage_resource_id: field("storage_resource_id").value || "storage:data",
      video_subdir: (field("video_subdir").value || "video").trim() || "video",
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
        };
      }),
    };
  }

  function errorText(body) {
    var details = body && body.details;
    if (Array.isArray(details) && details.length && details[0].msg) return String(details[0].msg);
    return (body && body.message) || "Не удалось сохранить";
  }

  function apply(body, replace) {
    status = body.status || {};
    episodes = body.episodes || [];
    if (replace) {
      cameras = (body.config && body.config.cameras) || [];
      fillDisks(body.storage_resources, (body.config && body.config.storage_resource_id) || "storage:data");
      field("video_subdir").value = (body.config && body.config.video_subdir) || "video";
      if (!cameras.some(function (camera) { return camera.id === selected; })) {
        selected = cameras.length ? cameras[0].id : "";
      }
      writeForm();
    } else {
      fillDisks(body.storage_resources, field("storage_resource_id").value);
      renderEpisodes();
      renderLive();
    }
    renderNav();
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
    renderEstimate();
  });
  document.getElementById("cameras-form").addEventListener("change", function () {
    renderEstimate();
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
    }).catch(function () { return null; });
  }, 3000);

  window.setInterval(function () {
    if (!watching || !selected) return;
    refreshPreviewImage();
    if (Date.now() - lastLease > 2000) {
      lastLease = Date.now();
      sendPreview(true);
    }
  }, 1000);
})();
