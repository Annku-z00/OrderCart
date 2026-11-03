# AGENTS.md — OrderCart Project Context for AI Agents

> Hand this file to any AI agent before asking it to modify OrderCart.
> It describes what the project is, how it is structured, the hard rules,
> and where the open improvement opportunities are.

## 1. What OrderCart Is

**OrderCart** is a Windows desktop tool that bulk-extracts Meesho-style order
data out of self-hosted WhatsApp groups, without the official WhatsApp
Business API.

The seller maintains chats named after **internal order IDs** (e.g. `12988`).
For every ID in a range the app:

1. Finds the WhatsApp chat named `<internal_id>`.
2. Pulls recent messages: plain text first, then images (screenshots of
   Meesho order screens).
3. Runs **OCR** on the images (Google Lens — the upload protocol is
   implemented in-tree, `ocr/proto.py` + `ocr/lens.py`; there is **no OCR
   dependency** to install, but every screenshot needs internet).
4. Extracts **Order ID / Customer Name / Address** from OCR text + chat text.
5. Appends the result to an output file (Notepad `id,value` lines or CSV).
6. Marks the ID as processed in SQLite so a re-run resumes, never repeats.

There are **two GUIs**: the original tkinter one (`main_tk.py`, backup) and
the current **PyWebview cosmic dashboard** (`main_web.py` + `ui/`).

- Python: 3.11+ (developer machine uses `py -3.12`, Anaconda).
- Platform: Windows first (os.startfile, WebView2, .ico, PowerShell).

## 2. Architecture / Data Flow

```
main_web.py (PyWebview shell)
  └─ Api class (JSON API exposed to JS as window.pywebview.api)
       └─ background thread
            └─ core/orchestrator.py   ← the batch brain
                 ├─ whatsapp/api_client.py    (WAHA/Evolution REST client)
                 ├─ ocr/engine.py             (Google Lens, implemented in-tree)
                 │    ├─ ocr/lens.py           (the Lens upload + protobuf answer)
                 │    └─ ocr/proto.py          (a tiny protobuf wire codec)
                 ├─ extractor/parser.py       (regex/label field extraction)
                 ├─ extractor/rules.py        (Rules config, normalize())
                 ├─ state/state.py            (SQLite resume-state store, scoped per account)
                 └─ output/writer.py + csv_writer.py (append-only writers)
                 └─ output/corrections.py     (rewrite rows after review)
```

Every step is dependency-injected into `Orchestrator` so tests can replace
API + OCR with fakes and run in milliseconds.

### Data flow contract (do not break)
* `mark_processed()` happens **only after** the output row was written.
  A crash between write and state-update always leads to a *safe re-process*,
  never a lost/skipped row.
* `TRANSIENT_ERROR` statuses are **never persisted** to the state DB
  (an API blip should be retried next run, not skipped forever).
* Output format per internal id: `f"{internal_id},{value}"` where value may
  carry prefixes `TRUNC:<digits>`, `NAME:<name>`, `ADDR:<address>`, or `FAILED`.
  This encoding is load-bearing: `main_tk.py`, `main_web.get_orders()`,
  `output/corrections.py`, and the tests all decode it.
