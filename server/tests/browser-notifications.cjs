const playwright = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const assert = require('node:assert/strict');
const BASE = process.env.APP01_TEST_BASE;
const PASSWORD = process.env.APP01_TEST_PASSWORD;
const key = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 7)]).toString('base64url');
async function main() {
  const target = new URL(BASE);
  if (process.env.APP01_TEST_DISPOSABLE !== '1' || target.hostname !== '127.0.0.1' || target.protocol !== 'http:') throw Error('disposable loopback server required');
  const browser = await playwright.chromium.launch({headless:true,...(process.env.APP01_TEST_EXECUTABLE ? {executablePath:process.env.APP01_TEST_EXECUTABLE}:{})});
  let subscribed = null, service = 'available', health = 'healthy', pulls = 0, syncs = 0, logoutEndpoint = null;
  try {
    const context = await browser.newContext({serviceWorkers:'block'});
    await context.addInitScript(() => {
      let sub = null;
      const endpoint = 'https://fcm.googleapis.com/fcm/send/test-device';
      const raw = Uint8Array.from([4,...Array(64).fill(7)]);
      const manager = {getSubscription:async()=>sub, subscribe:async options => {
        sub = {endpoint, options, toJSON:()=>({endpoint, keys:{p256dh:btoa(String.fromCharCode(...raw)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''), auth:'AAAAAAAAAAAAAAAAAAAAAA'}}), unsubscribe:async()=>{sub=null;}};
        return sub;
      }};
      const registration = {pushManager:manager};
      Object.defineProperty(window, 'isSecureContext', {value:true});
      Object.defineProperty(window, 'Notification', {value:{get permission(){return localStorage.getItem('__permission') || 'default'},requestPermission:async()=>{window.__requested=(window.__requested||0)+1; return localStorage.getItem('__permission') || 'default'}}});
      Object.defineProperty(navigator, 'serviceWorker', {value:{ready:Promise.resolve(registration),register:async()=>registration,addEventListener:()=>{}}});
      Object.defineProperty(window, 'PushManager', {value:function PushManager(){}});
    });
    await context.route('**/app/api/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const send = value => route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
      if (path.endsWith('/notifications')) return send(service === 'unavailable' ? {available:false,subscribed:false} : {available:true,publicKey:key,subscribed:!!subscribed});
      if (path.endsWith('/notifications/subscribe')) { subscribed = true; return send({subscribed:true}); }
      if (path.endsWith('/notifications/unsubscribe')) { subscribed = false; return send({unsubscribed:true}); }
      if (path.endsWith('/notifications/test')) return send({accepted:true});
      if (path.endsWith('/hermes') && request.method()==='POST') return route.fulfill({status:502,contentType:'application/json',body:'{"error":"Hermes rejected the credentials."}'});
      if (path.endsWith('/hermes')) return send({connected:true,url:'https://hermes.example.test',username:'owner',sourceId:'x',health:{state:health,error:health==='error'?{code:'unavailable',message:'unreachable'}:null}});
      if (path.endsWith('/hermes/check')) { health='healthy'; return send({connected:true,url:'https://hermes.example.test',username:'owner',sourceId:'x',health:{state:'healthy'}}); }
      if (path.endsWith('/logout')) logoutEndpoint=request.postDataJSON().notificationEndpoint;
      if (path.endsWith('/hermes/pull')) { pulls++; return route.fulfill({status:502,contentType:'application/json',body:'{"error":"pull failed"}'}); }
      if (path.endsWith('/sync') && request.method()==='GET') { syncs++; return route.continue(); }
      return route.continue();
    });
    const page = await context.newPage(); page.setDefaultTimeout(5000); await page.goto(BASE + '/app/');
    await page.locator('#username').fill('owner'); await page.locator('#password').fill(PASSWORD); await page.getByRole('button',{name:'Sign in'}).click(); await page.locator('#workspace').waitFor();
    const readyNotifications = () => page.waitForFunction(() => !document.querySelector('#notificationStatus').textContent.includes('Checking'));
    await page.locator('#openSettings').click(); await readyNotifications();
    assert.match(await page.locator('#notificationStatus').innerText(), /off/); assert.equal(await page.evaluate(()=>window.__requested||0), 0);
    await page.locator('#enableNotifications').click(); assert.match(await page.locator('#notificationMessage').innerText(), /not granted/);
    await page.evaluate(()=>localStorage.setItem('__permission','denied')); await page.reload(); await page.locator('#workspace').waitFor(); await page.locator('#openSettings').click(); await readyNotifications(); assert.match(await page.locator('#notificationStatus').innerText(), /blocked/);
    await page.evaluate(()=>localStorage.setItem('__permission','granted')); await page.reload(); await page.locator('#workspace').waitFor(); await page.locator('#openSettings').click(); await readyNotifications();
    await page.locator('#enableNotifications').click(); await page.waitForFunction(()=>document.querySelector('#notificationMessage').textContent.includes('Enabled'));
    await page.locator('#testNotification').click(); await page.waitForFunction(()=>document.querySelector('#notificationMessage').textContent.includes('Test accepted'));
    await page.locator('#disableNotifications').click(); await page.waitForFunction(()=>document.querySelector('#notificationMessage').textContent.includes('disabled'));
    service='unavailable'; await page.reload(); await page.locator('#workspace').waitFor(); await page.locator('#openSettings').click(); await readyNotifications(); assert.match(await page.locator('#notificationStatus').innerText(), /unavailable/);
    health='error'; await page.reload(); await page.locator('#workspace').waitFor(); await page.locator('#openSettings').click(); await page.waitForFunction(()=>document.querySelector('#hermesStatus').textContent.includes('needs attention')); assert.match(await page.locator('#hermesStatus').innerText(), /needs attention/); assert.match(await page.locator('#hermesDetail').innerText(), /Check the Hermes server URL and network connection/); await page.locator('#checkHermes').click();
    await page.locator('#settingsDialog').getByRole('button',{name:'Close settings'}).click();
    const beforeSync = syncs;
    await page.locator('#refresh').click();
    for (let n=0;n<50 && syncs<=beforeSync;n++) await new Promise(resolve=>setTimeout(resolve,100));
    assert.ok(pulls > 0 && syncs > beforeSync, 'Archive sync must still run after Hermes pull fails');
    assert.equal(await page.locator('a[href="https://buymeacoffee.com/muhdusama"]').isVisible(), true);
    assert.equal(await page.locator('a[href="https://buymeacoffee.com/muhdusama"]').innerText(), 'Support the project');
    assert.equal(new URL(await page.locator('a[href="https://buymeacoffee.com/muhdusama"]').getAttribute('href')).href, 'https://buymeacoffee.com/muhdusama');
    for (const [state, label] of [['stale','out of date'],['idle','no jobs enabled']]) {
      health=state; await page.locator('#openSettings').click();
      await page.waitForFunction(label=>document.querySelector('#hermesStatus').textContent.includes(label),label);
      await page.locator('#settingsDialog').getByRole('button',{name:'Close settings'}).click();
    }
    service='available'; await page.locator('#openSettings').click();
    await page.locator('#hermesPass').fill('candidate-password');
    await page.locator('#hermesForm').getByRole('button',{name:'Connect to Hermes'}).click();
    await page.waitForFunction(()=>document.querySelector('#hermesMessage').textContent.includes('settings were not changed'));
    await page.locator('#enableNotifications').click();
    await page.waitForFunction(()=>document.querySelector('#notificationStatus').textContent.includes('enabled on'));
    subscribed=false; // Server-side revocation must be visible without a permission prompt.
    await page.locator('#settingsDialog').getByRole('button',{name:'Close settings'}).click();
    await page.locator('#openSettings').click();
    await page.waitForFunction(()=>document.querySelector('#notificationStatus').textContent.includes('off on'));
    await page.locator('#enableNotifications').click();
    await page.waitForFunction(()=>document.querySelector('#notificationStatus').textContent.includes('enabled on'));
    await page.locator('#logout').click(); await page.locator('#login').waitFor({state:'visible'});
    assert.equal(logoutEndpoint,'https://fcm.googleapis.com/fcm/send/test-device');
    console.log('APP01_NOTIFICATION_BROWSER=PASS permission=explicit controls=PASS health=PASS failed_pull_sync=PASS support_label=PASS logout=PASS');
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1});
