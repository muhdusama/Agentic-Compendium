/* Native browser APIs only: IndexedDB, service worker and fetch. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = new Map(), published = new Map(), pending = new Map(), conflicts = new Map();
  let database, meta = {cursor: 0, publishedCursor: null, offlineUntil: 0}, authenticated = false;
  let channelId = null, selectedId = null, current = null, search = '', syncing = false, syncRequested = false;
  let showBin = false, selecting = false, accountUsername = 'owner';
  const selectedKeys = new Set();
  let hermes = {connected: false}, noteTimer, toastTimer;
  let flushPromise = null, flushRequested = false;
  let syncError = '', storageError = '', localWrites = 0;
  const localSaves = new Set();
  const recordKey = r => [r.sourceId, r.kind, r.key].join('::');
  const itemKey = item => JSON.stringify([item.sourceId, item.delivery.id]);
  const modes = new Set(['system', 'light', 'dark']);
  const accents = new Set(['teal', 'ocean', 'indigo', 'amber', 'rose']);
  const colourValues = {teal:'#3c8178', ocean:'#287ab5', indigo:'#7161ac', amber:'#b07b2e', rose:'#ba6577'};

  function appearance() {
    const saved = state.get(recordKey({sourceId: 'library', kind: 'setting', key: 'appearance'}))?.value || {};
    return {mode: modes.has(saved.mode) ? saved.mode : 'system',
      accent: accents.has(saved.accent) ? saved.accent : 'teal'};
  }
  function applyAppearance() {
    const {mode, accent} = appearance();
    const dark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.accent = accent;
    $('themeColor').content = dark ? '#141e2b' : '#f7f8f5';
    $('themeMode').value = mode;
    $('accentColor').value = accent;
    document.querySelectorAll('.swatch').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.accent === accent));
    });
  }
  const persist = (store, value) => new Promise((resolve, reject) => {
    const tx = database.transaction(store, 'readwrite');
    tx.objectStore(store).put(value);
    tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
  });
  const remove = (store, key) => new Promise((resolve, reject) => {
    const tx = database.transaction(store, 'readwrite');
    tx.objectStore(store).delete(key);
    tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
  });
  // Queue the durable draft and its visible record in the same transaction.
  const saveRecord = (id, record, draft) => new Promise((resolve, reject) => {
    const tx = database.transaction(['records', 'pending'], 'readwrite');
    tx.objectStore('records').put({id, record});
    if (draft) tx.objectStore('pending').put({id, record: draft});
    else tx.objectStore('pending').delete(id);
    tx.oncomplete = resolve;
    tx.onabort = tx.onerror = () => reject(tx.error || new Error('Local save failed'));
  });
  const all = store => new Promise((resolve, reject) => {
    const tx = database.transaction(store, 'readonly');
    const req = tx.objectStore(store).getAll();
    req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error);
  });
  async function openDatabase() {
    database = await new Promise((resolve, reject) => {
      const req = indexedDB.open('app01-offline-v1', 1);
      req.onupgradeneeded = () => {
        for (const store of ['records', 'published', 'pending', 'meta', 'conflicts']) {
          if (!req.result.objectStoreNames.contains(store)) req.result.createObjectStore(store, {keyPath: 'id'});
        }
      };
      req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error);
    });
    for (const r of await all('records')) state.set(r.id, r.record);
    for (const r of await all('published')) published.set(r.id, r.delivery);
    for (const r of await all('pending')) pending.set(r.id, r.record);
    for (const r of await all('conflicts')) conflicts.set(r.id, r.current);
    const settings = (await all('meta')).find(r => r.id === 'sync');
    if (settings) meta = settings.value;
    applyAppearance();
  }
  const toast = message => {
    $('toast').textContent = message; $('toast').classList.add('visible');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 3900);
  };
  const status = (message, offline = false) => {
    $('connectivity').textContent = message;
    $('connectivity').classList.toggle('offline', offline);
  };
  function updateSyncStatus() {
    const count = pending.size;
    const message = storageError ? 'Local save failed' : conflicts.size ? `${conflicts.size} sync conflict(s)`
      : !navigator.onLine ? (count ? `Offline · ${count} pending` : 'Offline archive')
      : !authenticated ? 'Sign in to sync' : localWrites ? 'Saving on this device…'
      : count ? `${count} change(s) waiting to sync` : syncError || (syncing || flushPromise ? 'Synchronising…' : 'Synced');
    status(message, !!storageError || !!syncError || !!count || !authenticated || !navigator.onLine);
    $('connectivity').title = message;
    $('reviewConflicts').classList.toggle('hidden', !conflicts.size);
    $('reviewConflicts').textContent = `Review ${conflicts.size} conflict(s)`;
    const key = current && recordKey({sourceId: current.sourceId, kind: 'note', key: current.delivery.id});
    $('noteStatus').textContent = storageError ? 'Not saved — keep this tab open'
      : key && conflicts.has(key) ? 'Conflict — your draft is retained'
      : localWrites ? 'Saving on this device…'
      : key && pending.has(key) ? 'Saved on this device · waiting to sync'
      : 'Saved';
  }
  async function api(path, method = 'GET', body) {
    const options = {method, credentials: 'same-origin', headers: {Accept: 'application/json'}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetch('/app/api/' + path, options);
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || 'Request failed');
      error.status = response.status; error.result = result; throw error;
    }
    return result;
  }
  function workspace(show) {
    $('login').classList.toggle('hidden', show);
    $('workspace').classList.toggle('hidden', !show);
    if (show) render();
  }
  async function login(event) {
    event.preventDefault();
    $('loginMessage').textContent = '';
    try {
      accountUsername = $('username').value;
      await api('login', 'POST', {username: accountUsername, password: $('password').value});
      $('password').value = ''; authenticated = true;
      meta.offlineUntil = Date.now() + 12 * 3600 * 1000;
      if (database) await persist('meta', {id: 'sync', value: meta});
      const session = await api('session');
      hermes = session.hermes || {connected: false};
      workspace(true);
      await syncAll();
    } catch (error) { $('loginMessage').textContent = error.message; }
  }
  async function init() {
    $('loginForm').addEventListener('submit', login);
    bindUI();
    try { await openDatabase(); } catch (error) { toast('Offline storage unavailable: ' + error.message); }
    try {
      const session = await api('session');
      authenticated = true; hermes = session.hermes || hermes;
      accountUsername = session.username || 'owner';
      $('username').value = accountUsername;
      meta.offlineUntil = Date.now() + 12 * 3600 * 1000;
      if (database) await persist('meta', {id: 'sync', value: meta});
      workspace(true); await syncAll();
    } catch (error) {
      if (!navigator.onLine && meta.offlineUntil > Date.now() && (state.size || published.size)) {
        workspace(true); status('Offline archive', true);
      } else {
        workspace(false);
        $('loginMessage').textContent = error.status === 401 ? '' : 'Server unavailable. Connect to your LAN or VPN.';
      }
    }
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/app/sw.js', {scope: '/app/'}).catch(() => {});
    window.addEventListener('online', async () => {
      try {
        const session = await api('session');
        authenticated = true; hermes = session.hermes || hermes;
        accountUsername = session.username || accountUsername;
        workspace(true); await syncAll();
      } catch (_) {
        authenticated = false; workspace(false);
      }
    });
    window.addEventListener('offline', updateSyncStatus);
    window.addEventListener('beforeunload', event => {
      if (localWrites || storageError) { event.preventDefault(); event.returnValue = ''; }
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && authenticated) syncAll(); });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyAppearance);
  }
  async function syncAll() {
    if (syncing) { syncRequested = true; return; }
    if (!authenticated || !database) return;
    syncing = true; syncError = ''; updateSyncStatus();
    try {
      await flushPending();
      let more = true, iterations = 0;
      while (more && iterations++ < 200) {
        const data = await api('sync?cursor=' + (meta.cursor || 0));
        for (const record of data.changes) {
          const id = recordKey(record);
          if (!pending.has(id)) {
            state.set(id, record);
            await persist('records', {id, record});
          } else if (record.version > pending.get(id).baseVersion) {
            conflicts.set(id, record); await persist('conflicts', {id, current: record});
          }
        }
        meta.cursor = data.cursor; more = data.hasMore;
        await persist('meta', {id: 'sync', value: meta});
      }
      more = true; iterations = 0;
      while (more && iterations++ < 200) {
        const suffix = meta.publishedCursor ? '?cursor=' + encodeURIComponent(meta.publishedCursor) : '';
        const data = await api('published' + suffix);
        for (const delivery of data.deliveries) {
          published.set(delivery.id, delivery);
          await persist('published', {id: delivery.id, delivery});
        }
        meta.publishedCursor = data.cursor; more = data.hasMore;
        await persist('meta', {id: 'sync', value: meta});
      }
      applyAppearance();
      render();
    } catch (error) {
      syncError = error.status === 401 ? 'Sign in again' : 'Sync failed · retry Sync';
      if (error.status === 401) authenticated = false;
      render();
    } finally {
      syncing = false;
      updateSyncStatus();
      if (syncRequested) { syncRequested = false; if (authenticated) syncAll(); }
    }
  }
  async function edit(sourceId, kind, key, value, deleted = false, deferUpdates = false) {
    if (!database) { toast('Offline storage unavailable'); return; }
    const id = recordKey({sourceId, kind, key});
    const original = pending.get(id) || state.get(id);
    const baseVersion = pending.get(id)?.baseVersion ?? original?.version ?? 0;
    const draft = {sourceId, kind, key, baseVersion, value, deleted};
    pending.set(id, draft);
    state.set(id, {...draft, version: baseVersion});
    localWrites++; updateSyncStatus();
    const save = saveRecord(id, {...draft, version: baseVersion}, draft);
    localSaves.add(save);
    try { await save; }
    catch (error) { storageError = error.message; throw error; }
    finally { localWrites--; localSaves.delete(save); updateSyncStatus(); }
    if (!deferUpdates) {
      render();
      if (authenticated && navigator.onLine) flushPending().catch(() => {});
    }
  }
  async function flushPending() {
    flushRequested = true;
    if (flushPromise) return flushPromise;
    flushPromise = (async () => {
      await Promise.all([...localSaves]);
      while (flushRequested) {
        flushRequested = false;
        for (const [id, draft] of [...pending]) {
          if (conflicts.has(id)) continue;
          try {
            const result = await api('state', 'PUT', draft);
            const newer = pending.get(id);
            if (newer && newer !== draft) {
              // Another local edit arrived during this request. Its CAS base
              // must advance, but its newer body must not be overwritten.
              newer.baseVersion = result.record.version;
              state.set(id, {...newer, version: newer.baseVersion});
              await saveRecord(id, state.get(id), newer);
              flushRequested = true;
            } else {
              pending.delete(id); state.set(id, result.record);
              await saveRecord(id, result.record, null);
            }
          } catch (error) {
            if (error.status === 409) {
              conflicts.set(id, error.result.current);
              await persist('conflicts', {id, current: error.result.current});
              toast('Concurrent edit detected. Local changes retained for review.');
              continue;
            }
            throw error;
          }
        }
      }
    })();
    try { await flushPromise; syncError = ''; }
    catch (error) {
      syncError = error.status === 401 ? 'Sign in again' : 'Sync failed · retry Sync';
      if (error.status === 401) authenticated = false;
      throw error;
    } finally { flushPromise = null; updateSyncStatus(); }
  }
  async function resolveConflicts() {
    if (!conflicts.size) return toast('No conflicts to review.');
    showConflict();
    $('conflictDialog').showModal();
  }
  function showConflict() {
    const entry = [...conflicts].find(([id]) => pending.has(id));
    if (!entry) { $('conflictDialog').close(); return; }
    const [id, remote] = entry, local = pending.get(id);
    $('conflictTitle').textContent = `Conflicting ${local.kind}`;
    const preview = record => record?.deleted ? '(Deleted)' : record?.value?.text ?? JSON.stringify(record?.value || {}, null, 2);
    $('localConflict').textContent = preview(local);
    $('remoteConflict').textContent = preview(remote);
    $('exportConflict').onclick = () => downloadBlob(new Blob([JSON.stringify(local, null, 2)], {type:'application/json'}), `compendium-draft-${Date.now()}.json`);
    const resolve = async keepLocal => {
      try {
        if (keepLocal) {
          const draft = {...local, baseVersion: remote?.version || 0};
          pending.set(id, draft); state.set(id, {...draft, version: draft.baseVersion});
          await saveRecord(id, state.get(id), draft);
        } else if (remote) {
          pending.delete(id); state.set(id, remote);
          await saveRecord(id, remote, null);
        } else return toast('Sync again before choosing a server version.');
        conflicts.delete(id); await remove('conflicts', id);
        showConflict(); render(); updateSyncStatus();
        if (authenticated && navigator.onLine) await syncAll();
      } catch (error) { toast('Could not resolve conflict: ' + error.message); }
    };
    $('keepLocal').onclick = () => resolve(true);
    $('keepRemote').onclick = () => resolve(false);
  }
  const records = kind => [...state.values()].filter(r => r.kind === kind && !r.deleted);
  const ordered = (a, b) => (a.value.order || 0) - (b.value.order || 0) || a.key.localeCompare(b.key);
  const channels = () => records('channel').sort(ordered);
  const sections = () => records('section').sort(ordered);
  const sourceKey = (source, kind, key) => state.get(recordKey({sourceId: source, kind, key}));
  const note = (source, key) => sourceKey(source, 'note', key)?.value || {};
  function destinations() { return channels().map(r => ({id: r.key, name: r.value.name, sectionId: r.value.sectionId || null, archived: !!r.value.archived})); }
  function placement(source, delivery) {
    const manual = sourceKey(source, 'placement', delivery.id);
    // An explicit Unfiled placement must override a job's default channel.
    if (manual && !manual.deleted && Object.hasOwn(manual.value, 'channelId')) {
      return manual.value.channelId || null;
    }
    const job = delivery.hermesJobID;
    const routed = job ? sourceKey(source, 'route', job)?.value.channelId : null;
    return routed || null;
  }
  function allDeliveries(inBin = false) {
    const items = records('delivery').map(r => ({sourceId: r.sourceId, delivery: r.value}));
    for (const d of published.values()) items.push({sourceId: 'delivery-service', delivery: d});
    return items.filter(({sourceId, delivery}) =>
      !!sourceKey(sourceId, 'hidden-delivery', delivery.id)?.value.hidden === inBin
    ).sort((a, b) => Date.parse(b.delivery.deliveredAt) - Date.parse(a.delivery.deliveredAt));
  }
  const labelDate = date => new Intl.DateTimeFormat('en-AU', {day: 'numeric', month: 'long', year: 'numeric'}).format(date);
  const shortDate = date => new Intl.DateTimeFormat('en-AU', {day: 'numeric', month: 'short'}).format(date);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function setPanel(name) { $('workspace').dataset.view = name; }
  function navigateTo(id = null, bin = false) {
    channelId = id; showBin = bin; selectedId = null; current = null;
    selecting = false; selectedKeys.clear(); setPanel('list'); render();
  }
  function render() {
    if ($('workspace').classList.contains('hidden')) return;
    applyAppearance();
    renderChannels(); renderList(); renderReader();
    updateSyncStatus();
    document.querySelectorAll('[data-panel]').forEach(button => {
      button.setAttribute('aria-current', $('workspace').dataset.view === button.dataset.panel ? 'page' : 'false');
    });
  }
  function dragPayload(event, type, id) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', JSON.stringify({type, id}));
  }
  function acceptDrop(target, action) {
    target.addEventListener('dragover', event => {
      if (!event.dataTransfer.types.includes('text/plain')) return;
      event.preventDefault(); event.dataTransfer.dropEffect = 'move';
      target.classList.add('drag-over');
    });
    target.addEventListener('dragleave', () => target.classList.remove('drag-over'));
    target.addEventListener('drop', async event => {
      event.preventDefault(); target.classList.remove('drag-over');
      try {
        const dragged = JSON.parse(event.dataTransfer.getData('text/plain'));
        if (!dragged || !['channel', 'section'].includes(dragged.type)) return;
        await action(dragged);
      } catch (error) { toast('Cannot reorder: ' + error.message); }
    });
  }
  async function reorderChannel(id, sectionId, beforeId = null) {
    const moving = sourceKey('library', 'channel', id);
    if (!moving || moving.deleted || (sectionId && !sourceKey('library', 'section', sectionId))) return;
    const target = channels().filter(item => (item.value.sectionId || null) === sectionId && !item.value.archived && item.key !== id);
    const insert = beforeId ? target.findIndex(item => item.key === beforeId) : -1;
    target.splice(insert < 0 ? target.length : insert, 0, moving);
    for (let index = 0; index < target.length; index++) {
      const record = target[index];
      if (record.value.order === index && (record.value.sectionId || null) === sectionId) continue;
      await edit('library', 'channel', record.key, {...record.value, sectionId, order:index}, false, true);
    }
    render();
    if (authenticated && navigator.onLine) flushPending().catch(() => toast('Channel changes saved offline.'));
  }
  async function reorderSection(id, beforeId = null) {
    const moving = sourceKey('library', 'section', id);
    if (!moving || moving.deleted) return;
    const rest = sections().filter(item => item.key !== id);
    const position = beforeId ? rest.findIndex(item => item.key === beforeId) : -1;
    rest.splice(position < 0 ? rest.length : position, 0, moving);
    for (let index = 0; index < rest.length; index++) {
      const record = rest[index];
      if (record.value.order !== index) {
        await edit('library', 'section', record.key, {...record.value, order:index}, false, true);
      }
    }
    render();
    if (authenticated && navigator.onLine) flushPending().catch(() => toast('Section changes saved offline.'));
  }
  async function moveWithinGroup(id, direction) {
    const moving = sourceKey('library', 'channel', id);
    if (!moving) return;
    const sectionId = moving.value.sectionId || null;
    const peers = channels().filter(item => (item.value.sectionId || null) === sectionId && !item.value.archived);
    const index = peers.findIndex(item => item.key === id);
    const destination = index + direction;
    if (destination < 0 || destination >= peers.length) return;
    const target = peers[destination];
    if (direction < 0) await reorderChannel(id, sectionId, target.key);
    else await reorderChannel(target.key, sectionId, id);
  }
  async function createChannel(sectionId = null) {
    const name = prompt('Create channel (1–48 characters)');
    if (!name?.trim()) return;
    if (name.trim().length > 48 || destinations().some(item => item.name.toLowerCase() === name.trim().toLowerCase())) {
      return toast('Channel name is too long or already in use.');
    }
    const key = crypto.randomUUID();
    const index = channels().filter(item => (item.value.sectionId || null) === sectionId).length;
    await edit('library', 'channel', key, {name:name.trim(), sectionId, order:index, archived:false});
    navigateTo(key);
  }
  async function createSection() {
    const name = prompt('Section name (1–48 characters)');
    if (!name?.trim()) return;
    if (name.trim().length > 48 || sections().some(item => item.value.name.toLowerCase() === name.trim().toLowerCase())) {
      return toast('Section name is too long or already in use.');
    }
    await edit('library', 'section', crypto.randomUUID(), {name:name.trim(), order:sections().length});
  }
  function renderChannels() {
    const root = $('channels'); root.replaceChildren();
    const allButton = element('button', 'channel-button' + (!channelId && !showBin ? ' active' : ''));
    allButton.append(element('span', '', '◫  All deliveries'), element('small', '', String(allDeliveries().length)));
    allButton.onclick = () => navigateTo();
    root.append(allButton);
    const binButton = element('button', 'channel-button' + (showBin ? ' active' : ''));
    binButton.append(element('span', '', 'Bin'), element('small', '', String(allDeliveries(true).length)));
    binButton.onclick = () => navigateTo(null, true);
    root.append(binButton);

    const drawChannel = (record, parent) => {
      const button = element('button', 'channel-button draggable-channel' + (channelId === record.key ? ' active' : ''));
      button.dataset.channelId = record.key;
      button.draggable = true;
      const name = element('span', '', '⋮⋮  ' + record.value.name);
      const count = allDeliveries().filter(({sourceId, delivery}) => placement(sourceId, delivery) === record.key).length;
      button.append(name, element('small', '', String(count)));
      button.title = 'Drag to reorder or move to a section';
      button.onclick = () => navigateTo(record.key);
      button.addEventListener('dragstart', event => dragPayload(event, 'channel', record.key));
      acceptDrop(button, dragged => dragged.type === 'channel' && dragged.id !== record.key
        ? reorderChannel(dragged.id, record.value.sectionId || null, record.key)
        : undefined);
      parent.append(button);
    };
    const drawGroup = (sectionId, label, recordsInGroup, isSection = false) => {
      const group = element('div', 'sidebar-section');
      const heading = element('div', 'sidebar-section-heading');
      heading.dataset.sectionId = sectionId || '';
      const title = element('button', 'section-toggle', label);
      const collapsedKey = `app01-section-${sectionId || 'unsectioned'}`;
      const collapsed = localStorage.getItem(collapsedKey) === 'true';
      title.setAttribute('aria-expanded', String(!collapsed));
      title.title = 'Collapse or expand section';
      title.onclick = () => {localStorage.setItem(collapsedKey, String(!collapsed)); renderChannels();};
      heading.append(title, element('small', 'section-counter', String(recordsInGroup.length)));
      if (isSection) {
        heading.draggable = true;
        heading.addEventListener('dragstart', event => dragPayload(event, 'section', sectionId));
        const rename = element('button', 'section-action', '⋯');
        rename.title = `Rename ${label}`;
        rename.setAttribute('aria-label', `Rename ${label}`);
        rename.onclick = async () => {
          const value = prompt('Rename section', label);
          if (!value?.trim() || value.trim().length > 48) return;
          if (sections().some(item => item.key !== sectionId && item.value.name.toLowerCase() === value.trim().toLowerCase())) return toast('That section already exists.');
          const record = sourceKey('library', 'section', sectionId);
          await edit('library', 'section', sectionId, {...record.value, name:value.trim()});
        };
        heading.append(rename);
        const add = element('button', 'section-action', '+');
        add.title = `Add channel to ${label}`;
        add.setAttribute('aria-label', `Add channel to ${label}`);
        add.onclick = () => createChannel(sectionId);
        heading.append(add);
      }
      acceptDrop(heading, dragged => dragged.type === 'channel'
        ? reorderChannel(dragged.id, sectionId)
        : dragged.type === 'section' && isSection && dragged.id !== sectionId
          ? reorderSection(dragged.id, sectionId) : undefined);
      group.append(heading);
      if (!collapsed) {
        const children = element('div', 'sidebar-section-children');
        for (const record of recordsInGroup) drawChannel(record, children);
        group.append(children);
      }
      root.append(group);
    };
    const visible = channels().filter(record => !record.value.archived);
    const grouped = new Set(sections().map(item => item.key));
    drawGroup(null, 'Unsectioned', visible.filter(item => !item.value.sectionId || !grouped.has(item.value.sectionId)));
    for (const section of sections()) {
      drawGroup(section.key, section.value.name,
        visible.filter(item => item.value.sectionId === section.key), true);
    }
    const archived = destinations().filter(c => c.archived);
    if (archived.length) {
      root.append(element('div', 'day-heading', 'Archived channels'));
      for (const channel of archived) {
        const button = element('button', 'channel-button', 'Restore · ' + channel.name);
        button.onclick = async () => {
          const current = sourceKey('library', 'channel', channel.id);
          if (current) await edit('library', 'channel', channel.id, {...current.value, archived: false});
        };
        root.append(button);
      }
    }
    if (channelId) {
      const actions = element('div', 'channel-options');
      const rename = element('button', '', 'Rename');
      rename.onclick = async () => {
        const r = sourceKey('library', 'channel', channelId);
        const name = prompt('Channel name', r?.value.name);
        if (name && name.trim() && name.trim().length <= 48 && !destinations().some(c => c.id !== channelId && c.name.toLowerCase() === name.trim().toLowerCase())) {
          await edit('library', 'channel', channelId, {...r.value, name: name.trim()});
        }
      };
      const archive = element('button', '', 'Archive');
      archive.onclick = async () => {
        const r = sourceKey('library', 'channel', channelId);
        if (r && confirm('Archive this channel? Past deliveries and notes will remain in All deliveries.')) {
          await edit('library', 'channel', channelId, {...r.value, archived: true}); channelId = null; render();
        }
      };
      const moveUp = element('button', '', 'Move up');
      moveUp.onclick = () => moveWithinGroup(channelId, -1);
      const moveDown = element('button', '', 'Move down');
      moveDown.onclick = () => moveWithinGroup(channelId, 1);
      const choice = element('select', 'channel-section-select');
      choice.setAttribute('aria-label', 'Move channel to section');
      choice.add(new Option('Unsectioned', ''));
      for (const section of sections()) choice.add(new Option(section.value.name, section.key));
      choice.value = sourceKey('library', 'channel', channelId)?.value.sectionId || '';
      choice.onchange = () => reorderChannel(channelId, choice.value || null);
      actions.append(rename, archive, moveUp, moveDown, choice); root.append(actions);
    }
  }
  function visibleDeliveries() {
    return allDeliveries(showBin).filter(({sourceId, delivery}) =>
      (showBin || !channelId || placement(sourceId, delivery) === channelId) &&
      (!search || [delivery.title, delivery.summary, delivery.body, delivery.publisher]
        .some(text => String(text || '').toLocaleLowerCase().includes(search)))
    );
  }
  function toggleSelected(item) {
    const id = itemKey(item);
    if (selectedKeys.has(id)) selectedKeys.delete(id);
    else selectedKeys.add(id);
    renderList();
  }
  function renderBulkActions(items) {
    $('selectMode').textContent = selecting ? 'Done' : 'Select';
    $('selectMode').setAttribute('aria-pressed', String(selecting));
    $('bulkActions').classList.toggle('hidden', !selecting);
    if (!selecting) return;
    $('selectedCount').textContent = `${selectedKeys.size} selected`;
    const visibleIds = items.map(itemKey);
    $('selectAll').textContent = visibleIds.length > 0 && visibleIds.every(id => selectedKeys.has(id)) ? 'Deselect all' : 'Select all';
    const destination = $('bulkChannel'), previous = destination.value;
    destination.replaceChildren(new Option('Unfiled', ''));
    for (const channel of destinations().filter(channel => !channel.archived)) {
      destination.add(new Option(channel.name, channel.id));
    }
    destination.value = Array.from(destination.options).some(option => option.value === previous) ? previous : '';
    const hasSelection = selectedKeys.size > 0;
    for (const id of ['bulkMove', 'bulkRead', 'bulkUnread', 'bulkDelete', 'bulkRestore']) {
      $(id).disabled = !hasSelection;
    }
    $('bulkMove').classList.toggle('hidden', showBin);
    $('bulkChannel').classList.toggle('hidden', showBin);
    $('bulkRead').classList.toggle('hidden', showBin);
    $('bulkUnread').classList.toggle('hidden', showBin);
    $('bulkDelete').classList.toggle('hidden', showBin);
    $('bulkRestore').classList.toggle('hidden', !showBin);
  }
  async function applyBulk(action) {
    const items = visibleDeliveries().filter(item => selectedKeys.has(itemKey(item)));
    if (!items.length) return;
    if (action === 'delete' && !confirm(`Move ${items.length} selected deliveries to Bin? Notes and original content are retained and can be restored.`)) return;
    try {
      for (const item of items) {
        const {sourceId, delivery} = item;
        if (action === 'move') {
          await edit(sourceId, 'placement', delivery.id, {channelId: $('bulkChannel').value || null}, false, true);
        } else if (action === 'read' || action === 'unread') {
          const previous = note(sourceId, delivery.id);
          const isRead = action === 'read';
          if (previous.isRead === isRead) continue;
          await edit(sourceId, 'note', delivery.id,
            {...previous, isRead, updatedAtWeb: new Date().toISOString()}, false, true);
        } else {
          await edit(sourceId, 'hidden-delivery', delivery.id,
            {hidden: action === 'delete'}, false, true);
        }
      }
      const count = items.length;
      selectedKeys.clear(); selecting = false; selectedId = null; current = null;
      render();
      toast(`${count} deliveries ${action === 'delete' ? 'moved to Bin' : action === 'restore' ? 'restored' : action === 'move' ? 'moved' : 'updated'}.`);
      if (authenticated && navigator.onLine) flushPending().catch(() => toast('Changes are saved locally until sync resumes.'));
    } catch (error) { toast('Could not update selection: ' + error.message); render(); }
  }
  function renderList() {
    $('listTitle').textContent = showBin ? 'Bin' : channelId ? destinations().find(c => c.id === channelId)?.name || 'Archive' : 'All deliveries';
    $('selectionLabel').textContent = showBin ? 'RECOVERABLE' : channelId ? 'CHANNEL' : 'LIBRARY';
    const items = visibleDeliveries(); $('deliveryCount').textContent = String(items.length);
    renderBulkActions(items);
    const root = $('deliveryList'); root.replaceChildren();
    if (!items.length) {
      const blank = element('div', 'blank-list');
      blank.append(element('strong', '', showBin ? 'Bin is empty' : 'Nothing here yet'), element('p', '', showBin
        ? 'Removed deliveries appear here until restored. Their original history and notes remain intact.'
        : 'Connect Hermes or choose another channel to see your deliveries.'));
      root.append(blank); return;
    }
    let lastDay = '';
    for (const item of items) {
      const d = item.delivery, stamp = new Date(d.deliveredAt), day = isNaN(stamp) ? 'Unknown date' : labelDate(stamp);
      if (day !== lastDay) { root.append(element('div', 'day-heading', day)); lastDay = day; }
      const id = itemKey(item);
      const button = element('button', 'delivery-card' + (selectedId === id ? ' active' : ''));
      const line = element('div', 'card-meta');
      line.append(element('span', '', d.publisher || 'Delivery'), element('span', '', isNaN(stamp) ? '' : shortDate(stamp)));
      const heading = element('h3', '', d.title || 'Untitled delivery');
      if (!note(item.sourceId, d.id).isRead) heading.append(element('span', 'unread-dot', '●'));
      const preview = window.CompendiumRichText.render(d.summary || String(d.body || '').slice(0, 145));
      button.append(line, heading, element('p', '', (preview.textContent || '').slice(0, 180)));
      button.onclick = () => selecting ? toggleSelected(item) : selectDelivery(item);
      if (selecting) {
        const row = element('div', 'selectable-row' + (selectedKeys.has(id) ? ' selected' : ''));
        const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.className = 'delivery-checkbox';
        checkbox.checked = selectedKeys.has(id);
        checkbox.setAttribute('aria-label', 'Select ' + (d.title || 'delivery'));
        checkbox.onchange = () => toggleSelected(item);
        row.append(checkbox, button); root.append(row);
      } else root.append(button);
    }
  }
  async function selectDelivery(item) {
    current = item; selectedId = itemKey(item);
    setPanel('reader'); render();
    if (!showBin && !note(item.sourceId, item.delivery.id).isRead) {
      await changeNote(item.sourceId, item.delivery.id, {isRead: true});
    }
  }
  function renderReader() {
    if (!current || !allDeliveries(showBin).some(i => i.sourceId === current.sourceId && i.delivery.id === current.delivery.id)) {
      $('emptyReader').classList.remove('hidden'); $('readerContent').classList.add('hidden'); return;
    }
    $('emptyReader').classList.add('hidden'); $('readerContent').classList.remove('hidden');
    const d = current.delivery, n = note(current.sourceId, d.id);
    $('readerTitle').textContent = d.title || 'Untitled';
    $('readerPublisher').textContent = (d.publisher || 'Delivery') + ' · ' + current.sourceId;
    const stamp = new Date(d.deliveredAt);
    $('readerDate').textContent = isNaN(stamp) ? '' : labelDate(stamp);
    $('readerSummary').replaceChildren(window.CompendiumRichText.render(d.summary || ''));
    $('readerBody').replaceChildren(window.CompendiumRichText.render(d.body || ''));
    if (document.activeElement !== $('noteText')) $('noteText').value = n.text || '';
    $('toggleRead').textContent = n.isRead ? 'Mark unread' : 'Mark read';
    const pick = $('readerChannel'); pick.replaceChildren(new Option('Unfiled', ''));
    for (const ch of destinations()) pick.add(new Option(ch.name + (ch.archived ? ' (archived)' : ''), ch.id));
    pick.value = placement(current.sourceId, d) || '';
    pick.disabled = showBin;
    $('toggleRead').disabled = showBin;
    $('hideDelivery').textContent = showBin ? 'Restore' : 'Move to Bin';
  }
  async function changeNote(source, id, changes, deferUpdates = false) {
    const old = note(source, id);
    await edit(source, 'note', id, {...old, ...changes, updatedAtWeb: new Date().toISOString()}, false, deferUpdates);
  }
  function bindUI() {
    document.querySelectorAll('[data-panel]').forEach(button => button.onclick = () => {setPanel(button.dataset.panel); render();});
    $('search').oninput = event => {
      search = event.target.value.toLocaleLowerCase().trim();
      selectedKeys.clear(); renderList();
    };
    $('selectMode').onclick = () => {
      selecting = !selecting; selectedKeys.clear(); renderList();
    };
    $('selectAll').onclick = () => {
      const ids = visibleDeliveries().map(itemKey);
      if (ids.every(id => selectedKeys.has(id))) ids.forEach(id => selectedKeys.delete(id));
      else ids.forEach(id => selectedKeys.add(id));
      renderList();
    };
    $('clearSelection').onclick = () => {selectedKeys.clear(); renderList();};
    $('bulkMove').onclick = () => applyBulk('move');
    $('bulkRead').onclick = () => applyBulk('read');
    $('bulkUnread').onclick = () => applyBulk('unread');
    $('bulkDelete').onclick = () => applyBulk('delete');
    $('bulkRestore').onclick = () => applyBulk('restore');
    $('toggleChannels').onclick = () => setCollapsed('channels', $('workspace').dataset.channelsCollapsed !== 'true');
    $('toggleList').onclick = () => setCollapsed('list', $('workspace').dataset.listCollapsed !== 'true');
    applyLayout();
    $('themeMode').onchange = updateAppearance;
    $('accentColor').onchange = updateAppearance;
    renderColourSwatches();
    $('accountForm').onsubmit = changeAccount;
    $('newChannel').onclick = () => createChannel();
    $('newSection').onclick = createSection;
    $('openSettings').onclick = () => {showSettings(); $('settingsDialog').showModal();};
    $('refresh').onclick = async () => {
      if (!authenticated) return toast('Sign in to sync.');
      try {
        if (hermes.connected) {const info = await api('hermes/pull', 'POST', {}); if (info.jobsAtWindowLimit) toast('Hermes has a limited recent-history window. Some runs may not be recoverable.');}
        await syncAll();
      } catch (error) {toast(error.message);}
    };
    $('readerChannel').onchange = async () => {
      if (!current) return;
      await edit(current.sourceId, 'placement', current.delivery.id,
        {channelId: $('readerChannel').value || null});
    };
    $('toggleRead').onclick = () => current && changeNote(current.sourceId, current.delivery.id,
      {isRead: !note(current.sourceId, current.delivery.id).isRead});
    $('downloadRTF').onclick = () => {
      if (!current) return;
      const rtf = window.CompendiumRichText.toRTF($('readerBody'));
      const safeName = (current.delivery.title || 'Compendium delivery').replace(/[^A-Za-z0-9 _-]/g, '').slice(0, 70);
      downloadBlob(new Blob([rtf], {type: 'application/rtf'}), `${safeName || 'Compendium'}.rtf`);
    };
    $('hideDelivery').onclick = async () => {
      if (!current || (!showBin && !confirm('Move this delivery to Bin? Its history and notes are preserved.'))) return;
      await edit(current.sourceId, 'hidden-delivery', current.delivery.id, {hidden: !showBin});
      current = null; selectedId = null; setPanel('list'); render();
    };
    $('noteText').oninput = () => {
      if (!current) return;
      const source = current.sourceId, id = current.delivery.id, text = $('noteText').value;
      // Persist every input now; only the network request is debounced.
      changeNote(source, id, {text}, true).catch(error => toast('Note not saved: ' + error.message));
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => {
        if (authenticated && navigator.onLine) flushPending().catch(() => {});
      }, 450);
    };
    $('hermesForm').onsubmit = connectHermes;
    $('importButton').onclick = importNative;
    $('exportArchive').onclick = exportArchive;
    $('logout').onclick = async () => {
      try {await api('logout', 'POST', {});} catch (_) {
        return toast('Connect to the LAN or VPN to sign out securely.');
      }
      meta.offlineUntil = 0;
      if (database) await persist('meta', {id: 'sync', value: meta});
      authenticated = false; $('settingsDialog').close(); workspace(false);
      status('Signed out', true);
    };
    $('resolveConflicts').onclick = resolveConflicts;
    $('reviewConflicts').onclick = resolveConflicts;
  }
  function applyLayout() {
    const compactTablet = window.innerWidth > 760 && window.innerWidth <= 1050;
    for (const name of ['channels', 'list']) {
      const saved = localStorage.getItem(`app01-layout-${name}`);
      const collapsed = saved === null ? name === 'channels' && compactTablet : saved === 'true';
      $('workspace').dataset[name === 'channels' ? 'channelsCollapsed' : 'listCollapsed'] = String(collapsed);
      $('toggle' + (name === 'channels' ? 'Channels' : 'List')).setAttribute('aria-expanded', String(!collapsed));
    }
  }
  function setCollapsed(name, collapsed) {
    localStorage.setItem(`app01-layout-${name}`, String(collapsed));
    applyLayout();
  }
  function renderColourSwatches() {
    const root = $('colourSwatches'); root.replaceChildren();
    for (const [accent, colour] of Object.entries(colourValues)) {
      const button = element('button', 'swatch');
      button.type = 'button'; button.dataset.accent = accent;
      button.style.setProperty('--swatch', colour);
      button.setAttribute('aria-label', accent[0].toUpperCase() + accent.slice(1));
      button.title = button.getAttribute('aria-label');
      button.onclick = () => {$('accentColor').value = accent; updateAppearance();};
      root.append(button);
    }
    applyAppearance();
  }
  async function updateAppearance() {
    const mode = $('themeMode').value, accent = $('accentColor').value;
    if (!modes.has(mode) || !accents.has(accent)) return;
    await edit('library', 'setting', 'appearance', {mode, accent});
    applyAppearance();
  }
  async function changeAccount(event) {
    event.preventDefault();
    const username = $('accountUsername').value.trim();
    const currentPassword = $('accountCurrentPassword').value;
    const newPassword = $('accountNewPassword').value;
    if (newPassword !== $('accountConfirmPassword').value) {
      $('accountStatus').textContent = 'The new passwords do not match.'; return;
    }
    if (newPassword && newPassword.length < 14) {
      $('accountStatus').textContent = 'New password must contain at least 14 characters.'; return;
    }
    const saveButton = $('accountForm').querySelector('button[type="submit"]');
    saveButton.disabled = true;
    try {
      await api('account', 'POST', {currentPassword, username, newPassword});
      accountUsername = username;
      $('username').value = username;
      $('accountForm').reset();
      meta.offlineUntil = 0;
      if (database) await persist('meta', {id: 'sync', value: meta});
      authenticated = false;
      $('settingsDialog').close(); workspace(false);
      toast('Owner credentials updated. Sign in again on this and other devices.');
    } catch (error) { $('accountStatus').textContent = error.message; }
    finally { saveButton.disabled = false; }
  }
  function showSettings() {
    $('accountUsername').value = accountUsername;
    $('accountCurrentPassword').value = '';
    $('accountNewPassword').value = '';
    $('accountConfirmPassword').value = '';
    $('accountStatus').textContent = '';
    applyAppearance();
    const details = hermes || {};
    $('hermesUrl').value = details.url || '';
    $('hermesUser').value = details.username || '';
    $('hermesPass').value = '';
    $('hermesStatus').textContent = details.connected ? 'Connected to ' + details.url : 'Not connected';
    const selector = $('hermesSource'); selector.replaceChildren(new Option('New source', ''));
    const ids = new Set(records('delivery').map(r => r.sourceId).concat(records('note').map(r => r.sourceId)));
    if (details.sourceId) ids.add(details.sourceId);
    for (const id of ids) if (id !== 'library' && id !== 'delivery-service') selector.add(new Option('Existing: ' + id, id));
    selector.value = details.sourceId || '';
    renderJobs();
  }
  async function connectHermes(event) {
    event.preventDefault();
    $('hermesStatus').textContent = 'Connecting…';
    try {
      hermes = await api('hermes', 'POST', {url: $('hermesUrl').value, username: $('hermesUser').value,
        password: $('hermesPass').value, sourceId: $('hermesSource').value});
      $('hermesPass').value = '';
      $('hermesStatus').textContent = 'Connected to ' + hermes.url + '. Select the jobs you want to import.';
      await syncAll(); showSettings();
    } catch (error) { $('hermesStatus').textContent = error.message; }
  }
  function renderJobs() {
    const root = $('hermesJobs'); root.replaceChildren();
    if (!hermes.connected || !authenticated) return;
    const load = element('button', '', 'Load Hermes cron jobs');
    load.onclick = async () => {
      load.disabled = true;
      try {
        const response = await api('hermes/jobs');
        root.replaceChildren(element('h3', '', 'Job subscriptions'));
        if (!response.jobs.length) root.append(element('p', 'muted small', 'No jobs are available on this Hermes connection.'));
        for (const job of response.jobs) {
          const line = element('div', 'hermes-job');
          const label = element('label');
          const check = element('input'); check.type = 'checkbox';
          check.checked = !!sourceKey(hermes.sourceId, 'job', job.id)?.value.enabled;
          label.append(check, element('span', '', job.name));
          const select = element('select'); select.add(new Option('Select channel', ''));
          for (const ch of destinations().filter(c => !c.archived)) select.add(new Option(ch.name, ch.id));
          select.value = sourceKey(hermes.sourceId, 'route', job.id)?.value.channelId || '';
          const setRoute = async () => {
            await edit(hermes.sourceId, 'route', job.id, {channelId: select.value || null});
          };
          select.onchange = setRoute;
          check.onchange = async () => {
            if (check.checked && !select.value) {
              check.checked = false; return toast('Create a channel and choose it before enabling a job.');
            }
            await edit(hermes.sourceId, 'job', job.id, {enabled: check.checked, name: job.name});
          };
          line.append(label, select); root.append(line);
        }
      } catch (error) {toast(error.message); load.disabled = false;}
    };
    root.append(load);
  }
  async function importNative() {
    const file = $('importFile').files[0];
    if (!file) return toast('Choose a Compendium archive JSON file.');
    if (file.size > 256 * 1024 * 1024) return toast('Archive exceeds the 256 MiB PWA import limit.');
    $('importStatus').textContent = 'Checking and importing archive…';
    try {
      await prepareArchive();
      const response = await fetch('/app/api/import', {
        method: 'POST', credentials: 'same-origin',
        headers: {Accept: 'application/json', 'Content-Type': 'application/json'},
        body: file
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Archive import failed');
      $('importStatus').textContent = result.alreadyImported ? 'Already imported, no data changed.' : `${result.imported} records imported without overwriting existing data.`;
      await syncAll(); showSettings();
    } catch (error) { $('importStatus').textContent = error.message; }
  }
  async function prepareArchive() {
    if (!authenticated || !navigator.onLine) throw new Error('Sign in and connect before exporting or importing an archive.');
    await Promise.all([...localSaves]);
    if (storageError) throw new Error('Resolve the local save error first.');
    await flushPending();
    if (pending.size || conflicts.size) throw new Error('Resolve pending sync conflicts before exporting or importing an archive.');
  }
  async function exportArchive() {
    $('exportArchive').disabled = true;
    $('importStatus').textContent = 'Synchronising changes and preparing archive…';
    try {
      await prepareArchive();
      const archive = await api('export');
      downloadBlob(new Blob([JSON.stringify(archive)], {type:'application/json'}), `compendium-archive-${new Date().toISOString().slice(0, 10)}.json`);
      $('importStatus').textContent = 'Archive downloaded. Keep it private: it contains your deliveries and notes.';
    } catch (error) { $('importStatus').textContent = error.message; }
    finally { $('exportArchive').disabled = false; }
  }
  function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = name;
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  init().catch(error => { $('loginMessage').textContent = error.message; });
})();
