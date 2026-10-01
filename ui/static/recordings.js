(function () {
  const root = document.getElementById('bb-explorer');
  const rows = document.getElementById('bb-explorer-rows');
  const treeBox = document.getElementById('bb-explorer-tree');
  const crumbsBox = document.getElementById('bb-explorer-crumbs');
  const statusBox = document.getElementById('bb-explorer-status');
  const backButton = document.getElementById('bb-explorer-back');
  const upButton = document.getElementById('bb-explorer-up');
  const refreshButton = document.getElementById('bb-explorer-refresh');
  const downloadButton = document.getElementById('bb-explorer-download');
  const fileDialog = document.getElementById('bb-explorer-file');
  const fileTitle = document.getElementById('bb-file-title');
  const fileName = document.getElementById('bb-file-name');
  const fileFacts = document.getElementById('bb-file-facts');
  const fileDownload = document.getElementById('bb-file-download');
  if (!root || !rows || !treeBox || !crumbsBox || !statusBox) return;

  let history = [''];
  let cursor = 0;
  let current = '';
  let place = 'root';
  let crumbItems = [{ label: 'Записи', path: '' }];
  let entries = [];
  let selected = '';
  let sortKey = 'name';
  let sortDir = 'asc';
  const folders = new Map();
  const expanded = new Set(['']);

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function when(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU');
  }

  function formatSize(bytes) {
    if (bytes == null) return '';
    const units = ['Б', 'КБ', 'МБ', 'ГБ'];
    let value = Number(bytes);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    const text = index === 0 ? String(bytes) : value.toFixed(value >= 10 ? 0 : 1);
    return text + ' ' + units[index];
  }

  function typeName(entry) {
    if (entry.kind === 'dir') return 'Папка';
    if (entry.ext === 'mkv') return 'Видео Matroska';
    if (entry.ext === 'mp4') return 'Видео MP4';
    return 'Видео';
  }

  function countLabel(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return count + ' элемент';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return count + ' элемента';
    return count + ' элементов';
  }

  function placeLabel() {
    if (place === 'motion') return 'Запись по движению';
    if (place === 'incident') return 'Запись по аварии';
    if (place === 'manual') return 'Ручная запись';
    return '';
  }

  function sorted(items) {
    const factor = sortDir === 'asc' ? 1 : -1;
    return items.slice().sort(function (left, right) {
      if (left.kind !== right.kind) return left.kind === 'dir' ? -1 : 1;
      if (sortKey === 'size') return factor * ((left.size || 0) - (right.size || 0));
      if (sortKey === 'modified') return factor * ((Date.parse(left.modified) || 0) - (Date.parse(right.modified) || 0));
      if (sortKey === 'type') return factor * typeName(left).localeCompare(typeName(right), 'ru');
      return factor * String(left.label || left.name).localeCompare(String(right.label || right.name), 'ru');
    });
  }

  function selectedEntry() {
    return entries.find(function (entry) { return entry.path === selected; }) || null;
  }

  async function fetchList(path) {
    const response = await fetch('/api/v1/video/files?path=' + encodeURIComponent(path), { credentials: 'same-origin' });
    if (!response.ok) throw new Error('list');
    return response.json();
  }

  function rememberFolders(path, items) {
    folders.set(path, (items || []).filter(function (entry) { return entry.kind === 'dir'; }));
  }

  async function ensureAncestors(path) {
    const parts = path ? path.split('/') : [];
    let acc = '';
    for (let index = 0; index < parts.length; index += 1) {
      if (!folders.has(acc)) {
        const data = await fetchList(acc);
        rememberFolders(acc, data.entries || []);
      }
      expanded.add(acc);
      acc = acc ? acc + '/' + parts[index] : parts[index];
    }
    expanded.add(path || '');
  }

  function renderTreeLevel(items, depth) {
    return (items || []).map(function (entry) {
      const open = expanded.has(entry.path);
      const children = open ? renderTreeLevel(folders.get(entry.path) || [], depth + 1) : '';
      const twist = '<button type="button" class="bb-explorer-twist" data-twist="' + esc(entry.path) + '" aria-label="' + (open ? 'Свернуть' : 'Развернуть') + '"><i class="bi ' + (open ? 'bi-chevron-down' : 'bi-chevron-right') + '"></i></button>';
      const label = '<button type="button" class="bb-explorer-node-label' + (entry.path === current ? ' is-current' : '') + '" data-open="' + esc(entry.path) + '"><i class="bi ' + (open && entry.path === current ? 'bi-folder2-open' : 'bi-folder-fill') + '"></i><span>' + esc(entry.label || entry.name) + '</span></button>';
      return '<div class="bb-explorer-node" style="--depth:' + depth + '">' + twist + label + '</div>' + children;
    }).join('');
  }

  function renderTree() {
    const top = folders.get('') || [];
    const rootCurrent = current === '' ? ' is-current' : '';
    treeBox.innerHTML = '<div class="bb-explorer-node" style="--depth:0"><button type="button" class="bb-explorer-twist" data-twist="" aria-label="' + (expanded.has('') ? 'Свернуть' : 'Развернуть') + '"><i class="bi ' + (expanded.has('') ? 'bi-chevron-down' : 'bi-chevron-right') + '"></i></button><button type="button" class="bb-explorer-node-label' + rootCurrent + '" data-open=""><i class="bi bi-cloud"></i><span>Все записи</span></button></div>' + (expanded.has('') ? renderTreeLevel(top, 1) : '');
  }

  function renderCrumbs() {
    crumbsBox.innerHTML = crumbItems.map(function (item, index) {
      const last = index === crumbItems.length - 1;
      const button = '<button type="button" data-open="' + esc(item.path) + '"' + (last ? ' aria-current="page"' : '') + '>' + esc(item.label) + '</button>';
      return index === 0 ? button : '<i class="bi bi-chevron-right" aria-hidden="true"></i>' + button;
    }).join('');
  }

  function renderRows() {
    const items = sorted(entries);
    rows.innerHTML = items.length ? items.map(function (entry) {
      const icon = entry.kind === 'dir' ? 'bi-folder-fill' : 'bi-play-btn-fill';
      const raw = entry.label && entry.label !== entry.name ? '<span class="bb-explorer-raw">' + esc(entry.name) + '</span>' : '';
      return '<tr class="bb-explorer-row' + (entry.path === selected ? ' is-selected' : '') + '" data-path="' + esc(entry.path) + '" data-kind="' + esc(entry.kind) + '"><td><span class="bb-explorer-name"><span class="bb-explorer-glyph is-' + esc(entry.kind) + '"><i class="bi ' + icon + '"></i></span><span class="bb-explorer-label"><span>' + esc(entry.label || entry.name) + '</span>' + raw + '</span></span></td><td>' + esc(when(entry.modified)) + '</td><td>' + esc(typeName(entry)) + '</td><td class="bb-explorer-size">' + esc(entry.kind === 'dir' ? '—' : formatSize(entry.size)) + '</td></tr>';
    }).join('') : '<tr class="bb-explorer-empty"><td colspan="4"><div class="bb-explorer-blank"><i class="bi bi-folder2-open"></i><p>Папка пуста</p></div></td></tr>';
    root.querySelectorAll('[data-sort]').forEach(function (button) {
      const key = button.getAttribute('data-sort');
      button.classList.toggle('is-sorted', key === sortKey);
      button.setAttribute('aria-sort', key === sortKey ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }

  function renderStatus() {
    const where = placeLabel();
    const chosen = selectedEntry();
    const tail = chosen ? ' · ' + (chosen.label || chosen.name) : '';
    statusBox.textContent = (where ? where + ' · ' : '') + countLabel(entries.length) + tail;
  }

  function renderChrome() {
    backButton.disabled = cursor <= 0;
    upButton.disabled = !current;
    const chosen = selectedEntry();
    downloadButton.disabled = !chosen || chosen.kind !== 'file';
  }

  function render() {
    renderTree();
    renderCrumbs();
    renderRows();
    renderStatus();
    renderChrome();
  }

  async function open(path, record) {
    rows.innerHTML = '<tr class="bb-explorer-empty"><td colspan="4"><div class="bb-explorer-blank"><i class="bi bi-arrow-repeat"></i><p>Загрузка…</p></div></td></tr>';
    try {
      const data = await fetchList(path);
      current = data.path || '';
      place = data.place || 'root';
      crumbItems = data.crumbs && data.crumbs.length ? data.crumbs : [{ label: 'Записи', path: '' }];
      entries = data.entries || [];
      selected = '';
      hideFile();
      rememberFolders(current, entries);
      if (record && history[cursor] !== current) {
        history = history.slice(0, cursor + 1);
        history.push(current);
        cursor = history.length - 1;
      }
      await ensureAncestors(current);
      render();
    } catch (_error) {
      rows.innerHTML = '<tr class="bb-explorer-empty"><td colspan="4"><div class="bb-explorer-blank"><i class="bi bi-exclamation-circle"></i><p class="bb-error-inline">Не удалось открыть папку.</p></div></td></tr>';
      statusBox.textContent = 'Папка недоступна';
    }
  }

  function hideFile() {
    if (fileDialog) fileDialog.hidden = true;
  }

  function showFile(entry) {
    if (!fileDialog || !entry || entry.kind !== 'file') return;
    selected = entry.path;
    fileTitle.textContent = entry.label || entry.name;
    const hasAlias = entry.label && entry.label !== entry.name;
    fileName.hidden = !hasAlias;
    fileName.textContent = hasAlias ? entry.name : '';
    const facts = [
      ['Тип', typeName(entry)],
      ['Размер', formatSize(entry.size) || '—'],
      ['Изменён', when(entry.modified) || '—'],
      ['Путь', entry.path],
    ];
    fileFacts.innerHTML = facts.map(function (fact) {
      return '<div><dt>' + esc(fact[0]) + '</dt><dd>' + esc(fact[1]) + '</dd></div>';
    }).join('');
    fileDownload.href = '/api/v1/video/files/download?path=' + encodeURIComponent(entry.path);
    fileDownload.setAttribute('download', entry.name || '');
    fileDialog.hidden = false;
    renderRows();
    renderStatus();
    renderChrome();
  }

  function activate(entry) {
    if (!entry) return;
    if (entry.kind === 'dir') {
      hideFile();
      open(entry.path, true);
      return;
    }
    showFile(entry);
  }

  backButton.addEventListener('click', function () {
    if (cursor <= 0) return;
    cursor -= 1;
    open(history[cursor], false);
  });
  upButton.addEventListener('click', function () {
    if (!current) return;
    const parent = current.includes('/') ? current.slice(0, current.lastIndexOf('/')) : '';
    open(parent, true);
  });
  refreshButton.addEventListener('click', function () {
    folders.delete(current);
    if (current) {
      const parent = current.includes('/') ? current.slice(0, current.lastIndexOf('/')) : '';
      folders.delete(parent);
    }
    open(current, false);
  });
  downloadButton.addEventListener('click', function () {
    const chosen = selectedEntry();
    if (chosen && chosen.kind === 'file') showFile(chosen);
  });
  fileDialog?.addEventListener('click', function (event) {
    if (event.target === fileDialog || event.target.closest('[data-file-close]')) hideFile();
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && fileDialog && !fileDialog.hidden) hideFile();
  });
  root.addEventListener('click', function (event) {
    const sort = event.target.closest('[data-sort]');
    if (sort) {
      const key = sort.getAttribute('data-sort');
      if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      else {
        sortKey = key;
        sortDir = key === 'modified' || key === 'size' ? 'desc' : 'asc';
      }
      renderRows();
      return;
    }
    const twist = event.target.closest('[data-twist]');
    if (twist) {
      const path = twist.getAttribute('data-twist') || '';
      if (expanded.has(path)) {
        expanded.delete(path);
        renderTree();
        return;
      }
      expanded.add(path);
      if (folders.has(path)) {
        renderTree();
        return;
      }
      fetchList(path).then(function (data) {
        rememberFolders(path, data.entries || []);
        renderTree();
      }).catch(function () {
        expanded.delete(path);
        renderTree();
      });
      return;
    }
    const opener = event.target.closest('[data-open]');
    if (opener && !opener.closest('tr')) {
      open(opener.getAttribute('data-open') || '', true);
    }
  });
  rows.addEventListener('click', function (event) {
    const row = event.target.closest('tr[data-path]');
    if (!row) return;
    activate(entries.find(function (entry) { return entry.path === row.getAttribute('data-path'); }));
  });

  open('', false);
})();
