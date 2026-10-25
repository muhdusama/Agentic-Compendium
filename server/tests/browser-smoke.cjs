// Run via python3 scripts/browser-smoke.py, never against a production archive.
// Set PLAYWRIGHT_PACKAGE if Playwright is installed outside node_modules.
const playwright = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const BASE = process.env.APP01_TEST_BASE || 'http://127.0.0.1:8798';
const PASSWORD = process.env.APP01_TEST_PASSWORD;

async function waitForNotes(page, expected) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const matched = await page.evaluate(async expected => {
      const {changes} = await (await fetch('/app/api/sync')).json();
      const notes = new Map(changes.filter(r => r.kind === 'note').map(r => [r.key, r.value.text]));
      return Object.entries(expected).every(([key, text]) => notes.get(key) === text);
    }, expected);
    if (matched) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Expected notes did not reach the server: ' + JSON.stringify(expected));
}

async function main() {
  const target = new URL(BASE);
  if (process.env.APP01_TEST_DISPOSABLE !== '1' || target.hostname !== '127.0.0.1' || target.protocol !== 'http:') {
    throw new Error('Use python3 scripts/browser-smoke.py with a disposable loopback server.');
  }
  if (!PASSWORD) throw new Error('APP01_TEST_PASSWORD is required');
  const browserType = process.env.APP01_TEST_BROWSER || 'chromium';
  const browser = await playwright[browserType].launch({headless: true,
    ...(process.env.APP01_TEST_EXECUTABLE ? {executablePath:process.env.APP01_TEST_EXECUTABLE} : {})});
  const evidence = process.env.APP01_TEST_EVIDENCE || await fs.mkdtemp(path.join(os.tmpdir(), 'compendium-qa-'));
  await fs.mkdir(evidence, {recursive:true});
  const errors = [];
  try {
    const context = await browser.newContext({viewport:{width:1194,height:834}, deviceScaleFactor:2,
      acceptDownloads:true});
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(BASE + '/app/');
    assert.equal(await page.locator('.logomark').first().getAttribute('src'), '/app/icon-192.png');
    await page.locator('#username').fill('owner');
    await page.locator('#password').fill(PASSWORD);
    await page.getByRole('button', {name:'Sign in'}).click();
    await page.locator('#workspace').waitFor({state:'visible'});

    page.once('dialog', async dialog => dialog.accept('Research'));
    await page.locator('#newChannel').click();
    await page.locator('#channels').getByText('Research').waitFor();
    page.once('dialog', async dialog => dialog.accept('Morning Reports'));
    await page.locator('#newSection').click();
    const section = page.locator('.sidebar-section-heading').filter({hasText:'Morning Reports'});
    await section.waitFor();
    await page.locator('.draggable-channel').filter({hasText:'Research'}).dragTo(section);
    await page.waitForFunction(() => {
      const section = [...document.querySelectorAll('.sidebar-section')].find(node => node.textContent.includes('Morning Reports'));
      return !!section?.querySelector('[data-channel-id]');
    });
    // Touch-friendly section relocation is an alternative to drag and drop.
    await page.locator('.draggable-channel').filter({hasText:'Research'}).click();
    await page.locator('.channel-section-select').selectOption('');
    await page.waitForFunction(() => {
      const group = [...document.querySelectorAll('.sidebar-section')].find(node => node.querySelector('.section-toggle')?.textContent === 'Unsectioned');
      return group?.querySelector('.draggable-channel')?.textContent.includes('Research');
    });
    await page.locator('.channel-section-select').selectOption({label:'Morning Reports'});
    await page.waitForFunction(() => {
      const group = [...document.querySelectorAll('.sidebar-section')].find(node => node.querySelector('.section-toggle')?.textContent === 'Morning Reports');
      return group?.querySelector('.draggable-channel')?.textContent.includes('Research');
    });
    page.once('dialog', async dialog => dialog.accept('Operations'));
    await page.locator('#newChannel').click();
    await page.locator('.draggable-channel').filter({hasText:'Operations'}).waitFor();
    await page.locator('.draggable-channel').filter({hasText:'Operations'})
      .dragTo(page.locator('.draggable-channel').filter({hasText:'Research'}));
    await page.waitForFunction(() => {
      const group = [...document.querySelectorAll('.sidebar-section')].find(node => node.querySelector('.section-toggle')?.textContent === 'Morning Reports');
      return group?.querySelectorAll('.draggable-channel').length === 2
        && group.querySelector('.draggable-channel')?.textContent.includes('Operations');
    });
    await page.locator('.draggable-channel').filter({hasText:'Operations'}).click();
    await page.locator('.channel-options').getByRole('button', {name:'Move down'}).click();
    await page.waitForFunction(() => {
      const group = [...document.querySelectorAll('.sidebar-section')].find(node => node.querySelector('.section-toggle')?.textContent === 'Morning Reports');
      return group?.querySelector('.draggable-channel')?.textContent.includes('Research');
    });
    await page.locator('#channels').getByText('All deliveries').click();
    await page.evaluate(async () => {
      for (let n = 1; n <= 3; n++) {
        const record = {id:`smoke-${n}`,title:`Briefing ${n}`,publisher:'Hermes',section:'Inbox',
          summary:'Rich reading and organisation',
          body:n === 1 ? '## Formatted result\n\nThis is **important** and *emphasised*.\n\n| Field | Value |\n| --- | --- |\n| Count | 3 |\n\n- First\n- Second\n\n[Reference](https://example.org)\n\n<img src=x onerror="window.injected=true">'
            : '## More updates\n\n- A result\n- Another result',
          deliveredAt:new Date(Date.now() - n * 60_000).toISOString(),needsAttention:false,hermesJobID:'daily'};
        const response = await fetch('/app/api/state', {method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({sourceId:'smoke-source',kind:'delivery',key:record.id,
            baseVersion:0,deleted:false,value:record})});
        if (!response.ok && response.status !== 409) throw new Error(await response.text());
      }
      const response = await fetch('/app/api/state', {method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sourceId:'smoke-source',kind:'note',key:'smoke-1',
          baseVersion:0,deleted:false,value:{text:'', drawingData:'cHJlc2VydmU=',
            drawingPreview:'iVBORw0KGgo=',webStrokes:[{kind:'pen',points:[{x:.1,y:.1,p:.5}]}]}})});
      if (!response.ok && response.status !== 409) throw new Error(await response.text());
      const changes = await (await fetch('/app/api/sync?cursor=0')).json();
      const channel = changes.changes.find(record => record.kind === 'channel' && record.value.name === 'Research');
      if (!channel) throw new Error('Research channel was not created');
      const route = await fetch('/app/api/state', {method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sourceId:'smoke-source',kind:'route',key:'daily',baseVersion:0,
          deleted:false,value:{channelId:channel.key}})});
      if (!route.ok && route.status !== 409) throw new Error(await route.text());
    });
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 3, null, {timeout:10000}).catch(async error => {
      console.error('BROWSER STATE', {
        status: await page.locator('#connectivity').innerText(),
        visibleDeliveries: await page.locator('.delivery-card').count(),
        listTitle: await page.locator('#listTitle').innerText(),
        errorMessages: errors
      });
      throw error;
    });

    // Select all visible, bulk move, bulk read/unread, then Bin and Restore.
    await page.locator('#selectMode').click();
    await page.locator('#selectAll').click();
    assert.match(await page.locator('#selectedCount').innerText(), /3 selected/);
    await page.locator('#bulkChannel').selectOption({label:'Research'});
    await page.locator('#bulkMove').click();
    await page.waitForFunction(() => document.querySelector('#selectMode')?.getAttribute('aria-pressed') === 'false');
    await page.locator('#channels').getByText('Research').click();
    assert.equal(await page.locator('.delivery-card').count(), 3);
    // A deliberate Unfiled move overrides automatic job routing, rather than
    // falling back to its default route. Re-filing reverses that local choice.
    await page.locator('.delivery-card').first().click();
    await page.locator('#readerChannel').selectOption('');
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 2);
    await page.locator('#readerChannel').selectOption({label:'Research'});
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 3);
    await page.locator('#channels').getByText('Research').click();
    await page.locator('#selectMode').click();
    await page.locator('#selectAll').click();
    await page.locator('#bulkRead').click();
    await page.waitForFunction(() => document.querySelector('#selectMode')?.getAttribute('aria-pressed') === 'false');
    await page.locator('#selectMode').click();
    await page.locator('#selectAll').click();
    await page.locator('#bulkUnread').click();
    await page.waitForFunction(() => document.querySelector('#selectMode')?.getAttribute('aria-pressed') === 'false');
    await page.locator('#selectMode').click();
    await page.locator('.delivery-card').first().click();
    assert.match(await page.locator('#selectedCount').innerText(), /1 selected/);
    page.once('dialog', async dialog => dialog.accept());
    await page.locator('#bulkDelete').click();
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 2);
    assert.equal(await page.locator('.delivery-card').count(), 2);
    await page.locator('#channels').getByText('Bin', {exact:true}).click();
    assert.equal(await page.locator('.delivery-card').count(), 1);
    await page.locator('#selectMode').click();
    await page.locator('#selectAll').click();
    await page.locator('#bulkRestore').click();
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 0);
    assert.match(await page.locator('#listTitle').innerText(), /Bin/);
    assert.equal(await page.locator('.delivery-card').count(), 0);

    await page.locator('#channels').getByText('All deliveries').click();
    await page.waitForFunction(() => document.querySelectorAll('.delivery-card').length === 3);
    await page.locator('.delivery-card').first().click();
    await page.locator('#readerContent').waitFor({state:'visible'});
    assert.equal(await page.locator('#readerBody h2').first().innerText(), 'Formatted result');
    assert.equal(await page.locator('#readerBody strong').first().innerText(), 'important');
    assert.equal(await page.locator('#readerBody table tbody tr').count(), 1);
    assert.equal(await page.locator('#readerBody a').count(), 1);
    assert.equal(await page.locator('#readerBody img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.locator('#drawingCanvas').count(), 0);
    const download = page.waitForEvent('download');
    await page.locator('#downloadRTF').click();
    const rtf = await download;
    assert.ok(rtf.suggestedFilename().endsWith('.rtf'));
    const rtfContent = await fs.readFile(await rtf.path(), 'utf8');
    assert.ok(rtfContent.startsWith('{\\rtf1'));
    assert.match(rtfContent, /\\b important/);

    await page.locator('#noteText').fill('Typed notes remain editable');
    await page.waitForTimeout(650);
    const preserved = await page.evaluate(async () => {
      const {changes} = await (await fetch('/app/api/sync?cursor=0')).json();
      return changes.filter(r => r.kind === 'note' && r.key === 'smoke-1').at(-1)?.value;
    });
    assert.equal(preserved?.text, 'Typed notes remain editable');
    assert.equal(preserved?.drawingData, 'cHJlc2VydmU=');
    assert.equal(preserved?.webStrokes.length, 1);

    // Switching notes faster than the network debounce must not lose either draft.
    await page.evaluate(() => {
      const cards = document.querySelectorAll('.delivery-card');
      const input = document.querySelector('#noteText');
      cards[0].click(); input.value = 'First rapid note'; input.dispatchEvent(new Event('input', {bubbles:true}));
      cards[1].click(); input.value = 'Second rapid note'; input.dispatchEvent(new Event('input', {bubbles:true}));
    });
    await waitForNotes(page, {'smoke-1':'First rapid note', 'smoke-2':'Second rapid note'});
    await page.locator('.delivery-card').first().click();
    await page.locator('#noteText').fill('Reload-safe draft');
    await page.waitForFunction(() => document.querySelector('#noteStatus').textContent.startsWith('Saved'));
    await page.reload();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('.delivery-card').first().click();
    assert.equal(await page.locator('#noteText').inputValue(), 'Reload-safe draft');

    // Theme and layout on desktop, iPad portrait, expanded Duo and compact iPhone.
    await page.locator('#openSettings').click();
    await page.locator('#themeMode').selectOption('dark');
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    await page.locator('#accentColor').selectOption('rose');
    await page.waitForFunction(() => document.documentElement.dataset.accent === 'rose');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    assert.equal(await page.locator('html').getAttribute('data-accent'), 'rose');
    await page.locator('#settingsDialog').getByRole('button',{name:'Close'}).click();
    await page.locator('#toggleChannels').click();
    assert.equal(await page.locator('.channels').isVisible(), false);
    await page.locator('#toggleChannels').click();
    assert.equal(await page.locator('.channels').isVisible(), true);
    await page.locator('#toggleList').click();
    assert.equal(await page.locator('.deliveries').isVisible(), false);
    await page.locator('#toggleList').click();
    assert.equal(await page.locator('.deliveries').isVisible(), true);
    await page.screenshot({path:path.join(evidence,'desktop-dark.png')});
    for (const [width,height] of [[834,1112],[951,760],[669,880],[390,844],[360,780]]) {
      await page.setViewportSize({width,height});
      if (width <= 760) {
        assert.equal(await page.locator('.mobile-tabs').isVisible(), true);
        assert.equal(await page.locator('#reader').isVisible(), true);
      }
      const dims = await page.evaluate(() => ({page:document.body.scrollWidth, screen:innerWidth}));
      assert.ok(dims.page <= dims.screen + 2, `horizontal overflow at ${width}: ${JSON.stringify(dims)}`);
    }
    await page.screenshot({path:path.join(evidence,'iphone-dark.png')});

    // Offline typed note survives reconnection and server revision updates.
    await context.setOffline(true);
    await page.locator('#noteText').fill('Offline note synchronised');
    await page.waitForFunction(() => document.querySelector('#noteStatus').textContent.includes('waiting to sync'));
    assert.match(await page.locator('#connectivity').innerText(), /Offline.*pending/);
    await context.setOffline(false);
    await page.locator('#refresh').click();
    await waitForNotes(page, {'smoke-1':'Offline note synchronised'});
    await page.reload();
    await page.locator('#workspace').waitFor({state:'visible'});
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    assert.equal(await page.locator('html').getAttribute('data-accent'), 'rose');

    const other = await browser.newContext({viewport:{width:834,height:1112}});
    const otherPage = await other.newPage();
    await otherPage.goto(BASE + '/app/');
    await otherPage.locator('#username').fill('owner');
    await otherPage.locator('#password').fill(PASSWORD);
    await otherPage.getByRole('button',{name:'Sign in'}).click();
    await otherPage.locator('#workspace').waitFor({state:'visible'});
    await otherPage.waitForFunction(() => document.documentElement.dataset.theme === 'dark'
      && document.documentElement.dataset.accent === 'rose');
    assert.equal(await otherPage.locator('html').getAttribute('data-theme'), 'dark');
    assert.equal(await otherPage.locator('html').getAttribute('data-accent'), 'rose');

    // Concurrent note edits retain a draft and let the owner accept the server.
    await page.setViewportSize({width:1194,height:834});
    await page.locator('.delivery-card').first().click();
    await context.setOffline(true);
    await page.locator('#noteText').fill('Conflicting local draft');
    await page.waitForFunction(() => document.querySelector('#noteStatus').textContent.includes('waiting to sync'));
    await otherPage.locator('.delivery-card').first().click();
    await otherPage.locator('#noteText').fill('Synced version from other device');
    await waitForNotes(otherPage, {'smoke-1':'Synced version from other device'});
    await context.setOffline(false);
    await page.locator('#refresh').click();
    await page.locator('#reviewConflicts').waitFor({state:'visible',timeout:10000}).catch(async error => {
      console.error('CONFLICT DIAGNOSTICS', JSON.stringify(await page.evaluate(async () => {
        const {changes} = await (await fetch('/app/api/sync')).json();
        const request = indexedDB.open('app01-offline-v1', 1);
        const db = await new Promise(resolve => { request.onsuccess = () => resolve(request.result); });
        const read = name => new Promise(resolve => {const r=db.transaction(name).objectStore(name).getAll();r.onsuccess=()=>resolve(r.result);});
        return {status:document.querySelector('#connectivity').textContent, title:document.querySelector('#readerTitle').textContent,
          notes:changes.filter(r=>r.kind==='note'),pending:await read('pending'), conflicts:await read('conflicts')};
      }), null, 2));
      throw error;
    });
    await page.setViewportSize({width:360,height:780});
    assert.ok(await page.evaluate(() => document.body.scrollWidth <= innerWidth + 2), 'Conflict notice must fit on a phone');
    await page.screenshot({path:path.join(evidence,'phone-conflict.png')});
    await page.locator('#reviewConflicts').click();
    assert.equal(await page.locator('#localConflict').innerText(), 'Conflicting local draft');
    assert.equal(await page.locator('#remoteConflict').innerText(), 'Synced version from other device');
    await page.locator('#keepRemote').click();
    await page.locator('#conflictDialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#noteText').inputValue(), 'Synced version from other device');
    await page.setViewportSize({width:1194,height:834});

    // Export and re-import through the actual controls without changing records.
    await page.locator('#openSettings').click();
    const archiveDownload = page.waitForEvent('download');
    await page.locator('#exportArchive').click();
    const archive = await archiveDownload;
    const archivePath = path.join(evidence, 'fixture-archive.json');
    await archive.saveAs(archivePath);
    const contents = JSON.parse(await fs.readFile(archivePath, 'utf8'));
    assert.equal(contents.format, 'agentic-compendium-pwa');
    assert.ok(contents.records.some(r => r.kind === 'note' && r.value.drawingData === 'cHJlc2VydmU='));
    assert.ok(!JSON.stringify(contents).includes(PASSWORD));
    await page.locator('#importFile').setInputFiles(archivePath);
    await page.locator('#importButton').click();
    await page.waitForFunction(() => /0 records imported|Already imported/.test(document.querySelector('#importStatus').textContent));
    await page.locator('#settingsDialog').getByRole('button',{name:'Close'}).click();

    // Owner updates both fields. All old sessions become invalid.
    await page.locator('#openSettings').click();
    await page.locator('#accountUsername').fill('updated.owner');
    await page.locator('#accountCurrentPassword').fill(PASSWORD);
    await page.locator('#accountNewPassword').fill('new-owner-password-long-12345');
    await page.locator('#accountConfirmPassword').fill('new-owner-password-long-12345');
    await page.locator('#accountForm').getByRole('button',{name:'Update account'}).click();
    await page.locator('#login').waitFor({state:'visible'});
    await otherPage.reload();
    await otherPage.locator('#login').waitFor({state:'visible'});
    // A reverse proxy's HTML error page becomes a readable message, not a JSON parse error.
    await page.route('**/app/api/login', route => route.fulfill({status:502, contentType:'text/html', body:'<html>Bad gateway</html>'}));
    await page.locator('#username').fill('updated.owner');
    await page.locator('#password').fill('new-owner-password-long-12345');
    await page.getByRole('button',{name:'Sign in'}).click();
    await page.waitForFunction(() => /HTTP 502/.test(document.querySelector('#loginMessage').textContent));
    await page.unroute('**/app/api/login');
    await page.locator('#username').fill('updated.owner');
    await page.locator('#password').fill('new-owner-password-long-12345');
    await page.getByRole('button',{name:'Sign in'}).click();
    await page.locator('#workspace').waitFor({state:'visible'});

    // Forgetting the device revokes the session and empties the offline copy.
    const offlineCount = () => page.evaluate(() => new Promise((resolve, reject) => {
      const req = indexedDB.open('app01-offline-v1', 1);
      req.onsuccess = () => {
        const db = req.result, tx = db.transaction(['records', 'published'], 'readonly');
        let total = 0;
        for (const name of ['records', 'published']) tx.objectStore(name).count().onsuccess = event => { total += event.target.result; };
        tx.oncomplete = () => { db.close(); resolve(total); };
      };
      req.onerror = () => reject(req.error);
    }));
    assert.ok(await offlineCount() > 0, 'Fixture data must be cached before forgetting the device');
    await page.locator('#openSettings').click();
    page.once('dialog', dialog => dialog.accept());
    await Promise.all([page.waitForEvent('load'), page.locator('#forgetDevice').click()]);
    await page.locator('#login').waitFor({state:'visible'});
    assert.equal(await offlineCount(), 0);
    assert.equal(await page.evaluate(async () => (await fetch('/app/api/session')).status), 401);

    assert.deepEqual(errors, []);
    console.log(`APP01_BROWSER_SMOKE=PASS browser=${browserType} bulk=PASS themes=PASS rich_text=PASS rapid_notes=PASS conflicts=PASS archive=PASS offline=PASS responsive=iPad,Duo,iPhone account=PASS proxy_error=PASS forget_device=PASS evidence=${evidence}`);
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error);process.exitCode=1;});
