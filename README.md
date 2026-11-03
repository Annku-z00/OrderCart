# OrderCart — WhatsApp Order Extractor

OrderCart is a small Windows desktop app for a picture-frame seller. It reads a
range of WhatsApp chats, pulls the **Order ID** out of the customer's
Meesho/Flipkart/Amazon screenshot with OCR, falls back to the **Customer Name**
and then the **Address**, and writes one tidy row per chat to a Notepad file or
a CSV — in a single click.

It talks to a **free, self-hosted** WhatsApp HTTP API (WAHA or Evolution). No
paid API, no Meta approval, no browser automation (no Playwright / Selenium).

---

## 1. What it does

For every internal id from **Start ID** to **End ID** (e.g. `12988` → `13010`):

1. Skips it if it was already processed (unless *Reprocess* is ticked).
2. Finds the chat whose contact name matches the internal id.
3. Reads every incoming text message (customers often type a labelled id such
   as `Order Id: 342216136358560832`) — text is parsed directly, never OCR'd.
4. Downloads **all** image messages, runs OCR on each and merges every id.
5. Writes one output row **immediately**, then marks the id as done.

Field priority is always (one row per distinct id when ids are found):

```
Order ID(s)  →  Customer Name  →  Address
```

### Output formats

**Notepad mode** (`orders.txt`) — one line per chat:

```
12988,342022570640639296        ← full 16-digit Meesho Order ID
12989,TRUNC:34187320244         ← the screenshot cut the ID off
12990,NAME:Priya Sharma         ← no ID found, name used instead
12991,ADDR:12 MG Road, Pune     ← only the address was readable
12992,FAILED                    ← nothing could be extracted
```

**CSV mode** (`orders.csv`):

```
internal_id,order_id,name,address,status,source,confidence,timestamp
12988,342022570640639296,,,OK,image,0.95,2026-10-16 18:20:04
12989,TRUNC:34187320244,,,TRUNC,image,0.70,2026-10-16 18:20:05
12990,,Priya Sharma,,FALLBACK_NAME,text,0.82,2026-10-16 18:20:06
```

Addresses keep their line breaks in the Notepad format (`\n`) and are joined
with `|` in CSV so a row always stays on one line.

---

## 2. Install a free WhatsApp API

You need **one** of the two self-hosted options below. Both work by scanning a
QR code once, just like WhatsApp Web; the server keeps the session alive and
exposes a REST API.

### Option A — WAHA (recommended)

- Project: <https://github.com/devlikeapro/waha>
- Docs: <https://waha.devlike.pro/>

Quick start with Docker:

```bash
docker run -d --name waha -p 3000:3000 \
  -e WHATSAPP_DEFAULT_ENGINE=WEBJS \
  -e WAHA_API_KEY=YOUR_KEY \
  -e WHATSAPP_FILES_LIFETIME=0 \
  -e WHATSAPP_DOWNLOAD_MEDIA=true \
  -v "$(pwd)/waha-data/sessions:/app/.sessions" \
  -v "$(pwd)/waha-data/files:/tmp/whatsapp-files" \
  devlikeapro/waha
```

Open <http://localhost:3000/dashboard>, start a session, scan the QR code with
your phone. Set `whatsapp_api.provider: "waha"` and `whatsapp_api.api_key` in
[`config.yaml`](config.yaml:6) if your instance uses a key.

**Media / images — how it works and why these flags matter**

WAHA has **no per-message media endpoint** (`…/messages/{id}/media` does not
exist and always answers `404`). Media is only available two ways:

1. `GET /api/{session}/chats/{chatId}/messages/{messageId}?downloadMedia=true`
   — WAHA fetches the attachment from WhatsApp and stores it server-side;
2. the stored file is then published as a **static** url in `message.media.url`
   (`…/api/files/{session}/{messageId}.{ext}`).

Two defaults get in the way, so the flags above override them:

| Variable                    | Default | Why it matters here                                   |
| --------------------------- | ------- | ----------------------------------------------------- |
| `WHATSAPP_FILES_LIFETIME`   | `180`   | Files are **deleted after 3 min**; `0` keeps them.     |
| `WHATSAPP_DOWNLOAD_MEDIA`   | `true`  | Ensures the message list already carries `media.url`.  |

The client in [`whatsapp/api_client.py`](whatsapp/api_client.py:403) asks for
`downloadMedia=true` on demand, so it works even with the default lifetime — but
keeping files around (`WHATSAPP_FILES_LIFETIME=0`) makes re-runs far faster and
more reliable. If you publish WAHA on a host port other than `3000`, no extra
configuration is needed: the client rewrites the hard-coded
`http://localhost:3000/...` inside `media.url` onto your configured
`whatsapp_api.base_url`. Note that voice notes / video cannot be read by OCR and
are skipped automatically.

### Option B — Evolution API

- Project: <https://github.com/EvolutionAPI/evolution-api>
- Docs: <https://doc.evolution-api.com/>

Quick start with Docker:

```bash
docker run -d --name evolution-api -p 8080:8080 \
  -e AUTHENTICATION_API_KEY=YOUR_KEY \
  atendai/evolution-api:latest
```

