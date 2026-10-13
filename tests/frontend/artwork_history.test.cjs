const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
let browser, fixtures;
before(async () => {
  fixtures = fs.mkdtempSync(path.join(os.tmpdir(), 'pmxbot-browser-'));
  execFileSync(process.env.PYTHON || 'python', ['tests/frontend/render_fixtures.py', fixtures], {env: {...process.env, PYTHONPATH: '.'}});
  browser = await chromium.launch();
});
after(async () => {
  if (browser) await browser.close();
  fs.rmSync(fixtures, {recursive: true, force: true});
});
async function setup(options = {}) {
  const page = await browser.newPage({viewport: {width: options.width || 900, height: 700}, reducedMotion: 'reduce', javaScriptEnabled: options.javascript !== false});
  await page.route('https://art.example/**', route => route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="blue"/></svg>'}));
  await page.route('https://albums.example/**', route => route.abort());
  await page.route('http://album.test/**', async route => {
    const request = route.request();
    const id = new URL(request.url()).searchParams.get('source_image_id') || '42';
    const suffix = options.signed ? '-signed' : '';
    if (request.method() === 'POST' && options.handle) { await options.handle(route, id); return; }
    if (request.headers().accept === 'application/json') {
      if (options.handle && await options.handle(route, id)) return;
      await route.fulfill({contentType: 'application/json', body: fs.readFileSync(path.join(fixtures, id + suffix + '.json'), 'utf8')});
    } else {
      await route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(fixtures, options.fixture ? options.fixture + '.html' : id + suffix + '.html'), 'utf8')});
    }
  });
  await page.goto('http://album.test/bot/albums/42?source_image_id=42');
  return page;
}
const thumbnail = (page, id) => page.locator(`[data-image-id="${id}"]`);
async function selected(page, id) {
  await page.waitForFunction(id => document.querySelector('[name=source_image_id]').value === String(id) && document.querySelector('#image-history').getAttribute('aria-busy') === 'false', id);
  assert.equal(await page.locator('#image-history [aria-current]').getAttribute('data-image-id'), String(id));
  assert.match(await page.locator('#selected-metadata').innerText(), new RegExp(String(id)));
}
test('selection updates all data, retains form and per-image edits, restores history without reload', async () => {
  const page = await setup();
  await page.evaluate(() => { window.documentMarker = 1; window.originalForm = document.querySelector('form#variation-form'); });
  await page.locator('[name=description]').fill('unsaved draft');
  await page.evaluate(() => window.scrollTo(0, 300));
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await thumbnail(page, 43).evaluate(link => link.click());
  await selected(page, 43);
  assert.equal(await page.evaluate(() => window.scrollY), scrollBefore);
  assert.equal(await page.locator('#selected-prompt').innerText(), 'Prompt #43');
  assert.equal(await page.locator('.artwork-preview').getAttribute('href'), 'https://art.example/43');
  assert.match(await page.locator('#selected-metadata').innerText(), /Metadata #43/);
  assert.equal(await page.locator('[name=description]').inputValue(), '');
  assert.equal(await page.evaluate(() => window.originalForm === document.querySelector('#variation-form')), true);
  assert.equal(await page.evaluate(() => window.documentMarker), 1);
  assert.match(page.url(), /source_image_id=43/);
  await page.goBack(); await selected(page, 42);
  assert.equal(await page.locator('[name=description]').inputValue(), 'unsaved draft');
  await page.goForward(); await selected(page, 43);
  await page.reload();
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '43');
  await thumbnail(page, 44).click(); await selected(page, 44);
  assert.equal(await page.locator('.artwork-preview').count(), 0);
  assert.match(await page.locator('#selected-artwork').innerText(), /unavailable/);
  await thumbnail(page, 43).click(); await selected(page, 43);
  await page.locator('.artwork-preview').click();
  assert.equal(await page.locator('dialog').evaluate(dialog => dialog.open), true);
  await page.keyboard.press('Escape');
  await page.close();
});
for (const width of [360, 1400]) test(`single row, keyboard focus, overflow boundaries at ${width}px`, async () => {
  const page = await setup({width});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const rows = await page.locator('#image-history > li').evaluateAll(items => new Set(items.map(item => item.offsetTop)).size);
  assert.equal(rows, 1);
  const initialScroll = await page.locator('#image-history').evaluate(el => el.scrollLeft);
  await page.locator('#history-left').click();
  assert.ok(await page.locator('#image-history').evaluate(el => el.scrollLeft) < initialScroll);
  await page.locator('#image-history').focus();
  await page.keyboard.press('Home');
  await page.waitForFunction(() => document.querySelector('#history-left').disabled);
  await page.keyboard.press('End');
  await page.waitForFunction(() => document.querySelector('#history-right').disabled);
  assert.equal(await page.locator(':focus').getAttribute('data-image-id'), '42');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '42');
  await page.keyboard.press('Home');
  await page.waitForFunction(() => document.querySelector('#history-left').disabled);
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator(':focus').getAttribute('data-image-id'), '61');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '42');
  await page.keyboard.press(' '); await selected(page, 61);
  await page.locator('[name=description]').focus();
  await page.keyboard.press('Home');
  assert.equal(await page.locator(':focus').getAttribute('name'), 'description');
  assert.equal(await page.locator('#image-history').evaluate(el => getComputedStyle(el).scrollBehavior), 'auto');
  await page.close();
});
test('rapid responses and errors keep a consistent selection and allow retry', async () => {
  let fail = true;
  const page = await setup({handle: async (route, id) => {
    if (id === '43') await new Promise(resolve => setTimeout(resolve, 150));
    if (id === '44' && fail) { await route.fulfill({status: 503, body: 'unavailable'}); return true; }
    return false;
  }});
  await page.evaluate(() => { const fetchOriginal = window.fetch; window.fetch = (url, options) => fetchOriginal(url, {...options, signal: undefined}); });
  await thumbnail(page, 43).click();
  await thumbnail(page, 45).click(); await selected(page, 45);
  await page.waitForTimeout(250);
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '45');
  await thumbnail(page, 44).click();
  await page.locator('#history-retry').waitFor({state: 'visible'});
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '45');
  assert.equal(await page.locator('.artwork-preview').getAttribute('href'), 'https://art.example/45');
  assert.match(page.url(), /source_image_id=45/);
  fail = false;
  await page.locator('#history-retry').click(); await selected(page, 44);
  await page.close();
});
test('fallback links and modified clicks keep normal browser behavior', async () => {
  const page = await setup({javascript: false});
  assert.equal(await thumbnail(page, 43).getAttribute('href'), '/bot/albums/42?source_image_id=43');
  await thumbnail(page, 43).click();
  assert.match(page.url(), /source_image_id=43/);
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '43');
  await page.close();
  const enhanced = await setup();
  await thumbnail(enhanced, 43).click({modifiers: ['Shift']});
  assert.equal(await enhanced.locator('[name=source_image_id]').inputValue(), '42');
  await enhanced.close();
});

