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

  const createProtocol = document.getElementById('create-protocol');
  if (createProtocol) {
    createProtocol.addEventListener('change', syncCreateMapOptions);
    syncCreateMapOptions();
  }

  window.bbMapsPage = function bbMapsPage() {
    return {
      selected: '',
      documentText: '',
      loading: false,
      async loadSelected() {
        if (!this.selected) {
          this.documentText = '';
          return;
        }
        const parts = this.selected.split('::');
        const version = parts[0];
        const protocol = parts[1] || '';
        this.loading = true;
        try {
          const url = '/api/v1/maps/' + encodeURIComponent(version) + (protocol ? ('?protocol=' + encodeURIComponent(protocol)) : '');
          const response = await fetch(url, { headers: headers() });
          if (!response.ok) {
            let detail = 'Не удалось загрузить карту';
            try { detail = problemMessage(await response.json(), detail); } catch (_e) {}
            throw new Error(detail);
          }
          const body = await response.json();
          this.documentText = JSON.stringify(body.document || body, null, 2);
        } catch (error) {
          toast(error.message, 'error');
          this.documentText = '';
        } finally {
          this.loading = false;
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
      const resources = Array.from(create.querySelectorAll('input[name="resource_id"]:checked')).map(function (el) {
        return { resource_id: el.value };
      });
      const payload = {
        name: form.get('name'),
        protocol: form.get('protocol'),
        preset_id: form.get('preset_id') || null,
        map_version: form.get('map_version'),
        resources: resources,
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
  if (!live || !window.WebSocket) return;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(protocol + '://' + location.host + '/ws/v1/events');
  socket.onmessage = function (event) {
    const message = JSON.parse(event.data);
    live.dataset.lastEvent = message.seq || '';
    const status = document.getElementById('live-status');
    if (status && message.topic === 'vm_status') {
      status.textContent = 'Событие #' + message.seq + ': ' + (message.payload.lifecycle || '');
    }
    const tags = document.getElementById('live-tags');
    if (tags && message.topic === 'tags') {
      tags.textContent = JSON.stringify(message.payload.tags || message.payload, null, 2);
    }
  };
})();
