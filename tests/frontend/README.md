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
Artwork history shows provenance-aware labels and a Selected indicator.
Detailed shortcuts live in a Keyboard help disclosure. Settings and generation
metadata start collapsed and retain an expanded state across selections.
Common variation controls stay visible, while format and band settings live in
Advanced options; toggling it preserves values and expansion across selections.
Variation edits are remembered separately for each image until the page is
closed or refreshed.
During a selection request, submission is disabled. Failed requests retain the
previous image, matching source and draft; Retry repeats the failed selection.

CI retains desktop (1400px) and mobile (320px) screenshots of collection views
and album editing states in the `ui-screenshots` artifact for seven days. Set
`BROWSER_SCREENSHOTS_DIR` when running locally to capture the same views.


Authenticated users can choose one favorite artwork per album using the star
below the selected image, when multiple versions exist. The control works after
history selection and keyboard activation, preserves unsaved generation values,
and announces saves or failures. Lost responses trigger a status reconciliation;
unknown state disables the control until reload. Favorites do not change an
explicit selection or Back/Forward history. Browser checks cover these behaviors,
including a save completing after selection changes. The screenshot artifact
includes a filled favorite star with an unsaved generation draft.

Prompt-rule checks cover mobile and desktop forms, whole-word/phrase defaults,
plain-language scope and match choices, separate explicit replacement, detail
edits without matching inputs, readable legacy summaries and absence of internal
matching patterns from rendered content or submitted controls. HTTP rejection,
preview evaluation and migration behavior are covered by the Python tests.