for (const fixture of ['one', 'empty']) test(`${fixture} history has usable boundaries`, async () => {
  const page = await setup({fixture});
  if (fixture === 'one') {
    assert.equal(await page.locator('#history-left').isDisabled(), true);
    assert.equal(await page.locator('#history-right').isDisabled(), true);
    await thumbnail(page, 42).focus();
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.locator(':focus').getAttribute('data-image-id'), '42');
  } else {
    assert.equal(await page.locator('#variation-form').count(), 1);
    assert.equal(await page.getByRole('button', {name: 'Create New', exact: true}).isDisabled(), true);
    assert.equal(await page.getByRole('button', {name: 'Edit', exact: true}).isDisabled(), true);
    assert.match(await page.locator('#selected-prompt').innerText(), /No artwork/);
  }
  await page.close();
});
for (const operation of ['edit', 'create_new']) test(`selection disables both actions while loading and submits ${operation} with the selected draft`, async () => {
  let submitted;
  const page = await setup({signed: true, handle: async (route, id) => {
    if (route.request().method() === 'POST') {
      submitted = new URLSearchParams(route.request().postData());
      await route.fulfill({status: 400, contentType: 'application/json', body: '{"error":"Test rejection"}'});
      return true;
    }
    if (id === '43') await new Promise(resolve => setTimeout(resolve, 200));
    return false;
  }});
  await page.locator('[name=description]').fill('draft for 42');
  await thumbnail(page, 43).click();
  for (const name of ['Create New', 'Edit']) assert.equal(await page.getByRole('button', {name, exact: true}).isDisabled(), true);
  await selected(page, 43);
  await page.locator('[name=description]').fill('draft for 43');
  await page.getByRole('button', {name: operation === 'edit' ? 'Edit' : 'Create New', exact: true}).click();
  await page.locator('#generation-error').waitFor({state: 'visible'});
  assert.equal(submitted.get('operation'), operation);
  assert.match(submitted.get('submission_token'), /^[0-9a-f]{32}$/);
  assert.equal(submitted.get('source_image_id'), '43');
  assert.equal(submitted.get('description'), 'draft for 43');
  await thumbnail(page, 42).click(); await selected(page, 42);
  assert.equal(await page.locator('[name=description]').inputValue(), 'draft for 42');
  await page.close();
});

test('accepted requests disable both buttons, then allow intentional repeated Create New with a fresh token', async () => {
  const submissions = [];
  const page = await setup({signed: true, handle: async route => {
    if (route.request().method() === 'POST') {
      submissions.push(new URLSearchParams(route.request().postData()));
      await new Promise(resolve => setTimeout(resolve, 100));
      await route.fulfill({status: 202, contentType: 'application/json', body: JSON.stringify({
        id: 'job', status_url: '/bot/jobs/job', submission_token: 'a'.repeat(32)
      })});
      return true;
    }
    if (route.request().url().includes('/jobs/')) {
      await route.fulfill({contentType: 'application/json', body: JSON.stringify({status: 'failed', error: 'Test failure'})});
      return true;
    }
    return false;
  }});
  const create = page.getByRole('button', {name: 'Create New', exact: true});
  await create.click();
  assert.equal(await create.isDisabled(), true);
  assert.equal(await page.getByRole('button', {name: 'Edit', exact: true}).isDisabled(), true);
  await page.locator('#generation-error').waitFor({state: 'visible'});
  await create.click();
  await page.locator('#generation-error').waitFor({state: 'visible'});
  assert.equal(submissions.length, 2);
  assert.notEqual(submissions[0].get('submission_token'), submissions[1].get('submission_token'));
  assert.equal(submissions[1].get('submission_token'), 'a'.repeat(32));
  assert.equal(submissions[0].get('description'), submissions[1].get('description'));
  assert.equal(submissions[1].get('operation'), 'create_new');
  await page.close();
});