* **Two Notepad layouts.** `output.notepad_mode: append` keeps the original
  `internal_id,value` lines. `fill` updates an existing Notepad++ master list
  **in place**: a line `12328` becomes `12328<TAB>3420026566347011`.
  **A line never carries more than one value** — several orders are never
  written as several tab-separated ids. `TRUNC:` placeholders are never
  written. When a chat yields **no** order id the fallback chain applies to
  the list too — **name**, else **address** — written plain, with no
  `NAME:`/`ADDR:` marker: `12329<TAB>Priya Sharma`, `12330<TAB>12 MG Road,
  Pune`. When a chat carries **several** orders that all belong to the **same
  customer** (same name, compared case- and whitespace-insensitively) the name
  is written instead — `12331<TAB>Priya Sharma`. A **mixed** set never blanks
  the line either: when the names differ or some orders carry none, the most
  common name actually received is used (the first one on a tie) and the row
  is **flagged in the app**, not in the file — the terminal prints `<id>: WARN
  MULTI: names differ - 4 orders in this chat (the names differ); the list
  used the name 'X'` (`event=multi_order_flagged kind=MULTI: names differ` in
  the log) and the result carries `ambiguous = True` plus `review_reason`, so
  the Orders table shows the row under "Needs review" **with a small amber
  chip naming the check**: `MULTI: names differ`, `MULTI: name missing`
  (some orders had no name — the usual cropped screenshot) or
  `MULTI: no name`. **A chat with two or more orders never gets an order
  id** — one of several ids would read like a single-order chat — so the line
  gets a name, else the address, else it stays blank and the app flags it.
  The address is likewise the last resort for a chat whose rows carry no
  usable id.
  A **cropped** id counts as an order from
  `extraction.truncated_id_min_digits` digits on (6 by default, and the
  customers crop the back of the Meesho screen), so a cut-off screenshot still
  fills its line with the digits that survived; a five-digit or shorter crop
  is **not** an id and falls through to the name / address instead.
  A fallback only ever fills a **blank** line:
  a real id (or a `TRUNC:` crop) already there is never overwritten by a name,
  and a later real id replaces a name. A `FAILED` chat stays blank **and
  raises a visible error**: `12988: ERROR nothing extracted - no order id, no
  name and no address` (red in the terminal, `event=nothing_extracted` at
  ERROR level in the log). The master list is authoritative — the seller makes
  one per day/week and it carries the ids already — so an internal id with no
  line is logged (`event=notepad_line_missing`) instead of being appended at
  the bottom; only a missing or still-empty file receives new rows, and the
  terminal shows `<id>: WARN not in the Notepad list - not filled` so the
  mismatch is visible. This writer is separate from
  `output.default_file`, which the Orders table reads.
