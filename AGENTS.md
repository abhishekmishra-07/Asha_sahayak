# Project rules

## Architecture and boundaries
- Product name is ASHA SAHAYAK. Do not bring back preloaded demo patients, scenario fixtures, or reset-demo UI.
- Python backend in `server.py` using the standard library, local SQLite by default, and optional psycopg 3 for PostgreSQL; semantic HTML, CSS, and browser-native JavaScript frontend.
- Persistent patient, encounter, and follow-up records use `data/asha_sahayak.sqlite3` unless `DATABASE_URL` configures PostgreSQL; generated PDFs remain local in `data/reports/`. Keep the web server on localhost unless real authentication and access control are implemented.
- Public HTTP routes must use an explicit static-file allowlist; never serve the data directory, source, tests, or environment files.
- `configured_host()` must reject non-loopback interfaces until authentication and per-user access control are implemented. Do not weaken this guard to make a cloud host start.
- No application authentication, role controls, or guaranteed encryption at rest are configured. An optional Gemini audio service is available only when GEMINI_API_KEY is configured and the user explicitly opts in. It transcribes after recording stops; do not present it as real-time streaming or production-secure. Do not recommend entering identifiable patient health data in this build.
- Keep microphone capture behind a deliberate action and Gemini transmission behind an explicit per-visit consent checkbox. In Gemini mode, audio is sent to the loopback server in memory and forwarded to Google; do not persist it locally and request deletion of the temporary Gemini file after processing. Explain provider processing and prohibit identifiable patient health information. Browser speech recognition remains the no-key fallback.

## Clinical documentation
- Preserve transcript, structured fields, source excerpts, and worker corrections separately.
- Missing values remain "Not documented" (translated in Hindi report labels); do not invent diagnoses, observations, prescriptions, medications, allergies, or history.
- Manual transcript matching uses deterministic literal phrase matching. Gemini audio suggestions must include exact transcript evidence; discard suggestions whose value or source excerpt is not present in the transcript. Label all Gemini output as suggestions and require human review.
- Require a review decision on each field before finalization. Preserve approved data before amendments.

## Reports
- Render from the selected saved encounter and associated patient only.
- Apply optional sections on the server. Never include the transcript when excluded.
- Validate the PDF signature before returning it. Use A4 layout, Hindi-capable font fallback, multi-page flow, page numbers, and sanitized ID/date filenames.
- Translate report headings only; never claim the app translated clinical content.

## Visual and interaction requirements
- Keep the charcoal, warm white, lime, and violet palette and tablet-first responsive layout.
- Keep English, Hindi, and Hinglish controls understandable; do not leave untranslated dictionary keys in UI.
- Respect reduced-motion preferences. Use icons with accessible labels rather than icon-only unlabeled actions.

## Commands
- Install PostgreSQL driver when using PostgreSQL: `python -m pip install -r requirements.txt`
- Start: `python server.py` (local SQLite works without `DATABASE_URL`)
- Syntax check: `python -m py_compile server.py`
- Tests: `python -m unittest discover -s tests -v`

## Acceptance criteria
- Startup presents an empty workspace unless the user has created records.
- Patient, encounter, review, finalization, amendment, and follow-up operations persist in local SQLite by default or PostgreSQL when `DATABASE_URL` is configured.
- Audio is sent to the local server and Gemini only after the explicit sharing checkbox and a deliberate transcription action. The server handles bytes in memory, requests Gemini file deletion after use, and never persists the raw audio. Gemini performs batch transcription after recording stops, not real-time transcription. Disclose provider handling and keep human review mandatory.
- The app does not claim authenticated authorization, secure offline storage, or real-time server transcription. Gemini field suggestions are explicitly labeled and reviewed with exact transcript excerpts.
- The app must refuse non-loopback binding while unauthenticated; network deployment is not accepted as ready for clinical data.
- PDF uses the correct saved encounter, supports selected sections and report-heading language, validates as PDF, and uses an ID/date filename.
- Tests must use isolated temporary stores and must not populate a configured live PostgreSQL database.
