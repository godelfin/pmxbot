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
  const page = await browser.newPage({viewport: {width: options.width || 900, height: 700}, hasTouch: options.touch || false, reducedMotion: 'reduce', javaScriptEnabled: options.javascript !== false});
  await page.route('https://art.example/**', route => route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="blue"/></svg>'}));
  await page.route('https://albums.example/**', route => route.abort());
  await page.route('http://album.test/**', async route => {
    const request = route.request();
    const id = new URL(request.url()).searchParams.get('source_image_id') || '42';
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/bot/pmxbot.png') {
      await route.fulfill({contentType: 'image/png', body: fs.readFileSync('pmxbot/web/templates/pmxbot.png')});
      return;
    }
    if (pathname === '/bot/gallery' || pathname === '/bot/') {
      await route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(fixtures, pathname === '/bot/gallery' ? 'gallery.html' : 'catalog.html'), 'utf8')});
      return;
    }
    const suffix = options.signed ? '-signed' : '';
    if (request.method() === 'POST' && options.handle) { await options.handle(route, id); return; }
    if (request.headers().accept === 'application/json') {
      if (options.handle && await options.handle(route, id)) return;
      await route.fulfill({contentType: 'application/json', body: fs.readFileSync(path.join(fixtures, id + suffix + '.json'), 'utf8')});
    } else {
      const album = new URL(request.url()).pathname.match(/\/albums\/(41|43)$/);
      const filename = album ? 'album' + album[1] + '.html' : options.fixture ? options.fixture + '.html' : id + suffix + '.html';
      await route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(fixtures, filename), 'utf8')});
    }
  });
  await page.goto('http://album.test/bot/albums/42?source_image_id=42');
  return page;
}
const thumbnail = (page, id) => page.locator(`[data-image-id="${id}"]`);
test('share icon copies a clean selected-image permalink and survives artwork changes', async () => {
  const page = await setup({width: 1400});
  await page.goto('http://album.test/bot/albums/42?source_image_id=42&return_to=%2Fbot%2Fgallery%3Fpage%3D2#scroll=500');
  await page.evaluate(() => {
    window.copiedLinks = [];
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => window.copiedLinks.push(text)}, configurable: true});
  });
  const button = page.getByRole('button', {name: 'Copy link to selected image'});
  await page.mouse.move(0, 0);
  assert.equal(await button.evaluate(el => getComputedStyle(el).opacity), '0');
  await page.locator('.artwork-frame').hover();
  assert.equal(await button.evaluate(el => getComputedStyle(el).opacity), '1');
  await button.click();
  assert.equal(await page.locator('.artwork-share-status').innerText(), 'Image link copied.');
  assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
  await thumbnail(page, 43).click(); await selected(page, 43);
  await button.focus();
  assert.equal(await button.evaluate(el => getComputedStyle(el).opacity), '1');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => window.copiedLinks.length === 2);
  assert.deepEqual(await page.evaluate(() => window.copiedLinks), [
    'http://album.test/bot/albums/42?source_image_id=42',
    'http://album.test/bot/albums/42?source_image_id=43',
  ]);
  await thumbnail(page, 44).click(); await selected(page, 44);
  assert.equal(await button.count(), 0);
  await page.close();
});
test('share is visible on touch screens and clipboard failure offers a selectable link', async () => {
  const page = await setup({width: 320, touch: true});
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw new Error('denied');}}, configurable: true}));
  const button = page.getByRole('button', {name: 'Copy link to selected image'});
  assert.equal(await button.evaluate(el => getComputedStyle(el).opacity), '1');
  await button.click();
  const input = page.getByLabel('Copy this link:');
  assert.equal(await input.inputValue(), 'http://album.test/bot/albums/42?source_image_id=42');
  assert.equal(await input.evaluate(el => document.activeElement === el && el.selectionEnd === el.value.length && el.selectionStart === 0), true);
  assert.equal(await button.isEnabled(), true);
  assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.close();
  const noJS = await setup({javascript: false});
  assert.equal(await noJS.locator('.artwork-share').isVisible(), false);
  await noJS.close();
});
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
  assert.match(await page.locator('#selected-metadata').textContent(), /Metadata #43/);
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
  await selected(page, 61);
  await page.keyboard.press('ArrowLeft'); await selected(page, 62);
  await page.keyboard.press('ArrowRight'); await selected(page, 61);
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

test('arrows select artwork from page focus and scroll controls without overriding editing or dialogs', async () => {
  const page = await setup();
  await page.keyboard.press('ArrowLeft'); await selected(page, 43);
  await page.keyboard.press('ArrowLeft'); await selected(page, 44);
  await page.keyboard.press('ArrowRight'); await selected(page, 43);
  await page.locator('#history-left').click();
  await page.keyboard.press('ArrowLeft'); await selected(page, 44);
  await page.locator('[name=description]').fill('keep this draft');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '44');
  await page.locator('[name=genre]').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '44');
  await thumbnail(page, 43).click(); await selected(page, 43);
  await page.locator('.artwork-preview').click();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '43');
  await page.keyboard.press('Escape');
  await page.keyboard.press('ArrowRight'); await selected(page, 42);
  await thumbnail(page, 43).click(); await selected(page, 43);
  await thumbnail(page, 43).focus();
  await page.keyboard.press('Control+ArrowRight');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '43');
  await page.keyboard.press('End');
  assert.equal(await page.locator(':focus').getAttribute('data-image-id'), '42');
  await page.keyboard.press('Enter'); await selected(page, 42);
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '42');
  await page.close();
});

