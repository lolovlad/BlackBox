(function () {
  function csrf() {
    return decodeURIComponent(document.cookie.split('; ').find(function (item) { return item.startsWith('bb_csrf='); })?.split('=')[1] || '');
  }
  function message(id, text, error) {
    var target = document.getElementById(id);
    if (!target) return;
    target.textContent = text;
    target.classList.toggle('is-error', !!error);
  }
  async function send(url, method, body) {
    var response = await fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    var result = await response.json();
    if (!response.ok) throw new Error((result.detail && result.detail.message) || result.message || 'Операция не выполнена');
    return result;
  }
  function setPanel(panel, toggle, open) {
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) panel.querySelector('input')?.focus();
  }

  var userForm = document.getElementById('bb-user-create');
  var userList = document.getElementById('bb-user-list');
  if (userForm && userList) {
    var userRows = [];
    var currentUser = Number(document.querySelector('[data-current-user]')?.dataset.currentUser || 0);
    var createPanel = document.getElementById('bb-user-create-panel');
    var createToggle = document.getElementById('bb-user-add-toggle');
    var passwordDialog = document.getElementById('bb-user-password-dialog');
    var passwordForm = document.getElementById('bb-user-password-form');
    var passwordTarget = 0;

    async function loadUsers() {
      userList.innerHTML = '<tr><td colspan="5" class="bb-admin-empty">Загрузка учетных записей…</td></tr>';
      var response = await fetch('/api/v1/users');
      if (!response.ok) throw new Error('Не удалось загрузить список пользователей');
      var result = await response.json();
      userRows = result.items || [];
      var count = document.getElementById('bb-user-count');
      if (count) count.textContent = String(userRows.length);
      renderUsers();
    }

    function renderUsers() {
      var query = (document.getElementById('bb-user-search')?.value || '').trim().toLocaleLowerCase();
      var role = document.getElementById('bb-user-role-filter')?.value || '';
      var status = document.getElementById('bb-user-status-filter')?.value || '';
      var rows = userRows.filter(function (item) {
        return (!query || item.username.toLocaleLowerCase().includes(query)) &&
          (!role || item.role === role) &&
          (!status || (item.disabled ? 'disabled' : 'active') === status);
      });
      userList.replaceChildren();
      rows.forEach(function (item) {
        var row = document.createElement('tr');
        row.className = item.disabled ? 'is-disabled' : '';
        row.dataset.username = item.username;

        var identity = row.insertCell();
        identity.className = 'bb-user-identity';
        var initials = document.createElement('span');
        initials.className = 'bb-user-avatar ' + (item.role === 'admin' ? 'is-admin' : 'is-operator');
        initials.textContent = (item.username.trim()[0] || '?').toLocaleUpperCase();
        initials.setAttribute('aria-hidden', 'true');
        var names = document.createElement('span');
        names.className = 'bb-user-names';
        var username = document.createElement('strong');
        username.textContent = item.username;
        var detail = document.createElement('small');
        detail.textContent = item.id === currentUser ? 'Ваша учетная запись' : 'ID ' + item.id;
        names.append(username, detail);
        identity.append(initials, names);

        var roleCell = row.insertCell();
        var roleSelect = document.createElement('select');
        roleSelect.className = 'bb-user-role-select';
        roleSelect.setAttribute('aria-label', 'Роль пользователя ' + item.username);
        [['operator', 'Оператор'], ['admin', 'Администратор']].forEach(function (optionData) {
          var option = document.createElement('option');
          option.value = optionData[0];
          option.textContent = optionData[1];
          roleSelect.append(option);
        });
        roleSelect.value = item.role;
        roleSelect.disabled = item.id === currentUser || !!item.disabled;
        roleSelect.addEventListener('change', async function () {
          roleSelect.disabled = true;
          try {
            await send('/api/v1/users/' + item.id, 'PATCH', { role: roleSelect.value });
            message('bb-user-message', 'Роль пользователя «' + item.username + '» обновлена.');
            await loadUsers();
          } catch (error) {
            message('bb-user-message', error.message, true);
            roleSelect.value = item.role;
            roleSelect.disabled = item.id === currentUser || !!item.disabled;
          }
        });
        roleCell.append(roleSelect);

        var statusCell = row.insertCell();
        var statusBadge = document.createElement('span');
        statusBadge.className = 'bb-user-status ' + (item.disabled ? 'is-disabled' : 'is-active');
        statusBadge.textContent = item.disabled ? 'Отключен' : 'Активен';
        statusCell.append(statusBadge);

        var created = row.insertCell();
        created.className = 'bb-user-created';
        var date = item.created_at ? new Date(item.created_at) : null;
        created.textContent = date && !Number.isNaN(date.valueOf()) ? new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium' }).format(date) : '—';

        var actions = row.insertCell();
        actions.className = 'bb-user-actions';
        var reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'bb-icon-btn';
        reset.dataset.userReset = String(item.id);
        reset.setAttribute('aria-label', 'Сменить пароль для ' + item.username);
        reset.title = 'Сменить пароль';
        reset.innerHTML = '<i class="bi bi-key" aria-hidden="true"></i>';
        reset.disabled = !!item.disabled;
        actions.append(reset);
        if (item.id !== currentUser) {
          var toggle = document.createElement('button');
          toggle.type = 'button';
          toggle.className = 'bb-icon-btn ' + (item.disabled ? 'is-restore' : 'is-danger');
          toggle.dataset.userToggle = String(item.id);
          toggle.dataset.nextDisabled = item.disabled ? 'false' : 'true';
          toggle.setAttribute('aria-label', item.disabled ? 'Включить ' + item.username : 'Отключить ' + item.username);
          toggle.title = item.disabled ? 'Включить учетную запись' : 'Отключить учетную запись';
          toggle.innerHTML = item.disabled ? '<i class="bi bi-person-check" aria-hidden="true"></i>' : '<i class="bi bi-person-slash" aria-hidden="true"></i>';
          actions.append(toggle);
        }
        userList.append(row);
      });
      if (!rows.length) {
        var empty = userList.insertRow();
        empty.innerHTML = '<td colspan="5" class="bb-admin-empty">Пользователи не найдены.</td>';
      }
    }

    document.getElementById('bb-user-search')?.addEventListener('input', renderUsers);
    document.getElementById('bb-user-role-filter')?.addEventListener('change', renderUsers);
    document.getElementById('bb-user-status-filter')?.addEventListener('change', renderUsers);
    createToggle.addEventListener('click', function () { setPanel(createPanel, createToggle, createPanel.hidden); });
    ['bb-user-create-close', 'bb-user-create-cancel'].forEach(function (id) {
      document.getElementById(id).addEventListener('click', function () { setPanel(createPanel, createToggle, false); });
    });
    userForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      var button = userForm.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        var result = await send('/api/v1/users', 'POST', {
          username: userForm.elements.username.value.trim(),
          password: userForm.elements.password.value,
          role: userForm.elements.role.value
        });
        userForm.reset();
        setPanel(createPanel, createToggle, false);
        message('bb-user-message', 'Пользователь «' + result.username + '» создан.');
        await loadUsers();
      } catch (error) { message('bb-user-message', error.message, true); }
      finally { button.disabled = false; }
    });

    userList.addEventListener('click', async function (event) {
      var reset = event.target.closest('[data-user-reset]');
      if (reset) {
        passwordTarget = Number(reset.dataset.userReset);
        var record = userRows.find(function (item) { return Number(item.id) === passwordTarget; });
        document.getElementById('bb-user-password-label').textContent = record ? 'Новый пароль для ' + record.username : '';
        passwordForm.reset();
        passwordDialog.showModal();
        passwordForm.elements.password.focus();
        return;
      }
      var toggle = event.target.closest('[data-user-toggle]');
      if (!toggle) return;
      var target = userRows.find(function (item) { return Number(item.id) === Number(toggle.dataset.userToggle); });
      if (!target) return;
      var disabled = toggle.dataset.nextDisabled === 'true';
      if (!window.confirm((disabled ? 'Отключить' : 'Включить') + ' учетную запись «' + target.username + '»?')) return;
      toggle.disabled = true;
      try {
        await send('/api/v1/users/' + target.id, 'PATCH', { disabled: disabled });
        message('bb-user-message', disabled ? 'Учетная запись отключена.' : 'Учетная запись включена.');
        await loadUsers();
      } catch (error) { message('bb-user-message', error.message, true); toggle.disabled = false; }
    });

    passwordForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      var button = passwordForm.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await send('/api/v1/users/' + passwordTarget, 'PATCH', { password: passwordForm.elements.password.value });
        passwordDialog.close();
        message('bb-user-message', 'Пароль обновлен. Активные сеансы пользователя завершены.');
      } catch (error) { message('bb-user-message', error.message, true); }
      finally { button.disabled = false; }
    });
    document.getElementById('bb-user-password-cancel').addEventListener('click', function () { passwordDialog.close(); });
    loadUsers().catch(function (error) { userList.innerHTML = '<tr><td colspan="5" class="bb-admin-empty is-error">' + error.message + '</td></tr>'; });
  }

  var networkForm = document.getElementById('bb-network-settings');
  if (networkForm) {
    var ethernetDhcp = networkForm.elements.ethernet_dhcp;
    var wifiEnabled = networkForm.elements.wifi_enabled;
    var wifiDhcp = networkForm.elements.wifi_dhcp;
    var dirty = false;
    function syncEthernetFields() {
      networkForm.querySelector('[data-ethernet-fields]').classList.toggle('is-readonly', ethernetDhcp.checked);
      ['ethernet_address', 'ethernet_gateway', 'ethernet_dns'].forEach(function (name) { networkForm.elements[name].disabled = ethernetDhcp.checked; });
    }
    function syncWifiFields() {
      networkForm.querySelector('[data-wifi-address-fields]').classList.toggle('is-readonly', !wifiDhcp.checked);
      networkForm.elements.wifi_address.disabled = wifiDhcp.checked;
      networkForm.querySelector('.bb-network-wifi-fields').classList.toggle('is-readonly', !wifiEnabled.checked);
      ['wifi_ssid', 'wifi_password'].forEach(function (name) { networkForm.elements[name].disabled = !wifiEnabled.checked; });
      wifiDhcp.disabled = !wifiEnabled.checked;
      networkForm.elements.wifi_address.disabled = !wifiEnabled.checked || wifiDhcp.checked;
    }
    syncEthernetFields();
    syncWifiFields();
    networkForm.addEventListener('input', function () { dirty = true; });
    networkForm.addEventListener('change', function (event) {
      dirty = true;
      if (event.target === ethernetDhcp) syncEthernetFields();
      if (event.target === wifiEnabled || event.target === wifiDhcp) syncWifiFields();
    });
    networkForm.querySelectorAll('[data-network-tab]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        networkForm.querySelectorAll('[data-network-tab]').forEach(function (item) {
          var active = item === tab;
          item.classList.toggle('is-active', active);
          item.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        networkForm.querySelectorAll('[role="tabpanel"]').forEach(function (panel) { panel.hidden = panel.id !== tab.getAttribute('aria-controls'); });
      });
    });
    networkForm.querySelectorAll('[data-password-toggle]').forEach(function (toggle) {
      toggle.addEventListener('click', function () {
        var input = toggle.parentElement.querySelector('input');
        var reveal = input.type === 'password';
        input.type = reveal ? 'text' : 'password';
        toggle.title = reveal ? 'Скрыть пароль' : 'Показать пароль';
        toggle.setAttribute('aria-label', toggle.title);
        toggle.querySelector('i').className = reveal ? 'bi bi-eye-slash' : 'bi bi-eye';
      });
    });
    networkForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!window.confirm('Применить сетевые настройки? Текущее подключение может прерваться.')) return;
      var button = document.getElementById('bb-network-save');
      button.disabled = true;
      message('bb-network-message', 'Применяем настройки…');
      var payload = {
        ethernet: {
          dhcp: ethernetDhcp.checked,
          address: networkForm.elements.ethernet_address.value,
          gateway: networkForm.elements.ethernet_gateway.value,
          dns: networkForm.elements.ethernet_dns.value
        },
        wifi: {
          enabled: wifiEnabled.checked,
          dhcp: wifiDhcp.checked,
          address: networkForm.elements.wifi_address.value,
          ssid: networkForm.elements.wifi_ssid.value,
          password: networkForm.elements.wifi_password.value
        }
      };
      try {
        await send('/api/v1/network', 'PUT', payload);
        networkForm.elements.wifi_password.value = '';
        message('bb-network-message', 'Настройки сохранены. Перезагрузите страницу, чтобы увидеть новое состояние.');
        dirty = false;
      } catch (error) { message('bb-network-message', error.message, true); }
      finally { button.disabled = false; }
    });
    window.addEventListener('beforeunload', function (event) {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }
})();