Then create an instance, scan the QR code, and set:

```yaml
whatsapp_api:
  provider: "evolution"
  base_url: "http://localhost:8080"
  api_key: "YOUR_KEY"
  session_name: "my-instance"
```

> **Cloud hosting:** both projects also run on Railway / Render / Fly.io if you
> don't want a machine running at home. Point `base_url` at the public URL.

⚠️ **Legal / ethical note.** Only read chats that belong to *your own* WhatsApp
account, i.e. orders your customers actually sent you. OrderCart never sends
messages; it only reads chats and downloads the images in them.

---

## 3. Run from source

Requires **Python 3.10+** (developed and tested on Windows 11 / 3.10.11).

```bash
python -m pip install -r requirements.txt
python main.py
```

OCR runs on **Google Lens** (the upload protocol is implemented in-tree —
`ocr/proto.py` + `ocr/lens.py` — so there is no OCR dependency to install),
so it needs an internet connection. There is nothing heavy to download and no
local model to warm up.

### Google Lens only

Google Lens is the **only** OCR engine. OrderCart never falls back to
PaddleOCR, Tesseract or any other local engine — those are not installed and
not referenced anywhere in the code. If a Lens request fails (network error,
timeout, bad response) it is retried per the retry policy in
[`config.yaml`](config.yaml):

```yaml
ocr:
  lens_timeout: 15        # seconds per request
  lens_retries: 2         # extra attempts after the first failure
  lens_retry_backoff: 0.5 # seconds between attempts
```

When every attempt fails, the image is recorded as an **OCR failure** and the
chat is left as `PARTIAL` (written but not marked processed) so the next run
retries it — failed OCR is never silently treated as success.

The OCR step is deliberately **simple**: the screenshot is handed straight to
Lens (the file path, no local decode) with no grayscale / denoise /
thresholding, because these are crisp digital images and binarisation only
hurts accuracy. The only optional tweak is `ocr.upscale_factor`, which is kept
for compatibility but is a no-op on the Lens path.

---

## 4. Using the app

| Field | Meaning |
|-------|---------|
| **Start ID / End ID** | The internal contact names to walk, inclusive. |
| **Format** | Notepad (`id,value`) or CSV. |
| **Output file** | Where rows are appended. Existing rows are never touched. |
| **Reprocess IDs already marked done** | Ignore the saved state and re-run. |
| **Dry run** | Process the range but write nothing and mark no state (a safe rehearsal). |
| **API URL / key / session** | Pre-filled from `config.yaml`, saved back on RUN. |

Buttons:

- **▶ RUN** — start the run in the background (the window stays responsive).
- **⏸ Pause / ▶ Resume** — pause between chats without losing your place.
- **■ Stop** — finish the current chat and stop; re-running resumes safely.
- **↺ Reset state** — forget processed ids so the next run does them again.
- **🔎 Review fixes…** — open the TRUNC/FAILED review window (see §4.1).
- **📁 Open output / review folder** — jump straight to the files.

The app writes a log to `logs/ordercart.log` and to the on-screen **Live log**.

### 4.1 Production-ready helpers

These make the tool safe to hand to a non-technical user:

**Dry run.** Tick the *Dry run* box (or call `run(..., dry_run=True)`) and the app
walks the whole range, runs OCR and builds the summary — but **writes no output
rows and records nothing in the state DB**. Use it to rehearse a big batch, or to
check that the API/keys work, before touching real files.

**Review workflow for TRUNC / FAILED ids.** After any run, the ids that need a
human decision (truncated or unreadable ids) are collected — `summary.review_ids()`
— and the review window pops up. Type or paste the correct Order ID next to each
id and press **💾 Save corrections**. Each row in the output file is rewritten in
place (`apply_corrections`) and the id is marked done so it won't be retried.

```
12988,342022570640639296
12989,341873202440000001        ← corrected from TRUNC:34187320244
```

**Raw OCR logs.** For every TRUNC or FAILED id the exact text OCR produced is
saved to `logs/<id>_raw.txt` (with a small header), so you can see *why* a row
failed without re-running anything.

**Saved images for review.** For every TRUNC id the source screenshot is copied
to `review/<id>.jpg`, so you can open the picture next to `logs/<id>_raw.txt`
and read the missing digits by eye.

### Folders it uses

```
logs/     ordercart.log  +  <id>_raw.txt (raw OCR for TRUNC/FAILED ids)
assets/   downloaded screenshots, one subfolder per internal id
state/    state.db  (SQLite resume state)
review/   <id>.jpg — screenshots for TRUNC ids, kept for manual checking
failed/   spare folder for manual follow-up
```

---

## 5. Build a standalone .exe

```bash
build.bat
```

That runs (see [`build.bat`](build.bat)):

```bat
pyinstaller --onedir --windowed --name OrderCart main_web.py ^
  --add-data "ui;ui" --add-data "config.yaml;." ...
```