test('Shift+arrows navigate albums on page and navigation-button focus, including empty albums and boundaries', async () => {
  const page = await setup();
  const previous = page.locator('.album-navigation a[rel=prev]');
  assert.equal(await previous.getAttribute('aria-keyshortcuts'), 'Shift+ArrowLeft');
  assert.equal(await previous.locator('kbd').innerText(), 'Shift+←');
  await previous.focus();
  await page.keyboard.press('Shift+ArrowLeft');
  await page.waitForURL('**/albums/43');
  assert.equal(await page.locator('#image-history a[data-image-id]').count(), 0);
  await page.keyboard.press('Shift+ArrowLeft');
  assert.match(page.url(), /albums\/43$/);
  await page.keyboard.press('Shift+ArrowRight');
  await page.waitForURL('**/albums/42');
  await page.keyboard.press('Shift+ArrowRight');
  await page.waitForURL('**/albums/41');
  await page.keyboard.press('Shift+ArrowRight');
  assert.match(page.url(), /albums\/41$/);
  await page.keyboard.press('Shift+ArrowLeft');
  await page.waitForURL('**/albums/42');
  await page.close();
});

test('album shortcuts leave form fields, modified shortcuts and the full-size dialog alone', async () => {
  const page = await setup();
  const initialURL = page.url();
  for (const selector of ['[name=description]', '[name=genre]']) {
    await page.locator(selector).focus();
    await page.keyboard.press('Shift+ArrowLeft');
    await page.keyboard.press('Shift+ArrowRight');
    assert.equal(page.url(), initialURL);
  }
  await page.locator('.artwork-preview').click();
  await page.keyboard.press('Shift+ArrowRight');
  assert.equal(page.url(), initialURL);
  await page.keyboard.press('Escape');
  for (const modifier of ['Control', 'Meta', 'Alt']) {
    await page.keyboard.press(modifier + '+Shift+ArrowRight');
    assert.equal(page.url(), initialURL);
  }
  await page.close();
});

test('history labels, selection indicator, and keyboard help stay clear without overriding Enter/Space', async () => {
  const page = await setup();
  const history = page.locator('section[aria-labelledby="image-history-heading"]');
  assert.equal(await history.locator('h2').innerText(), 'Artwork history');
  const help = history.locator('.keyboard-help');
  assert.equal(await help.evaluate(el => el.open), false);
  await help.locator('summary').click();
  assert.equal(await help.locator('#history-help').isVisible(), true);
  assert.match(await help.locator('#history-help').innerText(), /Left\/Right/);
  await help.locator('summary').click();
  assert.equal(await thumbnail(page, 43).locator('.history-label').innerText(), 'Original');
  assert.equal(await thumbnail(page, 44).locator('.history-label').innerText(), 'Variation 1');
  assert.equal(await thumbnail(page, 42).locator('.history-label').innerText(), 'Image 1');
  assert.equal(await thumbnail(page, 42).locator('.selection-indicator').isVisible(), true);
  await thumbnail(page, 44).click(); await selected(page, 44);
  assert.equal(await thumbnail(page, 44).locator('.selection-indicator').isVisible(), true);
  assert.equal(await thumbnail(page, 42).locator('.selection-indicator').isVisible(), false);
  assert.match(await thumbnail(page, 44).getAttribute('aria-label'), /Variation 1, based on Original.*Selected/);
  await thumbnail(page, 42).click(); await selected(page, 42);
  await page.locator('#image-history').focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press(' ');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '42');
  await page.keyboard.press('Home');
  await page.keyboard.press(' ');
  assert.equal(await page.locator('[name=source_image_id]').inputValue(), '42');
  await page.close();
});