* **One screenshot = one order.** The orchestrator extracts every image
  *separately* and merges the results — the OCR text of a whole chat is never
  parsed as one blob (the label of screenshot #1 would swallow the digits of
  screenshot #2). A chat can carry dozens of orders; with
  `extraction.multi_order_output: true` every order id becomes its own output
  row (`order_rows` carries the per-row status, `order_ids` the complete ids,
  `truncated_ids` the cut-off ones).
* Extraction rules (business): **the label is the anchor.** Every order
  screenshot carries an order label (`Order ID` / `Order Id` / `Order #` /
  `Order No` / Hindi-regional equivalents), so a number is only an order id
  when it sits next to such a label. Phone numbers, PIN codes, prices,
  **dates** and **product numbers** are rejected; adjacent digit groups are
  never concatenated. The label-free fallback exists only as a safety net for
  a garbled label and is restricted to the **Meesho id band** (15–18 digits) —
  a bare 6-digit number (a PIN) or a 9-digit one (a product code) can never
  be written out as an order id. WhatsApp text messages are parsed directly
  (never OCR'd) and merged with image ids, first-seen order, de-duplicated.
  Fallback order: Order ID(s) → **name** → address → `FAILED`. When
  `extraction.cropped_id_is_complete` is on (the shipped config), a cut-off id
  showing at least `extraction.truncated_id_min_digits` digits (6 by default)
  is still the order id and wins before the name; five or fewer digits is not
  an id and falls through. A hash-prefixed Meesho-band
  number on an order screen (`ID: #342…`, or a label OCR mangled to
  `໙໕ #342…`) is accepted too, so a broken label does not cost the id. The
  address fallback also works when the "Delivery Address"
  label was cropped off: a Meesho delivery block keeps its shape (a 6-digit
  PIN line with the mobile number right below it), so that shape is the
  second chance.  The same shape gives the **name** when the customer line
  reads "<name> <mobile>" (e.g. "punitha 7418622538") — a "Contact Number -"
  or "Customer:" line is a label, never a name.
* **The order id is written exactly as it appears** — 6, 7, 8, 10 or 18
  digits, never shortened. The shipped `config.yaml` sets
  `extraction.order_id_truncate_length: 0` (no window); set a number to go
  back to a short key (the end it is taken from is
  `extraction.order_id_truncate_from`).
* **A cropped order id is a complete id** when
  `extraction.cropped_id_is_complete` is on (the shipped `config.yaml` has it
  on): the customers crop the **back** of the Meesho screen, so the visible
  "Order #342187" is all there is of that id — and it is written out as it
  stands (no `TRUNC:` prefix, no review flag) instead of being queued for
  review.
* **What a row collects** is the `extraction.collect` setting:
  * `"single"` (the shipped default) — **one** value per row, taken in this
    order: the order id, else the customer name, else the address.
  * `"all"` — the order id **and** the customer name **and** the address of
    the same screenshot, together in one row.
  The Settings view has a dropdown for it.
* **A picture that yields nothing** (no id, no name, no address) goes into
  `failed/<internal id>/` — but only when it really looks like an **order
  screen** (`ORDER DETAILS`, `Order #`, `Delivery Address`, `Cancel Order`,
  `Out for Delivery`, `Sold by`, plus the regional `ऑर्डर` / `ஆர்டர்` /
  `ఆర్డర్` / `অর্ডার`). A customer often sends a plain product picture
  (a photo of the frame); that is **not** a failed order and must never
  reach the failed list. The counters are `Orchestrator.failed_images`,
  `.skipped_pictures` and `.no_id_images`.
* **Several WhatsApp accounts** in one run: every account is a WAHA session
  (`whatsapp_api.accounts` — a list of `{name, label, enabled}`). A run
  visits every ticked account, one after the other. Each account gets its
  own **state scope**, so an internal id that was already done for one
  account is still scanned for the next one. An account whose session is not
  `WORKING` yet is skipped with a clear message, not a failure.

## 3. Directory Map (what each folder is for)

| Path | Purpose | Notes |
|---|---|---|
| `main_web.py` | **Entry point**. PyWebview shell + `Api` bridge + worker thread | modify freely |
| `ui/` | Frontend: `index.html`, `style.css`, `app.js`, `logo.png`, `app.ico` | COSMIC THEME — see §5 |
| `main_tk.py` | Old tkinter GUI, kept as reference/backup | do not delete |
| `build.bat` | PyInstaller `--onedir --windowed` build via `OrderCart.spec` → `dist\OrderCart\OrderCart.exe` | bundles `ui/`, `app.ico`; `config.yaml` stays out of the bundle (API key) |
| `config.yaml` | User settings: `whatsapp_api` (provider/base_url/api_key/session), `ocr`, `extraction` regexes+labels, `output`, `processing` | merged over `frameflow_config.DEFAULTS` |
| `frameflow_config.py` | `load_config()`, `ensure_dirs()`, defaults | light import |
| `core/orchestrator.py` | Batch loop, `ProcessResult`, `RunSummary`, `RunControl` (pause/stop), statuses | STABLE |
| `whatsapp/` | WAHA + Evolution REST client, chat-list caching, retry/backoff, media download | STABLE |
| `ocr/engine.py` | Google Lens OCR, `OCRResult` — the upload protocol is implemented in-tree | STABLE |
| `ocr/lens.py` | The Lens transport: request bytes, the POST, the answer | STABLE |
| `ocr/proto.py` | A ~200-line protobuf wire codec (no protobuf runtime) | STABLE |
| `extractor/` | `parser.extract_fields()` (label-anchored: OrderID→Name→Address priority) + `rules.py` label/regex config | STABLE |
| `state/state.py` | SQLite `processed` table (`INSERT OR REPLACE`), scoped per WhatsApp account | STABLE |
| `output/` | `NotepadWriter`, `CSVWriter` (schema: internal_id, order_id, name, address, status, source, confidence, timestamp), `corrections.py` | STABLE |
| `labels/` | Shipping-label PDF generator: 4×6 inch page per order/copy, Code128 barcode, Ship-To + Sold-By blocks | `reportlab`; pure PDF, no GUI imports |
| `updater.py` | GitHub update check (`check_for_update`) + `git pull --ff-only` (`apply_update`) | stdlib only; runner/fetch injectable for tests |
| `tests/` | pytest, 878 tests, fakes for everything | run after any change |
| `logs/`, `state/`, `assets/`, `review/`, `failed/` | runtime data dirs (created by `ensure_dirs`) | `logs/ordercart.log` is the file log |
| `review/` | Screenshots of chats that came out `TRUNC`/`FAILED` for manual fixing | "manual" source rows |
| `requirements.txt` | Pinned deps (pandas, reportlab 5.1.0, opencv, pywebview 6.2.1, pyinstaller 6.11.0, pytest 8.3.3) | **No OCR dependency** — the Lens client is in-tree |
| `VERSION`, `make_build_info.py`, `build_info.json` | Installed version + the commit a build came from | `build_info.json` is generated, git-ignored |
| `make_icon.py`, `app.ico`, `ui/app.ico` | Generated multi-size Windows icons from `ui/logo.png` | `build.bat` regenerates them when missing |
| `.gitignore` | Keeps `config.yaml` (API key), runtime folders and WAHA sessions out of git | never commit `config.yaml` |

## 4. Hard Rules for the Agent

1. **Never break the resume contract** (§2). If you touch orchestrator/state
   interaction, re-check: write output → then mark processed.
2. **Stable modules**: `whatsapp/`, `extractor/`, `ocr/`, `output/`, `core/`
   are considered frozen backend. Only change them when the task is a genuine
   backend feature, and keep the full test suite green afterwards.
3. **Orchestrator drives everything through callbacks**:
   `log_callback(msg) -> None`, `progress_callback(ProcessResult, index, total)`,
   `control: RunControl` for pause/stop. The GUIs only *consume* these.
4. **Never call `evaluate_js` with unsanitised text** — always
   `json.dumps()` the message (see `_emit_log` in main_web.py) so quotes/
   newlines can't break the JS string literal. In JS, log text is inserted
   via `textContent`, never `innerHTML`.
5. **Style follows surrounding code**: full module docstrings, type hints,
   dataclass-ish result objects, defensive try/except with log fallbacks.
6. **Tests run with**: `py -3.12 -m pytest tests/ -q` (~30 s, fakes only).

## 5. The UI (PyWebview — matte dark + pastel)

> The "cosmic" glassmorphism theme was **intentionally** replaced by a matte
> dark + pastel look (flat surfaces, no blur, Google Fonts). Do not "restore"
> the old glass cards.

- One native window, one page: `ui/index.html` + app.js + style.css
  (Plus Jakarta Sans + JetBrains Mono; subtle grid background).
- Bridge: `window.pywebview.api` → `Api` in `main_web.py`
  (start_extraction, pause, stop, get_orders, apply_correction, export_orders,
  export_orders_as, get_whatsapp_accounts, save_whatsapp_accounts,
  get_account_qr, get_failed_info, open_failed_folder,
  get/save_settings, open_logs_folder, test_image, select_image,
  get_image_data_url, check_updates, apply_update, open_repo).
- Push channel: every runtime event is pushed Python → JS via
  `window.evaluate_js(f"appendLog(...)")`, `updateStats(t,s,f)`,
  `updateProgress(index,total)`, `setRunning(running, paused)`,
  `setUpdateInfo(payload)` and `loadOrders()`.
- **Online updates**: `updater.check_for_update()` compares this install's
  commit (`build_info.json`, else `git rev-parse HEAD`) with the head of
  `Annku-z00/OrderCart@main`. `Api.check_updates()` caches the answer for an
  hour and pushes `setUpdateInfo(...)`; the UI shows a lilac banner (dismissed
  per commit in `localStorage`) and a Settings → **App updates** card.
  `Api.apply_update()` refuses while a run is active and only runs
  `git pull --ff-only` — a PyInstaller build has no `.git`, so it returns
  `needs_reinstall` and the UI opens the GitHub page instead.
  Config: `update.enabled / repo / branch / check_on_start`.
- Views: Dashboard (KPI cards, the **Collecting from** strip naming every
  ticked WhatsApp account with its live status, run progress bar, range +
  **dry-run** + **reprocess** + RUN controls, Live Terminal), Orders (matte
  table + search +
  "Needs review" filter + order counter + badges + inline Order-ID correction +
  **Full list / Copy all / Export Excel / Export Notepad / Labels shortcut / Failed folder**),
  **Labels** (a dedicated view: printable-order picker + search + select all +
  seller details + Generate PDF), Logs (full terminal), Settings
  (API url/key/session + **collect mode** + **WhatsApp account manager** +
  **App updates**),
  Test Image (single-screenshot OCR preview).
- **WhatsApp account manager** (Settings): every account is a WAHA session.
  The list shows the live status (`WORKING` / `SCAN_QR_CODE` / …) and the
  WhatsApp name behind it; the seller ticks the accounts to collect from, adds
  new ones by name, and links a phone with the **QR button** (the code is
  fetched from `GET /api/<session>/auth/qr` and shown in the app).
- **Full list** (Orders): a panel with every order as tab-separated text and
  a one-click copy. **Copy all** puts the visible rows on the clipboard
  (browser clipboard API → `execCommand` → the Python `copy_to_clipboard`
  fallback, which owns the Windows clipboard through a hidden `tkinter` root).
  **Export Excel** writes a real `.xlsx` (pandas + openpyxl), **Export
  Notepad** writes the **master-list format** — one line per internal id,
  `<id><TAB><value>` with the same one-value rule as the in-place fill (single
  order → the id, 2+ orders → the name, else the address), ids in ascending
  order, no header — so it drops straight into Notepad++; the plain
  tab-separated table is still available as `export_orders_as("txt")`.  Both
  land in `<output folder>/exports/`.
- **Labels is under development** — the OCR + order-ID extraction pipeline
  must be finished first. The sidebar entry and the Orders shortcut stay
  visible but are disabled (`LABELS_UNDER_DEVELOPMENT = true` in `ui/app.js`,
  `nav-link-dev` / `disabled` in `ui/index.html`); the view/code is kept so
  the feature can be re-enabled without rebuilding it.
- **Labels** (its own sidebar view): a de-duplicated order picker (search,
  per-row checkboxes, **Select all ready**) on the left and the seller details
  on the right. It turns the ticked rows into a 4×6 inch PDF (one label per
  order, one page per copy) with a **Code128 barcode**, a Ship-To block (name,
  address, phone) and a Sold-By block. Seller details are saved in
  `config.yaml` under `labels` (the **Save** button, or automatically when a
  PDF is generated). A shipping label needs a **customer address**, so id-only
  rows from `extraction.collect: single` are disabled and skipped; the view's
  **Use name + address** button switches to `collect: all` (then tick
  **Reprocess** and run the chats again). PDFs land in
  `<output folder>/exports/` and open automatically; **Open exports** opens
  that folder. The Orders toolbar **Labels** button is just a shortcut to this
  view.
- The **Reprocess** toggle scans chats that were already done again — it is
  the switch that makes a new extraction rule apply to the chats that were
  processed before.  Without it the resume contract skips them.
- Settings persist on Save via `Api.save_settings` → `config.yaml`; the
  Dashboard sends `start_extraction(start_id, end_id, dry_run, reprocess)` and the
  worker replies through the push channel above.
- **Review workflow**: a row needs review when the run marked it
  `ambiguous`/`partial` (session-only flags on `Api._review_flags`, surfaced
  by `get_orders`) or when it persisted as `TRUNC:`/`FAILED`. The pencil in
  the Order-ID cell opens an inline editor; **Enter saves** →
  `Api.apply_correction(id, value)` → `output.corrections.apply_corrections`
  (frozen module — called, never modified) → INFO line
  `event=correction internal_id=%s old=%s new=%s source=ui` in
  `logs/ordercart.log` → the table re-renders from the returned rows.
- Keyboard in Orders: **J/K move rows, Enter saves, Esc cancels** — the only
  shortcuts (`tests/test_web_review.py` asserts the exact key set).
- Theme palette (`ui/style.css` `:root`):
  bg `#0A0B0E`, sidebar `#121318`, card `#14161C`, input `#1B1E26`,
  text `#F4F5F8`, dim `#7E8494`, border `rgba(255,255,255,.07)`, accents
  amber `#F5C451`, lilac `#B68DF5`, lime `#CEE88C`, coral `#FF7E7E`.
- Status badge ↔ CSS class map lives in `ui/app.js` BADGE_CLASS
  (ok/trunc/partial/name = lilac-leaning, failed/missing = coral, addr = amber,
  fallback `.badge-other`); every entry needs a `.badge-*` rule in style.css.
- If a bridge call errors, `callBridge()` in app.js logs it into the terminal
  and returns `undefined` — check the terminal first when the UI misbehaves.

## 6. Known Sharp Edges / Gotchas

* `evaluate_js` only awaits a Promise when a `callback` is passed — probes
  that call async JS directly must capture results another way (e.g.
  `pywebview.api.get_orders().then(r => window.__x = ...)` then read later).
* Anaconda Python 3.12 has a cp1252 console — printing `●` etc. from probes
  requires `PYTHONIOENCODING=utf-8`.
* OCR is **Google Lens only**, and the client is **written in-tree**
  (`ocr/proto.py` + `ocr/lens.py`): a hand-rolled protobuf codec and one
  `POST` to `lensfrontend-pa.googleapis.com/v1/crupload`. There is no local
  model to bundle or warm up, no PaddleOCR/Tesseract code path anywhere — and
  no third-party OCR package in `requirements.txt` — but every OCR call needs
  internet. A Lens failure becomes an OCR failure (retryable `PARTIAL` state),
  never a silent fallback.
* The Lens request is a **protobuf** message; the field numbers live in
  `ocr/lens.py` next to the code that uses them. Changing the codec means
  changing `ocr/proto.py` — never reach for a generated `*_pb2.py` module.
* Long-running tests: none — don't assume a test needs the API running.
* Output file choice matters: the current `config.yaml` often points to
  `test_jeetu.txt` — the Orders table reads whatever `output.default_file`
  says, switching between `.csv` and `.txt` parsers automatically.
* **Labels need an address**: with `extraction.collect: single` the output
  rows carry only the order id, so the Labels view warns and skips every
  row. Switch to `collect: all` (the modal's **Use name + address** button
  does it), tick **Reprocess**, and rerun the chats; the label generator
  merges the old id-only row with the new complete row for the same order.
* Windows build: use `build.bat` (it runs `py -3.12`, adds Anaconda's
  `Library\bin` to PATH so libexpat/libffi/tcl DLLs resolve, and builds from
  `OrderCart.spec`).  The bundle contains `ui\` and `app.ico`; `config.yaml`
  is deliberately **not** bundled because it holds the WAHA API key.  Copy it
  next to `dist\OrderCart\OrderCart.exe` for a local test build.
* Everything on disk is append-only while a run is active: writers only
  ever add rows. The exceptions are `output/corrections.py`, which may
  rewrite rows - and only for IDs the user manually supplied - and
  `NotepadWriter(mode="fill")`, which rewrites the matching master-list line
  in place (that is the point of the fill layout).

## 7. Open Improvement Ideas (roadmap suggestions)

UI (PyWebview side, safely modifiable):
1. **Progress bar** in the Dashboard (python already sends index/total — the
   old tkinter GUI had one; the web UI dropped it).
2. **Review/corrections window** — the old tkinter GUI could paste correct
   order IDs for TRUNC/FAILED rows and call `output.corrections.apply_corrections`;
   ports nicely to an Orders-view "Needs review" filter + inline edit.
3. **Export/Open buttons** for review folder and failed folder (Api already
   has open_logs_folder as a template).
4. **Pause/Resume buttons in the UI** already exist — wire a keyboard
   shortcut and a "stopped/done" toast.
5. Config editor for OCR knobs (lens_timeout, lens_retries, upscale_factor, delay) —
   they're all in config.yaml already.
6. ~~A "reprocess done?" checkbox~~ — **done**: the Dashboard has a
   Reprocess toggle next to Dry Run (`start_extraction(..., reprocess)`).

Backend ideas (need care, tests mandatory):
7. **Dry-run smoke test command** (`opencode run`-style CLI without GUI).
8. Rate limiting adaptive backoff on repeated 429 (client has some already).
9. Multi-session parallel extraction (one thread per WhatsApp session) —
   big change, needs RunControl redesign.
10. Incremental CSV read for the Orders view (currently full-file parse; fine
    up to tens of thousands of rows — cap it at ~5k rows then paginate).

## 8. Acceptance checks the agent should run after changes

```bash
py -3.12 -m pytest tests/ -q          # must be 878 passed
py -3.12 -m py_compile main_web.py    # entry point compiles
node --check ui/app.js                # if node available
py -3.12 main_web.py                  # window opens, bridge connects
                                      # ("● Python engine connected" in terminal)
.\build.bat                           # optional full packaging check
```