The result is `dist\OrderCart\OrderCart.exe` (ship the whole `dist\OrderCart\`
folder). There are no OCR models to bundle — OCR runs on Google Lens, so the
target machine just needs an internet connection.

`build.bat` also runs `make_build_info.py`, which stamps `build_info.json`
with the commit the build came from and copies it next to the `.exe` — that is
what the update check compares against. `config.yaml` is optional: the app
starts on the built-in defaults and writes one when Settings is saved (copy
`config.example.yaml` next to the exe if you want to start from the template).

### 5.1 Updates (git)

On launch OrderCart asks GitHub for the newest commit on `main` and shows a
small banner when it is newer than this install. **Settings → App updates**
has **Check now**, **Update now** and **Open GitHub**.

* **Source run / git clone** — `Update now` runs `git pull --ff-only` in the
  project folder and asks you to restart (the running process keeps the old
  code until then).
* **Built copy** — there is no `.git` folder, so it cannot be patched in
  place; the button is hidden and the banner opens
  <https://github.com/Annku-z00/OrderCart> so you can download the newest
  release.

It compares **commits**, not version numbers: `build_info.json` for a build,
`git rev-parse HEAD` for a clone, against the head of `main`. Configure it in
`config.yaml`:

```yaml
update:
  enabled: true
  repo: Annku-z00/OrderCart
  branch: main
  check_on_start: true
```

Only the public repository is contacted — orders, chats and settings never
leave the machine, and nothing is downloaded during a check.

---

## 6. Known limitations

- **Truncated IDs.** When a screenshot cuts the Order ID off (e.g.
  `Order #34187320244...`), OrderCart cannot invent the missing digits. It emits
  `TRUNC:34187320244` so you can look it up manually. Digit runs of 5–14 next to
  an "Order …" label are treated the same way.
- **Minimum length.** A complete Order ID must have **6 or more digits** — there
  is no fixed 16- or 18-digit minimum. A shorter run is never accepted as a
  complete id; the customer-name then address fallback is used instead.
- **Unrelated numbers.** Length alone never proves an id: phone numbers, PIN
  codes, prices, **dates** (`20261015` / `15/10/2026`), product numbers
  (`SKU` / `Model` / `Size` / `Qty` / tracking / invoice) and other unrelated
  numbers are rejected. Adjacent digit groups are never fused into one id.
- **Multiple Order IDs.** A chat may hold several orders across several
  screenshots and typed messages; every distinct id is extracted and written as
  its own row.
- **No AI fallback (yet).** An AI-agent pass for the messy leftovers is
  intentionally **not** included in this version; it is planned for v2.
- OCR occasionally misreads a digit. Low-confidence rows still get written so
  you can eyeball them; check `confidence` in the CSV.

---

## 7. Tests

```bash
python -m pytest tests/ -q
```

The suite covers the extractor (34 tests), the OCR/writer/state layer (29), the
orchestrator loop (29) and the Phase 4 production features (17): dry run,
raw-OCR logs, saved review images, `apply_corrections` for both formats and
`RunSummary.review_ids()`. Everything uses a fake WhatsApp client and a fake OCR
engine, so the whole suite runs in well under a second and never touches the
network.

---

## 8. Design decisions worth knowing

1. **Write output *before* updating state.** If the app is killed between the
   two, the worst case is that a chat is processed twice — never that a row is
   lost.
2. **Append + `flush` + `fsync` on every row**, with a `.tmp` + rename fallback,
   so a crash or a locked Notepad window can't corrupt the file.
3. **Truncated detection runs before the generic number rule**, because a
   10–14 digit run near an Order label is far more likely a cut-off id than a
   complete one.
4. **No image preprocessing beyond an optional upscale** (see §3).
5. **The GUI never blocks.** The loop runs in a worker thread and reports back
   through a `queue.Queue` polled with `after()`.
6. **Dry run short-circuits *after* the artefacts but *before* the write.**
   Side files (raw OCR, review image) are still produced so a rehearsal is just
   as diagnostic as a real run, while the output file and state DB stay pristine.
7. **Corrections go through one dispatcher** (`output/corrections.py`) so the GUI
   only needs the output path + format — it never has to know which writer class
   is in play.

---

## 9. Project layout

```
main.py                  tkinter GUI + worker thread
frameflow_config.py      config.yaml loader + folder helpers
config.yaml              all tunables (API, OCR, regexes, output)
whatsapp/api_client.py   WAHA + Evolution REST client
ocr/preprocess.py        minimal OpenCV load/upscale
ocr/engine.py            Google Lens OCR — the client is in-tree
ocr/lens.py              the Lens upload + the protobuf answer
ocr/proto.py             a tiny protobuf wire codec (no protobuf runtime)
extractor/rules.py       labels, regexes, validation rules
extractor/parser.py      Order ID / Name / Address extraction + TRUNC
output/writer.py         Notepad writer (+ apply_corrections)
output/csv_writer.py     CSV writer (+ apply_corrections)
output/corrections.py    format-agnostic correction dispatcher
state/state.py           SQLite resume state
core/orchestrator.py     the per-id batch loop (dry run + artefacts)
tests/                   pytest suite (109 tests)
build.bat                one-click PyInstaller build
```