test('advanced form settings retain values and expansion across artwork selection', async () => {
  const page = await setup({signed: true});
  const form = page.locator('#variation-form');
  const advanced = form.locator('#variation-advanced');
  assert.equal(await advanced.evaluate(el => el.open), false);
  assert.equal(await form.locator('[name=description]').isVisible(), true);
  assert.equal(await form.locator('[name=genre]').isVisible(), true);
  assert.equal(await form.locator('[name=artist_description]').isVisible(), false);
  await form.locator('[name=description]').fill('album cue');
  await form.getByLabel('Style', {exact: true}).selectOption('001');
  assert.equal(await form.locator('[name=style] optgroup').count(), 12);
  assert.equal(await form.locator('[name=style] optgroup').first().getAttribute('label'), 'Minimalism & Abstract Design');
  await advanced.locator('summary').click();
  await form.locator('[name=artist_description]').fill('band cue');
  await form.locator('[name=format]').selectOption({index: 1});
  const format = await form.locator('[name=format]').inputValue();
  await advanced.locator('summary').click();
  assert.equal(await form.locator('[name=artist_description]').inputValue(), 'band cue');
  assert.equal(await form.evaluate(el => new FormData(el).get('artist_description')), 'band cue');
  await advanced.locator('summary').click();
  await thumbnail(page, 43).click(); await selected(page, 43);
  assert.equal(await advanced.evaluate(el => el.open), true);
  await form.locator('[name=artist_description]').fill('other band cue');
  assert.equal(await form.locator('[name=style]').inputValue(), '');
  await form.locator('[name=style]').selectOption('002');
  await thumbnail(page, 42).click(); await selected(page, 42);
  assert.equal(await advanced.evaluate(el => el.open), true);
  assert.equal(await form.locator('[name=artist_description]').inputValue(), 'band cue');
  assert.equal(await form.locator('[name=description]').inputValue(), 'album cue');
  assert.equal(await form.locator('[name=format]').inputValue(), format);
  assert.equal(await form.locator('[name=style]').inputValue(), '001');
  assert.equal(await form.locator('[name=style] optgroup').count(), 12);
  await thumbnail(page, 43).click(); await selected(page, 43);
  assert.equal(await form.locator('[name=artist_description]').inputValue(), 'other band cue');
  assert.equal(await form.locator('[name=style]').inputValue(), '002');
  await page.close();
});

