# Artwork history browser checks

From the repository root, with Python testing/viewer dependencies installed:

```sh
npm install --prefix tests/frontend
tests/frontend/node_modules/.bin/playwright install chromium
node --test tests/frontend/artwork_history.test.cjs
```

Set `PYTHON` to the Python executable containing the project dependencies if needed.
The tests render real CherryPy/Jinja views into temporary fixtures and serve them
through Playwright's request interception. No provider credentials or live image
services are used. The dedicated GitHub Actions workflow runs them in Chromium.

The history strip supports native horizontal scrolling and arrow buttons.
While a thumbnail has focus, Left/Right moves focus, Home/End reaches either end,
and Enter/Space selects. Focus movement does not select artwork. Variation edits
are remembered separately for each image until the page is closed or refreshed.
During a selection request, submission is disabled. Failed requests retain the
previous image, matching source and draft; Retry repeats the failed selection.
