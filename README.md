# ASHA SAHAYAK

**Your voice. Structured care. Better community health.**

A tablet-friendly encounter documentation tool for ASHA workers and care teams. Create patient records from information you enter, record a conversation, review the transcript and source-linked field suggestions, save the encounter to local SQLite or an optional PostgreSQL server, and download an A4 PDF for clinical handoff.

The interface follows the shared visual reference: charcoal navigation, warm white workspace, electric lime actions, violet accents, and bold display type. It supports English, Hindi, and Hinglish interface labels. Patient-entered clinical text stays as entered; the application does not translate medical content.

## Database setup

Requirements: Python 3.10+ and Chrome or Edge for PDF generation. Local SQLite is used by default; PostgreSQL is optional.

Create a private environment file:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

The app uses `data/asha_sahayak.sqlite3` and reuses existing local records. To use PostgreSQL instead, create a database and app user with permission to create tables and indexes, set `DATABASE_URL` in `.env`, and install the driver with `python -m pip install -r requirements.txt`. Use the TLS option required by your provider; hosted providers commonly require `sslmode=require`. Set `GEMINI_API_KEY` for optional Gemini transcription and field suggestions. Keep credentials private; the Gemini key stays on the server and is never sent to browser JavaScript. Restart the app after changing `.env`, then start it:

```powershell
python server.py
```

Open <http://127.0.0.1:8765>. Stop the service with Ctrl+C. Patient, encounter, and follow-up records are stored in local SQLite unless `DATABASE_URL` is configured, in which case PostgreSQL is used. Generated PDFs remain in `data/reports/`. If configured PostgreSQL cannot be reached, the app reports the connection error and does not silently switch databases.

If the browser shows an older app, stop its running server in the terminal where it was started by pressing Ctrl+C, then start `python server.py` again from this folder. Only one app can use port 8765 at a time. If that terminal is unavailable, choose another port with `$env:ASHA_PORT = "8766"` before starting and open <http://127.0.0.1:8766>.

The SQLite schema is created automatically, and an existing local SQLite database is reused. To migrate reviewed local records into an empty PostgreSQL database, set `DATABASE_URL` and `$env:ASHA_IMPORT_SQLITE = "1"` before the first app request. For an older JSON store, use `$env:ASHA_IMPORT_JSON = "1"` instead. Import is opt in and the source file is left unchanged. Do not set either option if the source contains placeholders.

## Create and export a real record

1. Open **Patients** and create a patient profile with only the details you need.
2. Select **New visit**, choose the patient, visit type, date, and spoken language.
3. For Gemini transcription, check the audio-sharing consent, press the microphone button, and stop when finished. The recording is sent for batch transcription after you stop; transcription is not live while recording. Without a Gemini key, the app falls back to browser speech recognition.
4. To use an existing clip, select it, check the sharing consent, then press **Transcribe audio & find fields**. Gemini transcribes the clip and returns structured suggestions. If transcription fails, you can enter or paste the transcript and use **Find supported phrases**.
5. Review the transcript and every source excerpt. Gemini suggestions are accepted only when their value and evidence appear in the transcript, but they can still be wrong. Keep the review decision human; these are not clinical conclusions.
6. Review each field and its source text. Accept, edit, or mark each field unknown; do not treat suggestions as clinical decisions.
7. Save the record. Finalization is enabled only after each field has a review decision.
8. Open **Report center**, choose the saved encounter, output language, report type, and sections, then download the PDF. A new PDF is generated from the saved record.
9. Create and manage follow-up tasks from **Follow-ups**.

## Voice and data boundaries

- Gemini transcription requires a deliberate recording/upload action and a separate per-visit consent checkbox. In that mode, audio travels through the loopback server in memory to Google Gemini; the local app does not save the raw audio. The app requests deletion of its temporary Gemini file after processing. Google documents that uploaded files can remain for up to 48 hours if they are not deleted.
- Recording transcription runs after the worker stops recording. It does not display a Gemini transcript in real time. Without `GEMINI_API_KEY`, the app uses the browser speech API where available; browser providers may process audio under their own terms.
- Gemini output includes the transcript and field suggestions. Suggestions are shown only when the returned value and source excerpt occur in the transcript, but this check cannot prove clinical correctness. Review each field against the audio before saving.
- Do not submit identifiable patient health information. Google data handling depends on whether the Gemini API project uses unpaid or paid service; check the applicable Google API terms for the key in use.
- Hindi report labels use the chosen language. Clinical values and transcript are preserved in their original entered language, without automatic translation.

