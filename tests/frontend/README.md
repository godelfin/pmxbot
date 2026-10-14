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
Left/Right selects adjacent artwork, except while typing in a form field or
viewing the full-size dialog. In history, Home/End moves focus to either end,
without selecting artwork. Shift+Left/Right opens the previous/next album,
except in form fields and the full-size dialog. Album Previous/Next follows the
originating collection filters and sort across page boundaries; direct album
URLs use newest-first order. These controls appear below the album title.
Artwork history is visually
thumbnail-and-icon only, with accessible labels. Settings and generation
metadata start collapsed and retain an expanded state across selections.
Variation edits are remembered separately for each image until the page is
closed or refreshed.
During a selection request, submission is disabled. Failed requests retain the
previous image, matching source and draft; Retry repeats the failed selection.