test('metadata panels start collapsed and preserve user expansion across artwork selection', async () => {
  const page = await setup();
  const panels = page.locator('#selected-metadata details');
  assert.equal(await panels.count(), 2);
  assert.equal(await page.locator('#selected-metadata details[open]').count(), 0);
  const generation = page.locator('details[data-metadata-label="Generation metadata"]');
  await generation.locator('summary').click();
  assert.equal(await generation.evaluate(el => el.open), true);
  await thumbnail(page, 43).click(); await selected(page, 43);
  assert.equal(await generation.evaluate(el => el.open), true);
  assert.match(await generation.locator('pre').innerText(), /Metadata #43/);
  await generation.locator('summary').click();
  await thumbnail(page, 42).click(); await selected(page, 42);
  assert.equal(await page.locator('#selected-metadata details[open]').count(), 0);
  await page.reload();
  assert.equal(await page.locator('#selected-metadata details[open]').count(), 0);
  await page.close();
});


for (const width of [320, 1400]) test(`browsing and editing layout and independent artist links at ${width}px`, async () => {
  const page = await setup({signed: true, width});
  await thumbnail(page, 43).click(); await selected(page, 43);
  await page.waitForFunction(() => document.querySelector('.artwork-preview img').naturalWidth > 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.evaluate(() => document.querySelector('.variation-section').getBoundingClientRect().top >= document.querySelector('.image-layout').getBoundingClientRect().bottom), true);
  const screenshots = process.env.BROWSER_SCREENSHOTS_DIR;
  if (screenshots) {
    fs.mkdirSync(screenshots, {recursive: true});
    await page.screenshot({path: path.join(screenshots, `album-${width}.png`), fullPage: true});
    await page.locator('#variation-advanced summary').click();
    await page.screenshot({path: path.join(screenshots, `album-advanced-${width}.png`), fullPage: true});
  }
  for (const view of ['gallery', 'catalog']) {
    await page.goto(`http://album.test/bot/${view === 'gallery' ? 'gallery' : ''}?q=%3CAlbum%3E&artist=%3CBand%3E&sort=album&order=asc&page=1`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.getByLabel('Search albums and artists').inputValue(), '<Album>');
    assert.equal(await page.getByRole('button', {name: 'Search', exact: true}).count(), 1);
    const artist = page.locator('.artist-link').first();
    const destination = new URL(await artist.getAttribute('href'), page.url());
    assert.equal(destination.pathname, view === 'gallery' ? '/bot/gallery' : '/bot/');
    assert.equal(destination.searchParams.get('artist'), '<Band>');
    assert.equal(destination.searchParams.get('page'), '1');
    assert.equal(destination.searchParams.has('q'), false);
    if (view === 'gallery') {
      assert.equal(await page.locator('.gallery-item a a').count(), 0);
      assert.equal(await page.locator('.gallery-item').first().locator('a').count(), 2);
    }
    await artist.focus();
    if (screenshots) await page.screenshot({path: path.join(screenshots, `${view}-${width}.png`), fullPage: true});
  }
  await page.close();
});

test('favorite follows artwork selection, supports keyboard toggling and preserves drafts', async () => {
  const writes = [];
  let favorite = null;
  const page = await setup({signed: true, handle: async route => {
    if (!route.request().url().includes('/favorites')) return false;
    const values = new URLSearchParams(route.request().postData());
    writes.push(Object.fromEntries(values));
    favorite = favorite === values.get('image_id') ? null : values.get('image_id');
    await route.fulfill({contentType: 'application/json', body: JSON.stringify({favorite_image_id: favorite})});
    return true;
  }});
  await page.evaluate(() => window.favoriteDocument = document);
  await thumbnail(page, 43).click(); await selected(page, 43);
  const star = page.locator('#favorite-toggle');
  assert.equal(await star.getAttribute('aria-pressed'), 'false');
  await page.locator('[name=description]').fill('unsaved favorite draft');
  const url = page.url();
  await star.focus(); await page.keyboard.press('Space');
  await page.waitForFunction(() => document.querySelector('#favorite-toggle').getAttribute('aria-pressed') === 'true');
  assert.equal(await star.locator('span').innerText(), '★');
  if (process.env.BROWSER_SCREENSHOTS_DIR) {
    fs.mkdirSync(process.env.BROWSER_SCREENSHOTS_DIR, {recursive: true});
    await page.screenshot({path: path.join(process.env.BROWSER_SCREENSHOTS_DIR, 'favorite-selected.png'), fullPage: true});
  }
  assert.equal(page.url(), url);
  assert.equal(await page.locator('[name=description]').inputValue(), 'unsaved favorite draft');
  assert.equal(await page.evaluate(() => document === window.favoriteDocument), true);
  assert.equal(writes[0].album_id, '42');
  assert.equal(writes[0].image_id, '43');
  assert.ok(writes[0].csrf_token);
  assert.equal(writes[0].user_id, undefined);
  await thumbnail(page, 44).click(); await selected(page, 44);
  assert.equal(await star.getAttribute('aria-pressed'), 'false');
  await page.goBack(); await selected(page, 43);
  assert.equal(await star.getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('[name=description]').inputValue(), 'unsaved favorite draft');
  await star.click();
  await page.waitForFunction(() => document.querySelector('#favorite-toggle').getAttribute('aria-pressed') === 'false');
  assert.equal(await star.locator('span').innerText(), '☆');
  await page.close();
});

test('favorite failures reconcile a lost response and disable unknown state without altering artwork', async () => {
  for (const available of [true, false]) {
    const page = await setup({signed: true, handle: async route => {
      if (!route.request().url().includes('/favorites')) return false;
      if (route.request().method() === 'POST' || !available) await route.abort();
      else await route.fulfill({contentType: 'application/json', body: '{"favorite_image_id":"43"}'});
      return true;
    }});
    await thumbnail(page, 43).click(); await selected(page, 43);
    await page.locator('[name=description]').fill('keep my draft');
    await page.locator('#favorite-toggle').click();
    await page.waitForFunction(() => document.querySelector('#favorite-status').textContent.includes('Could not') || document.querySelector('#favorite-toggle').getAttribute('aria-pressed') === 'mixed');
    assert.equal(await page.locator('#favorite-toggle').getAttribute('aria-pressed'), available ? 'true' : 'mixed');
    assert.equal(await page.locator('#favorite-toggle').isDisabled(), !available);
    assert.equal(await page.locator('[name=source_image_id]').inputValue(), '43');
    assert.equal(await page.locator('[name=description]').inputValue(), 'keep my draft');
    await page.close();
  }
});

test('a favorite save finishing after navigation updates the current star correctly', async () => {
  let finish;
  const page = await setup({signed: true, handle: async route => {
    if (!route.request().url().includes('/favorites')) return false;
    await new Promise(resolve => finish = resolve);
    await route.fulfill({contentType: 'application/json', body: '{"favorite_image_id":"42"}'});
    return true;
  }});
  await page.locator('#favorite-toggle').click();
  await page.waitForFunction(() => document.querySelector('#favorite-toggle').disabled);
  await thumbnail(page, 43).click(); await selected(page, 43);
  assert.equal(await page.locator('#favorite-toggle').isDisabled(), true);
  finish();
  await page.waitForFunction(() => !document.querySelector('#favorite-toggle').disabled);
  assert.equal(await page.locator('#favorite-toggle').getAttribute('aria-pressed'), 'false');
  await page.goBack(); await selected(page, 42);
  assert.equal(await page.locator('#favorite-toggle').getAttribute('aria-pressed'), 'true');
  await page.close();
});

test('Back restores the original default selection after a favorite change on a queryless URL', async () => {
  let favorite = null;
  const page = await setup({signed: true, handle: async route => {
    const request = route.request();
    if (request.url().includes('/favorites')) {
      favorite = '43';
      await route.fulfill({contentType: 'application/json', body: '{"favorite_image_id":"43"}'});
      return true;
    }
    if (favorite && !new URL(request.url()).searchParams.has('source_image_id')) {
      await route.fulfill({contentType: 'application/json', body: fs.readFileSync(path.join(fixtures, '43-signed.json'), 'utf8')});
      return true;
    }
    return false;
  }});
  await page.goto('http://album.test/bot/albums/42');
  await thumbnail(page, 43).click(); await selected(page, 43);
  await page.locator('#favorite-toggle').click();
  await page.waitForFunction(() => document.querySelector('#favorite-toggle').getAttribute('aria-pressed') === 'true');
  await thumbnail(page, 44).click(); await selected(page, 44);
  await page.goBack(); await selected(page, 43);
  await page.goBack(); await selected(page, 42);
  assert.equal(new URL(page.url()).searchParams.has('source_image_id'), false);
  await page.close();
});

test('Torta Mode is private to signed-in users and submits its CSRF-protected checkbox', async () => {
  const anonymous = await setup();
  assert.equal(await anonymous.getByLabel('Torta Mode', {exact: true}).count(), 0);
  assert.equal(await anonymous.getByText('Torta Mode', {exact: true}).count(), 0);
  await anonymous.close();
  const writes = [];
  const page = await setup({signed: true, handle: async route => {
    if (!route.request().url().includes('/torta_mode')) return false;
    writes.push(Object.fromEntries(new URLSearchParams(route.request().postData())));
    await route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(fixtures, '42-signed.html'), 'utf8')});
    return true;
  }});
  const checkbox = page.getByLabel('Torta Mode', {exact: true});
  assert.equal(await checkbox.isChecked(), false);
  assert.equal(await page.getByRole('button', {name: 'Save Torta Mode', exact: true}).isVisible(), false);
  await Promise.all([page.waitForNavigation(), checkbox.check()]);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].enabled, '1');
  assert.ok(writes[0].csrf_token);
  assert.equal(writes[0].return_to, '/bot/albums/42?source_image_id=42');
  assert.equal(writes[0].user_id, undefined);
  // Adding a second navigation form must not break the sign-out link.
  let logout = false;
  await page.route('**/logout', async route => {
    logout = route.request().method() === 'POST' && new URLSearchParams(route.request().postData()).has('csrf_token');
    await route.fulfill({status: 200, body: 'Signed out'});
  });
  await Promise.all([page.waitForNavigation(), page.getByRole('link', {name: 'Sign out', exact: true}).click()]);
  assert.equal(logout, true);
  await page.close();
  const noJS = await setup({signed: true, javascript: false});
  assert.equal(await noJS.getByLabel('Torta Mode', {exact: true}).isVisible(), true);
  assert.equal(await noJS.getByRole('button', {name: 'Save Torta Mode', exact: true}).isVisible(), true);
  await noJS.close();
});