## Privacy and production readiness

The app has no user sign-in, roles, or per-user access controls. The web service binds to `127.0.0.1` and refuses non-loopback binding. Local SQLite data is stored on this computer; encryption at rest is not configured by this app. **This build is not ready for public hosting or identifiable patient health information.** Do not expose it to a network or use it for identifiable records without authentication, access controls, protected storage, and an approved hosting environment. No legal, regulatory, government, ABDM, or ABHA compliance is claimed.

The configured database is the source of truth for patient, encounter, and follow-up records. Generated PDFs remain under `ASHA_DATA_DIR` (default `data/`). Back up the SQLite file and reports folder, or your PostgreSQL database and reports folder, as appropriate.

The app does not sync between authenticated users and has no secure offline storage. A browser-only pending draft fallback may be used after a server request fails; browser storage is not encrypted. Do not rely on it as a protected backup.

## Reports

The report center provides comprehensive, summary, follow-up, audit, and patient-history report types; section selection; English/Hindi/Hinglish headings; A4 layout; source excerpts; workflow state; a documented completeness denominator; and page numbering. The PDF renderer uses Chrome or Edge headless mode and validates the `%PDF-` signature before download. Long transcripts flow across pages. Devanagari output depends on an installed Hindi-capable font such as Nirmala UI or Mangal.

Structured JSON export is available. FHIR, aggregate CSV, cloud sync, authentication, and real-time server transcription are not implemented. Gemini audio processing is optional and requires an API key plus explicit consent. No public report URLs are provided.

## Commands

```powershell
python -m py_compile server.py
python -m unittest discover -s tests -v
```

## Feature status

| Capability | Current status |
|---|---|
| English, Hindi, Hinglish interface | Implemented labels; clinical values are not translated |
| Responsive tablet and mobile interface | Implemented |
| Patient and encounter records | Local SQLite by default; PostgreSQL when `DATABASE_URL` is configured |
| Microphone capture | Browser recording; audio remains in browser memory |
| Speech recognition | Optional browser fallback, or Gemini batch transcription after consent |
| Structured field suggestions | Gemini suggestions with exact source excerpt checks; human review required |
| Review and finalize workflow | Implemented locally; no authenticated user identity |
| Follow-up management | Local create, edit, complete, and filter |
| Professional PDF | Chrome/Edge generated from saved encounter; section and language selection |
| JSON export | Implemented |
| Dark mode, loading state, scroll reveal | Implemented; reduced-motion preference respected |
| App authentication, role controls, guaranteed encryption, FHIR, ABDM/ABHA | Not implemented |

## Three-minute walk-through

- 0:00-0:25: Show the blank dashboard and explain that records are entered by the user and this build loads no fake patients.
- 0:25-0:55: Register a test patient using non-identifying sample information and begin a voice visit.
- 0:55-1:30: Record a short English, Hindi, or Hinglish statement; review the transcript and manually correct any errors.
- 1:30-2:00: Run phrase matching, show source excerpts, and accept/edit/mark unknown fields.
- 2:00-2:35: Save and finalize, then generate a concise or comprehensive PDF in the chosen heading language.
- 2:35-3:00: Open the PDF, show the review status and follow-up section, and explain the selected database mode and browser speech limitations.

## Project layout

- `server.py` | local web API, SQLite/PostgreSQL persistence, phrase matching, and PDF generation.
- `requirements.txt` | Optional PostgreSQL driver.
- `index.html`, `styles.css`, `app.js`, `brand-logo.png`, `favicon.png`, `icons.svg` | responsive web UI, branding, language/theme controls, audio capture, and report workflow.
- `report_translations.json` | report headings for Hindi/Hinglish.
- `tests/` | isolated tests using temporary synthetic records; tests do not populate the live record store.
- `AGENTS.md` | continuation rules and project acceptance criteria.
