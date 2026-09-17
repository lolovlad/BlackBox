(function () {
  function csrf() {
    return document.cookie.split('; ').find(function (x) { return x.trim().startsWith('bb_csrf='); })?.split('=')[1] || '';
  }

  function headers(extra) {
    return Object.assign({ 'X-CSRF-Token': decodeURIComponent(csrf()) }, extra || {});
  }

  function problemMessage(body, fallback) {
    if (!body || typeof body !== 'object') return fallback;
    if (typeof body.message === 'string' && body.message) return body.message;
    if (body.detail && typeof body.detail === 'object' && body.detail.message) return body.detail.message;
    if (typeof body.detail === 'string') return body.detail;
    return fallback;
  }

  function toast(message, kind) {
    window.dispatchEvent(new CustomEvent('bb-toast', { detail: { message: message, kind: kind || 'info' } }));
  }

  async function mutate(url, options) {
    const response = await fetch(url, Object.assign({ headers: headers() }, options || {}));
    if (!response.ok) {
      let detail = 'Ошибка операции';
      try {
        const body = await response.json();
        detail = problemMessage(body, detail);
      } catch (_e) {}
      throw new Error(detail);
    }
    const type = response.headers.get('content-type') || '';
    if (type.includes('application/json')) return response.json();
    return response;
  }

  window.bbShell = function bbShell() {
    return {
      toasts: [],
      pushToast: function (detail) {
        const item = { id: Date.now() + Math.random(), message: detail.message, kind: detail.kind || 'info' };
        this.toasts.push(item);
        setTimeout(function () {
          this.toasts = this.toasts.filter(function (t) { return t.id !== item.id; });
        }.bind(this), 4200);
      },
    };
  };

  function syncCreateMapOptions() {
    const protocol = document.getElementById('create-protocol');
    const select = document.getElementById('create-map-version');
    const hint = document.getElementById('create-map-hint');
    if (!protocol || !select) return;
    let visible = 0;
    Array.from(select.options).forEach(function (opt, index) {
      if (index === 0) {
        opt.hidden = false;
        return;
      }
      const match = opt.dataset.protocol === protocol.value;
      opt.hidden = !match;
      if (match) visible += 1;
    });
    if (select.selectedOptions[0] && select.selectedOptions[0].hidden) select.value = '';
    if (hint) hint.hidden = visible > 0;
  }

  function syncReaderOptions(protocol) {
    document.querySelectorAll('[data-serial-only]').forEach(function (el) {
      el.hidden = protocol !== 'modbus_rtu';
      const input = el.querySelector('input,select');
      if (input) input.disabled = protocol !== 'modbus_rtu';
    });
    document.querySelectorAll('[data-tcp-only]').forEach(function (el) {
      el.hidden = protocol !== 'modbus_tcp';
      const input = el.querySelector('input,select');
      if (input) input.disabled = protocol !== 'modbus_tcp';
    });
  }

  async function loadMapPreview(select, target, status) {
    if (!select || !target) return;
    const option = select.selectedOptions && select.selectedOptions[0];
    const version = select.value;
    const protocol = option && option.dataset ? option.dataset.protocol : (select.closest('form')?.dataset.vmProtocol || '');
    if (!version) {
      target.textContent = 'Выберите карту, чтобы открыть requests и fields.';
      if (status) status.textContent = ' Выберите карту';
      return;
    }
    if (status) status.textContent = ' Загрузка…';
    try {
      const response = await fetch('/api/v1/maps/' + encodeURIComponent(version) + (protocol ? '?protocol=' + encodeURIComponent(protocol) : ''), { headers: headers() });
      if (!response.ok) throw new Error('Карта не найдена');
      const body = await response.json();
      target.textContent = JSON.stringify(body.document || body, null, 2);
      if (status) status.textContent = ' ' + (body.checksum ? body.checksum.slice(0, 12) + '…' : 'готово');
    } catch (error) {
      target.textContent = error.message;
      if (status) status.textContent = ' ошибка';
    }
  }

  function numberOr(value, fallback) {
    if (value === null || value === undefined || value === '') return fallback;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function runtimeConfigFromForm(form) {
    const get = function (name) { return form.querySelector('[name="' + name + '"]'); };
    const protocol = form.querySelector('[name="protocol"]')?.value || form.dataset.vmProtocol || 'simulator';
    const reader = {
      enabled: get('enabled') ? get('enabled').checked : true,
      close_port_after_each_call: get('close_port_after_each_call') ? get('close_port_after_each_call').checked : true,
      clear_buffers_before_each_transaction: get('clear_buffers_before_each_transaction') ? get('clear_buffers_before_each_transaction').checked : true,
      poll_interval_sec: numberOr(get('poll_interval_sec')?.value, 0.12),
      timeout_sec: numberOr(get('timeout_sec')?.value, 0.35),
      retries: numberOr(get('retries')?.value, 3),
      retry_delay_sec: numberOr(get('retry_delay_sec')?.value, 0.2),
      address_offset: numberOr(get('address_offset')?.value, 1),
    };
    if (protocol === 'modbus_rtu') {
      Object.assign(reader, {
        port: get('port')?.value || '/dev/ttyAMA0',
        slave_id: numberOr(get('slave_id')?.value, 1),
        baudrate: numberOr(get('baudrate')?.value, 9600),
        bytesize: numberOr(get('bytesize')?.value, 8),
        parity: get('parity')?.value || 'N',
        stopbits: numberOr(get('stopbits')?.value, 1),
        mode: get('mode')?.value || 'rtu',
      });
    }
    if (protocol === 'modbus_tcp') {
      Object.assign(reader, {
        host: get('host')?.value || '127.0.0.1',
        tcp_port: numberOr(get('tcp_port')?.value, 502),
        unit_id: numberOr(get('unit_id')?.value, 1),
      });
    }
    return {
      reader: reader,
      storage: {
        target_resource_id: get('storage_resource_id')?.value || 'storage:data',
        flush_seconds: numberOr(get('flush_seconds')?.value, 5),
        min_free_bytes: numberOr(get('min_free_bytes')?.value, 67108864),
        quota_bytes: get('quota_bytes')?.value ? numberOr(get('quota_bytes').value, null) : null,
        telemetry_subdir: get('telemetry_subdir')?.value || 'telemetry',
      },
      buffer: {
        ram_rows: numberOr(get('ram_rows')?.value, 60),
        max_queue: numberOr(get('max_queue')?.value, 2048),
      },
    };
  }

  const createProtocol = document.getElementById('create-protocol');
  if (createProtocol) {
    createProtocol.addEventListener('change', syncCreateMapOptions);
    createProtocol.addEventListener('change', function () { syncReaderOptions(createProtocol.value); });
    syncCreateMapOptions();
    syncReaderOptions(createProtocol.value);
  }
  const createMapSelect = document.getElementById('create-map-version');
  if (createMapSelect) {
    createMapSelect.addEventListener('change', function () { loadMapPreview(createMapSelect, document.getElementById('create-map-preview'), document.getElementById('create-map-preview-status')); });
    const firstMap = Array.from(createMapSelect.options).find(function (opt) { return opt.value && !opt.hidden; });
    if (firstMap) {
      createMapSelect.value = firstMap.value;
      loadMapPreview(createMapSelect, document.getElementById('create-map-preview'), document.getElementById('create-map-preview-status'));
    }
  }
  const editMapSelect = document.getElementById('edit-map-version');
  if (editMapSelect) {
    editMapSelect.addEventListener('change', function () { loadMapPreview(editMapSelect, document.getElementById('edit-map-preview'), document.getElementById('edit-map-preview-status')); });
    loadMapPreview(editMapSelect, document.getElementById('edit-map-preview'), document.getElementById('edit-map-preview-status'));
    syncReaderOptions(document.getElementById('edit-vm')?.dataset.vmProtocol || '');
  }

  window.bbMapsPage = function bbMapsPage() {
    const labels = { simulator: 'Simulator', modbus_rtu: 'Modbus RTU', modbus_tcp: 'Modbus TCP' };

    function nextVersion(version) {
      const match = String(version || '').match(/^(.*?)(\d+)$/);
      if (!match) return (version || 'map') + '-v2';
      return match[1] + String(Number(match[2]) + 1);
    }

    function blankDocument(protocol) {
      if (protocol === 'simulator') {
        return {
          requests: [{ name: 'sim', fc: 3, address: 0, count: 1 }],
          fields: [{ name: 'value', type: 'uint16', source: 'sim', address: 0 }],
        };
      }
      return {
        requests: [{ name: 'holding', fc: 3, address: 0, count: 1 }],
        fields: [{ name: 'register_0', type: 'uint16', source: 'holding', address: 0 }],
      };
    }

    return {
      maps: [],
      query: '',
      filter: 'all',
      studioOpen: false,
      publishOpen: false,
      loading: false,
      saving: false,
      selectedKey: '',
      documentText: '',
      originalText: '',
      publishVersion: '',
      draftProtocol: 'modbus_tcp',
      draftPreset: '',
      get dirty() {
        return this.documentText !== this.originalText;
      },
      get currentMap() {
        const key = this.selectedKey;
        return this.maps.find(function (map) { return map.version + '::' + map.protocol === key; }) || null;
      },
      hydrate() {
        const node = document.getElementById('bb-maps-payload');
        try {
          this.maps = node ? JSON.parse(node.textContent || '[]') : [];
        } catch (_e) {
          this.maps = [];
        }
      },
      protocolLabel(protocol) {
        return labels[protocol] || protocol;
      },
      shortChecksum(value) {
        return value ? String(value).slice(0, 12) + '…' : '—';
      },
      formatStamp(value) {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value;
        return date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
      },
      isSelected(map) {
        return this.selectedKey === map.version + '::' + map.protocol;
      },
      get families() {
        const needle = this.query.trim().toLowerCase();
        const groups = {};
        const order = [];
        this.maps.forEach(function (map) {
          if (this.filter !== 'all' && map.protocol !== this.filter) return;
          const haystack = [map.version, map.protocol, map.preset_id || ''].join(' ').toLowerCase();
          if (needle && haystack.indexOf(needle) === -1) return;
          const key = map.protocol + '::' + (map.preset_id || '');
          if (!groups[key]) {
            groups[key] = { key: key, protocol: map.protocol, preset_id: map.preset_id, items: [] };
            order.push(key);
          }
          groups[key].items.push(map);
        }.bind(this));
        return order.map(function (key) {
          const family = groups[key];
          family.items.sort(function (a, b) { return String(b.created_at || '').localeCompare(String(a.created_at || '')); });
          return family;
        });
      },
      get familyVersions() {
        const current = this.currentMap;
        const protocol = current ? current.protocol : this.draftProtocol;
        const preset = current ? (current.preset_id || '') : (this.draftPreset || '');
        return this.maps.filter(function (map) {
          return map.protocol === protocol && (map.preset_id || '') === preset;
        }).sort(function (a, b) { return String(b.created_at || '').localeCompare(String(a.created_at || '')); });
      },
      onEscape() {
        if (this.publishOpen) {
          this.publishOpen = false;
          return;
        }
        if (this.studioOpen) this.closeStudio();
      },
      closeStudio() {
        if (this.dirty && !window.confirm('Есть несохранённые правки JSON. Закрыть окно?')) return;
        this.studioOpen = false;
      },
      createBlank() {
        this.selectedKey = '';
        this.draftProtocol = this.filter === 'all' ? 'modbus_tcp' : this.filter;
        this.draftPreset = '';
        this.publishVersion = 'custom-v1';
        this.documentText = JSON.stringify(blankDocument(this.draftProtocol), null, 2);
        this.originalText = this.documentText;
        this.studioOpen = true;
      },
      async openMap(map) {
        if (this.dirty && this.studioOpen && !this.isSelected(map) && !window.confirm('Есть несохранённые правки JSON. Открыть другую версию?')) {
          return;
        }
        this.selectedKey = map.version + '::' + map.protocol;
        this.draftProtocol = map.protocol;
        this.draftPreset = map.preset_id || '';
        this.publishVersion = nextVersion(map.version);
        this.studioOpen = true;
        this.loading = true;
        try {
          const url = '/api/v1/maps/' + encodeURIComponent(map.version) + '?protocol=' + encodeURIComponent(map.protocol);
          const response = await fetch(url, { headers: headers() });
          if (!response.ok) {
            let detail = 'Не удалось загрузить карту';
            try { detail = problemMessage(await response.json(), detail); } catch (_e) {}
            throw new Error(detail);
          }
          const body = await response.json();
          this.documentText = JSON.stringify(body.document || body, null, 2);
          this.originalText = this.documentText;
        } catch (error) {
          toast(error.message, 'error');
          this.documentText = '';
          this.originalText = '';
        } finally {
          this.loading = false;
        }
      },
      async copyJson() {
        try {
          await navigator.clipboard.writeText(this.documentText);
          toast('JSON скопирован', 'ok');
        } catch (_e) {
          toast('Не удалось скопировать JSON', 'error');
        }
      },
      downloadJson() {
        const name = (this.currentMap ? this.currentMap.version : this.publishVersion || 'map') + '.json';
        const blob = new Blob([this.documentText], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = name;
        link.click();
        URL.revokeObjectURL(url);
      },
      async publishFromEditor() {
        let parsed;
        try {
          parsed = JSON.parse(this.documentText);
        } catch (_e) {
          toast('JSON некорректен. Исправьте документ перед публикацией.', 'error');
          return;
        }
        if (!this.publishVersion.trim()) {
          toast('Укажите имя новой версии.', 'error');
          return;
        }
        if (this.currentMap && this.publishVersion.trim() === this.currentMap.version && this.dirty) {
          toast('Версия ' + this.currentMap.version + ' неизменяема. Укажите новое имя версии.', 'error');
          return;
        }
        this.saving = true;
        try {
          const created = await mutate('/api/v1/maps', {
            method: 'POST',
            headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({
              protocol: this.draftProtocol,
              version: this.publishVersion.trim(),
              preset_id: this.draftPreset.trim() || null,
              document: parsed,
            }),
          });
          const record = {
            id: created.map_id || created.id,
            version: created.version,
            protocol: created.protocol,
            preset_id: created.preset_id,
            checksum: created.checksum,
            created_at: new Date().toISOString(),
          };
          this.maps = [record].concat(this.maps.filter(function (map) {
            return !(map.version === record.version && map.protocol === record.protocol);
          }));
          this.selectedKey = record.version + '::' + record.protocol;
          this.originalText = this.documentText;
          this.publishVersion = nextVersion(record.version);
          toast('Опубликована версия ' + record.version, 'ok');
        } catch (error) {
          toast(error.message, 'error');
        } finally {
          this.saving = false;
        }
      },
    };
  };

  document.querySelectorAll('[data-vm-action]').forEach(function (button) {
    button.addEventListener('click', async function () {
      const action = button.dataset.vmAction;
      if ((action === 'stop' || action === 'restart') && !window.confirm(action === 'stop' ? 'Остановить виртуальную машину?' : 'Перезапустить виртуальную машину?')) {
        return;
      }
      button.disabled = true;
      try {
        await mutate('/api/v1/vms/' + button.dataset.vmId + '/' + action, { method: 'POST' });
        toast(action === 'apply-map' ? 'Команда apply-map отправлена' : 'Операция выполнена', 'ok');
        location.reload();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    });
  });

  const create = document.getElementById('create-vm');
  if (create) {
    create.addEventListener('submit', async function (event) {
      event.preventDefault();
      const form = new FormData(create);
      const resources = Array.from(create.querySelectorAll('input[name="read_resource_id"]:checked')).map(function (el) {
        return { resource_id: el.value };
      });
      const payload = {
        name: form.get('name'),
        protocol: form.get('protocol'),
        preset_id: form.get('preset_id') || null,
        map_version: form.get('map_version'),
        read_resources: resources,
        storage_resource_id: form.get('storage_resource_id') || 'storage:data',
        config: runtimeConfigFromForm(create),
      };
      if (!payload.map_version) {
        toast('Выберите опубликованную карту или создайте её на странице «Карты».', 'error');
        return;
      }
      try {
        await mutate('/api/v1/vms', {
          method: 'POST',
          headers: headers({ 'Content-Type': 'application/json' }),
          body: JSON.stringify(payload),
        });
        toast('ВМ создана. Запустите её кнопкой «Старт».', 'ok');
        location.reload();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  const upload = document.getElementById('map-upload');
  if (upload) {
    upload.addEventListener('submit', async function (event) {
      event.preventDefault();
      try {
        await mutate('/api/v1/maps/upload', { method: 'POST', headers: headers(), body: new FormData(upload) });
        toast('Карта опубликована', 'ok');
        location.reload();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  const edit = document.getElementById('edit-vm');
  if (edit) {
    edit.addEventListener('submit', async function (event) {
      event.preventDefault();
      const form = new FormData(edit);
      try {
        await mutate('/api/v1/vms/' + edit.dataset.vmId, {
          method: 'PATCH',
          headers: headers({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            name: form.get('name'),
            description: form.get('description'),
            map_version: form.get('map_version'),
            read_resources: Array.from(edit.querySelectorAll('input[name="read_resource_id"]:checked')).map(function (el) { return { resource_id: el.value }; }),
            storage_resource_id: form.get('storage_resource_id') || 'storage:data',
            config: runtimeConfigFromForm(edit),
          }),
        });
        toast('ВМ сохранена', 'ok');
        location.href = '/admin/vms';
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  const applyMap = document.querySelector('[data-apply-map]');
  if (applyMap) {
    applyMap.addEventListener('click', async function () {
      applyMap.disabled = true;
      try {
        const form = document.getElementById('edit-vm');
        if (form) {
          const data = new FormData(form);
          await mutate('/api/v1/vms/' + applyMap.dataset.applyMap, {
            method: 'PATCH',
            headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ map_version: data.get('map_version') }),
          });
        }
        await mutate('/api/v1/vms/' + applyMap.dataset.applyMap + '/apply-map', { method: 'POST' });
        toast('Карта привязана и apply-map отправлен', 'ok');
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        applyMap.disabled = false;
      }
    });
  }

  const scan = document.querySelector('[data-resource-scan]');
  if (scan) {
    scan.addEventListener('click', async function () {
      scan.disabled = true;
      const message = document.getElementById('resource-message');
      try {
        await mutate('/api/v1/resources/scan', { method: 'POST' });
        if (message) message.textContent = 'Сканирование завершено';
        toast('Ресурсы обновлены', 'ok');
        location.reload();
      } catch (error) {
        if (message) message.textContent = error.message;
        toast(error.message, 'error');
      } finally {
        scan.disabled = false;
      }
    });
  }

  document.querySelectorAll('[data-resource-approve]').forEach(function (button) {
    button.addEventListener('click', async function () {
      button.disabled = true;
      try {
        await mutate('/api/v1/resources/' + encodeURIComponent(button.dataset.resourceApprove) + '/approve', { method: 'POST' });
        toast('Ресурс подтверждён', 'ok');
        location.reload();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    });
  });

  const logout = document.querySelector('[data-logout]');
  if (logout) {
    logout.addEventListener('click', async function () {
      try {
        await mutate('/api/v1/auth/logout', { method: 'POST' });
      } catch (_e) {}
      location.href = '/login';
    });
  }

  const live = document.getElementById('live');
  const logTargets = {};
  document.querySelectorAll('[data-live-logs]').forEach(function (target) {
    logTargets[target.dataset.liveLogs] = target;
  });
  if ((!live && Object.keys(logTargets).length === 0) || !window.WebSocket) return;

  // One socket is shared by dashboard, VM details and the admin log tail.
  // The server sends a snapshot first, then deltas; a short reconnect loop
  // keeps the operator view useful during Hub restarts without polling.
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socketUrl = protocol + '://' + location.host + '/ws/v1/events';
  let socket = null;
  let reconnectTimer = null;
  let closing = false;

  function logLines(target, lines, replace) {
    if (!target) return;
    const limit = Number(target.dataset.logLimit || 200);
    const current = replace ? [] : target.textContent.split('\n').filter(function (line) {
      return line.trim() !== '' && line !== 'Логов пока нет.';
    });
    const next = current.concat((lines || []).map(function (item) {
      return typeof item === 'string' ? item : (item && item.line) || '';
    }).filter(Boolean)).slice(-Math.max(1, limit));
    target.textContent = next.length ? next.join('\n') : 'Логов пока нет.';
    target.scrollTop = target.scrollHeight;
  }

  function handleMessage(message) {
    if (!message || !message.type) return;
    const payload = message.payload || {};
    if (message.type === 'snapshot') {
      if (live) live.dataset.lastEvent = payload.seq || '';
      Object.keys(logTargets).forEach(function (vmId) {
        const snapshotLines = payload.logs && payload.logs[vmId] ? payload.logs[vmId] : [];
        // Replace when the Hub has a history for this VM, otherwise retain
        // the server-rendered Docker tail until the first live log arrives.
        logLines(logTargets[vmId], snapshotLines, snapshotLines.length > 0);
      });
      return;
    }
    if (message.type !== 'delta') return;
    if (live) live.dataset.lastEvent = message.seq || '';
    const status = document.getElementById('live-status');
    if (status && message.topic === 'vm_status') {
      status.textContent = 'Событие #' + message.seq + ': ' + (payload.lifecycle || '');
    }
    const tags = document.getElementById('live-tags');
    if (tags && message.topic === 'tags') {
      tags.textContent = JSON.stringify(payload.tags || payload, null, 2);
    }
    if (message.topic === 'logs') {
      logLines(logTargets[String(payload.vm_id || '')], [payload]);
    }
  }

  function connectEvents() {
    if (closing) return;
    socket = new WebSocket(socketUrl);
    socket.onmessage = function (event) {
      try { handleMessage(JSON.parse(event.data)); } catch (_e) { /* ignore malformed event */ }
    };
    socket.onclose = function () {
      socket = null;
      if (!closing && reconnectTimer === null) {
        reconnectTimer = setTimeout(function () {
          reconnectTimer = null;
          connectEvents();
        }, 1500);
      }
    };
    socket.onerror = function () {
      // onclose schedules the reconnect; closing here avoids a noisy browser
      // error and prevents a half-open socket from accumulating.
      try { socket.close(); } catch (_e) {}
    };
  }
  connectEvents();
  window.addEventListener('beforeunload', function () {
    closing = true;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    if (socket) socket.close();
  });
})();
