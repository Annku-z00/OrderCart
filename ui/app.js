/* =========================================================================
   OrderCart - app.js
   UI logic for the cosmic dashboard. All heavy lifting happens in Python
   (main_web.py) over the pywebview js_api bridge.
   ========================================================================= */
"use strict";

const $ = (id) => document.getElementById(id);

/* Labels is intentionally parked until the OCR + order-ID extraction work is
   finished.  Flip this to false (and enable the two entry points in
   index.html) when the label feature should go live again. */
const LABELS_UNDER_DEVELOPMENT = true;

/* -------------------------------------------------------------- state */
let isRunning = false;
let isPaused = false;
/* Orders view: "Needs review" filter + inline correction state */
let reviewOnly = false;
let editing = null;   /* { tr, input } while an Order ID cell is being edited */
/* Every row the last load brought in — the "Full list" shows all of them,
   regardless of the search / review filter. */
let allOrderRows = [];
/* Live-refresh timer: while a run is active the Orders table re-reads the
   output file every few seconds, so new rows appear as they are fetched. */
let ordersRefreshTimer = null;

/* ---------------------------------------------------- log terminal */
function appendLog(message) {
  const term = $("terminal");
  const termLogs = $("terminal-logs");
  const line = document.createElement("div");

  // Simple colour hints, applied by class only (content stays text-safe).
  const msg = String(message);
  if (/ERROR|Traceback|crash/i.test(msg)) line.className = "term-line err";
  else if (/WARN|dry run|Dry run/i.test(msg)) line.className = "term-line warn";
  else if (/OK\b|Done|finished|complete/i.test(msg)) line.className = "term-line ok";
  else line.className = "term-line";

  line.textContent = msg;
  term.appendChild(line);
  if (termLogs) termLogs.appendChild(line.cloneNode(true));

  // Keep the terminal from growing without bound (mirrors main_tk's cap).
  while (term.childElementCount > 4000) term.removeChild(term.firstElementChild);
  while (termLogs && termLogs.childElementCount > 4000)
    termLogs.removeChild(termLogs.firstElementChild);

  term.scrollTop = term.scrollHeight;
  if (termLogs) termLogs.scrollTop = termLogs.scrollHeight;
}

/* --------------------------------------------------------- KPI updates */
function updateStats(total, success, failed) {
  $("kpi-total").textContent = String(total);
  $("kpi-success").textContent = String(success);
  $("kpi-failed").textContent = String(failed);
}

/* Run progress bar — pushed by Api._emit_progress() as updateProgress(i, n).
   total <= 0 (unknown length yet) just leaves the bar at 0 %. */
function updateProgress(index, total) {
  const card = $("progress-card");
  const fill = $("progress-fill");
  const text = $("progress-text");
  if (!card || !fill || !text) return;

  const done = Math.max(0, parseInt(index, 10) || 0);
  const all = Math.max(0, parseInt(total, 10) || 0);
  const pct = all > 0 ? Math.min(100, Math.round((done / all) * 100)) : 0;

  fill.style.width = pct + "%";
  text.textContent = all > 0
    ? done + " / " + all + "  ·  " + pct + "%"
    : done + " processed";
  card.hidden = false;
  card.classList.toggle("done", all > 0 && done >= all);
}

/* -------------------------------------------------------- orders table */
const BADGE_CLASS = {
  ok: "badge badge-ok",
  failed: "badge badge-failed",
  trunc: "badge badge-trunc",
  partial: "badge badge-partial",
  missing: "badge badge-missing",
  name: "badge badge-name",
  addr: "badge badge-addr",
};
function badgeClass(status) {
  return BADGE_CLASS[String(status).toLowerCase()] || "badge badge-other";
}

function makeCell(cls, text) {
  const td = document.createElement("td");
  if (cls) td.className = cls;
  td.textContent = text;
  return td;
}

/* --------------------------------------------------------------------
   Review filter: a row needs review when the run flagged it
   ambiguous / partial, or when it persisted as TRUNC: / FAILED.
   -------------------------------------------------------------------- */
function rowNeedsReview(row) {
  const status = String(row.status || "").trim().toUpperCase();
  const orderId = String(row.order_id || "").trim().toUpperCase();
  return (
    row.ambiguous === true ||
    row.partial === true ||
    status === "TRUNC" ||
    status === "FAILED" ||
    orderId.startsWith("TRUNC:")
  );
}

function updateReviewCount(n) {
  const el = $("review-count");
  if (el) el.textContent = String(n);
}

/* How many orders the list holds (and how many the filters leave visible). */
function updateOrdersTotal(n, total) {
  const el = $("orders-total");
  if (!el) return;
  const count = Number(n) || 0;
  const all = Number(total) || 0;
  el.textContent = all && all !== count
    ? count + " of " + all + " orders"
    : (count === 1 ? "1 order" : count + " orders");
}

/* The Order ID cell: value + copy + inline correction button (rewritten by
   the editor while a correction is in flight — see fillOrderCell). */
function fillOrderCell(td, tr) {
  td.textContent = "";
  const val = document.createElement("span");
  val.className = "order-val";
  val.textContent = tr.dataset.orderId || "—";
  td.appendChild(val);

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "row-edit";
  copyBtn.title = "Copy this row (internal id, order id, name, address)";
  copyBtn.setAttribute(
    "aria-label",
    "Copy row for internal id " + (tr.dataset.internalId || "?")
  );
  copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i>';
  copyBtn.addEventListener("click", () => copyRow(tr));
  td.appendChild(copyBtn);

  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "row-edit";
  editBtn.title = "Correct this Order ID";
  editBtn.setAttribute(
    "aria-label",
    "Correct Order ID for internal id " + (tr.dataset.internalId || "?")
  );
  editBtn.innerHTML = '<i class="fa-solid fa-pen"></i>';
  editBtn.addEventListener("click", () => startRowEdit(tr));
  td.appendChild(editBtn);
}

function rowDate(row, mode) {
  const raw = mode === "order"
    ? row.timestamp
    : (row.last_scanned || row.timestamp);
  return String(raw || "").trim().slice(0, 10);
}

function dateMode() {
  const el = $("orders-date-mode");
  return el && el.value === "order" ? "order" : "last";
}

function renderOrderRow(row) {
  const tr = document.createElement("tr");
  tr.dataset.search = [row.internal_id, row.order_id, row.name, row.address, row.status]
    .join(" ").toLowerCase();

  const internalId = String(row.internal_id || "").trim();
  const orderId = String(row.order_id || "").trim();
  const name = String(row.name || "").trim();
  // The CSV stores a multi-line address with "|" separators.
  const rawAddress = String(row.address || "").trim();
  const address = rawAddress.split("|")
    .map((part) => part.trim()).filter(Boolean).join(", ");

  tr.dataset.internalId = internalId;
  tr.dataset.orderId = orderId;
  tr.dataset.name = name;
  tr.dataset.address = address;
  tr.dataset.rawAddress = rawAddress;
  tr.dataset.review = rowNeedsReview(row) ? "1" : "0";
  /* Two dates for the two filter modes: when the row was first written
     ("order") and when its chat was last scanned ("last", from the state DB;
     older rows fall back to the row timestamp). */
  tr.dataset.dateOrder = String(row.timestamp || "").trim().slice(0, 10);
  tr.dataset.dateLast = rowDate(row, "last");
  tr.dataset.date = tr.dataset.dateOrder;

  tr.appendChild(makeCell("cell-id", internalId || "—"));

  const orderTd = document.createElement("td");
  orderTd.className = "cell-order";
  fillOrderCell(orderTd, tr);
  tr.appendChild(orderTd);

  tr.appendChild(makeCell("cell-name", name || "—"));
  tr.appendChild(makeCell("cell-address", address || "—"));

  const statusTd = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = badgeClass(row.status);
  badge.textContent = row.status || "—";
  statusTd.appendChild(badge);

  /* The typed review flag ("MULTI: names differ" / "MULTI: name missing" /
     "MULTI: no name"): a small amber chip so the row says *which* check it
     needs, not just that it needs one. */
  const reason = String(row.review_reason || "").trim();
  if (reason) {
    const note = document.createElement("span");
    note.className = "review-note";
    note.textContent = reason;
    note.title = reason + " - open this chat and check the orders";
    statusTd.appendChild(note);
  }
  tr.appendChild(statusTd);

  return tr;
}

function renderOrders(rows) {
  const tbody = $("orders-tbody");
  tbody.innerHTML = "";
  editing = null;          // the table was rebuilt — no cell is open any more
  clearRowSelection();
  allOrderRows = Array.isArray(rows) ? rows.slice() : [];
  populateDateFilter(allOrderRows);
  updateOrdersTotal(allOrderRows.length);

  if (!rows || !rows.length) {
    const tr = document.createElement("tr");
    tr.className = "table-empty";
    tr.appendChild(makeCell("", "No orders yet — press RUN on the Dashboard."));
    tr.firstElementChild.colSpan = 5;
    tbody.appendChild(tr);
    updateReviewCount(0);
    return;
  }

  let reviewCount = 0;
  rows.forEach((row) => {
    const tr = renderOrderRow(row);
    if (tr.dataset.review === "1") reviewCount += 1;
    tbody.appendChild(tr);
  });
  updateReviewCount(reviewCount);
  filterOrders();
}

async function loadOrders() {
  const rows = await callBridge(async (a) => {
    const result = await a.get_orders();
    renderOrders(result);
    return result;
  });
  if (rows !== undefined) {
    const el = $("orders-updated");
    if (el) {
      el.textContent = "updated " + new Date().toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    }
  }
}

/* The date filter is filled from the dates actually in the file, so it
   never offers an empty date.  The current selection survives a refresh.
   "Last scanned" uses the state DB's processed timestamp, so a re-scan of
   an already-saved order still shows up under today's date. */
function populateDateFilter(rows) {
  const sel = $("orders-date");
  if (!sel) return;
  const mode = dateMode();
  const counts = new Map();
  (rows || []).forEach((row) => {
    const date = rowDate(row, mode);
    if (date) counts.set(date, (counts.get(date) || 0) + 1);
  });
  const current = sel.value;
  sel.innerHTML = "";
  sel.add(new Option("All dates", ""));
  Array.from(counts.keys()).sort().reverse().forEach((date) => {
    sel.add(new Option(date + " (" + counts.get(date) + ")", date));
  });
  sel.value = current && counts.has(current) ? current : "";
}

/* While a run is active, poll the output file so the Orders table fills up
   live.  An open inline edit or a selected row is never clobbered. */
function startOrdersAutoRefresh() {
  if (ordersRefreshTimer) return;
  ordersRefreshTimer = setInterval(() => {
    if (!isRunning) { stopOrdersAutoRefresh(); return; }
    if (editing) return;
    if (document.querySelector("#orders-tbody tr.row-selected")) return;
    loadOrders();
  }, 4000);
}

function stopOrdersAutoRefresh() {
  if (ordersRefreshTimer) {
    clearInterval(ordersRefreshTimer);
    ordersRefreshTimer = null;
  }
}

/* How many pictures the last run put into the failed/ folder. */
async function refreshFailedCount() {
  await callBridge(async (a) => {
    const info = await a.get_failed_info();
    const el = $("failed-count");
    if (el && info) el.textContent = String(info.count || 0);
  });
}

function filterOrders() {
  const term = $("orders-search").value.trim().toLowerCase();
  const dateEl = $("orders-date");
  const date = dateEl ? dateEl.value : "";
  const mode = dateMode();
  let visible = 0;
  document.querySelectorAll("#orders-tbody tr").forEach((tr) => {
    if (tr.classList.contains("table-empty")) return;
    const matchTerm = !term || (tr.dataset.search || "").includes(term);
    const matchReview = !reviewOnly || tr.dataset.review === "1";
    const rowDateValue = mode === "order" ? tr.dataset.dateOrder : tr.dataset.dateLast;
    const matchDate = !date || (rowDateValue || "") === date;
    const show = matchTerm && matchReview && matchDate;
    tr.classList.toggle("hidden", !show);
    if (show) visible += 1;
  });
  updateOrdersTotal(visible, allOrderRows.length);
  // A row hidden by the new filter must not stay selected.
  const sel = document.querySelector("#orders-tbody tr.row-selected");
  if (sel && sel.classList.contains("hidden")) clearRowSelection();
}

function toggleReviewFilter() {
  reviewOnly = !reviewOnly;
  const btn = $("btn-review-filter");
  btn.classList.toggle("active", reviewOnly);
  btn.setAttribute("aria-pressed", String(reviewOnly));
  filterOrders();
  appendLog(
    reviewOnly
      ? "— Orders: showing rows that need review —"
      : "— Orders: showing all rows —"
  );
}

/* --------------------------------------------------------------------
   Copy + export — the seller takes the list away in one click.
   -------------------------------------------------------------------- */
const ORDER_TEXT_COLUMNS = ["Internal ID", "Order ID", "Name", "Address"];

function rowToLine(row) {
  return [
    String(row.internal_id || "").trim(),
    String(row.order_id || "").trim(),
    String(row.name || "").trim(),
    String(row.address || "").trim(),
  ].join("\t");
}

function rowsToText(rows) {
  const head = ORDER_TEXT_COLUMNS.join("\t");
  const body = (rows || []).map(rowToLine).join("\n");
  return body ? head + "\n" + body : head;
}

/* The rows the table currently shows (the search / review filter applied). */
function visibleRowsData() {
  return visibleOrderRows().map((tr) => ({
    internal_id: tr.dataset.internalId || "",
    order_id: tr.dataset.orderId || "",
    name: tr.dataset.name || "",
    address: tr.dataset.address || "",
    raw_address: tr.dataset.rawAddress || "",
    timestamp: tr.dataset.dateOrder || "",
  }));
}

/* Copy a block of text to the clipboard: the browser API first, the old
   execCommand route second, the Python side (which owns the Windows
   clipboard) last. */
async function copyText(text, label) {
  const payload = String(text || "");
  if (!payload.trim()) {
    appendLog("ERROR: there is nothing to copy.");
    return false;
  }

  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(payload);
      appendLog("✓ " + (label || "Text") + " copied to the clipboard.");
      return true;
    }
  } catch (err) { /* the API can be blocked — try the next route */ }

  try {
    const area = document.createElement("textarea");
    area.value = payload;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    if (ok) {
      appendLog("✓ " + (label || "Text") + " copied to the clipboard.");
      return true;
    }
  } catch (err) { /* fall through to the Python route */ }

  const res = await callBridge((a) => a.copy_to_clipboard(payload));
  if (res && res.ok) {
    appendLog("✓ " + (label || "Text") + " copied to the clipboard.");
    return true;
  }
  appendLog("ERROR: could not copy to the clipboard.");
  return false;
}

function copyRow(tr) {
  if (!tr) return;
  const line = rowToLine({
    internal_id: tr.dataset.internalId || "",
    order_id: tr.dataset.orderId || "",
    name: tr.dataset.name || "",
    address: tr.dataset.address || "",
  });
  copyText(line, "Row");
}

async function onCopyAll() {
  const rows = visibleRowsData();
  if (!rows.length) {
    appendLog("ERROR: there is nothing to copy.");
    return;
  }
  await copyText(rowsToText(rows), rows.length + " order(s)");
}

async function onExport(format) {
  const res = await callBridge((a) => a.export_orders_as(format));
  if (res && res.ok) {
    appendLog("✓ Exported " + res.rows + " order(s) to " + res.path);
  } else {
    appendLog("ERROR: export failed — " + ((res && res.error) || "unknown error"));
  }
}

/* --- the full list overlay ------------------------------------------- */
function onFullList() {
  const rows = allOrderRows || [];
  $("full-list-text").value = rowsToText(rows);
  $("full-list-count").textContent = String(rows.length);
  $("full-list-overlay").classList.remove("hidden");
}

function closeFullList() {
  $("full-list-overlay").classList.add("hidden");
}

async function onFullListCopy() {
  await copyText($("full-list-text").value, "Full list");
}

/* --------------------------------------------------------------------
   Shipping labels — a dedicated view with its own order picker.
   The table is de-duplicated by (internal_id, order_id) and mirrors the
   generator's rules: a row is printable only with a usable Order ID *and*
   a customer address.
   -------------------------------------------------------------------- */
let labelAllRows = [];        // every order, merged + de-duplicated
let labelSelection = new Set(); // keys of the rows ticked for the PDF
let labelRows = [];           // the selection handed to the bridge

function labelRowKey(row) {
  return String(row.internal_id || "").trim() + "\u0000" +
    String(row.order_id || "").trim();
}

function labelUsableOrderId(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const upper = text.toUpperCase();
  if (upper.startsWith("TRUNC:")) return "";
  if (["FAILED", "MISSING", "NONE", "N/A", "NA", "-", "—"].includes(upper)) {
    return "";
  }
  return text;
}

function labelHasAddress(row) {
  return Boolean(String(row.raw_address || row.address || "").trim());
}

/* Merge duplicates the way labels/generator.py does: a later row with an
   address/name fills in an earlier id-only row from before a reprocess. */
function mergeLabelRows(rows) {
  const merged = new Map();
  (rows || []).forEach((row) => {
    if (!row) return;
    const orderId = labelUsableOrderId(row.order_id);
    if (!orderId) return;
    const internalId = String(row.internal_id || "").trim();
    const key = internalId + "\u0000" + orderId;
    const status = String(row.status || "").trim();
    const blocked = ["FAILED", "TRUNC", "MISSING"].includes(status.toUpperCase());
    const rawAddress = String(row.raw_address || row.address || "").trim();
    let item = merged.get(key);
    if (!item) {
      item = {
        internal_id: internalId,
        order_id: orderId,
        name: String(row.name || "").trim(),
        address: String(row.address || "").trim(),
        raw_address: rawAddress,
        status: status,
        timestamp: String(row.timestamp || "").trim(),
        _key: key,
        _ok: !blocked,
      };
      merged.set(key, item);
    } else {
      if (!blocked) item._ok = true;
      if (!item.name && row.name) item.name = String(row.name).trim();
      if (!item.address && row.address) item.address = String(row.address).trim();
      if (!item.raw_address && rawAddress) item.raw_address = rawAddress;
      if (!item.status && status) item.status = status;
      if (!item.timestamp && row.timestamp) item.timestamp = String(row.timestamp).trim();
    }
  });
  const list = Array.from(merged.values());
  list.forEach((row) => { row._ready = row._ok && labelHasAddress(row); });
  return list;
}

function labelDisplayAddress(row) {
  return String(row.raw_address || row.address || "").trim()
    .split("|").map((part) => part.trim()).filter(Boolean).join(", ");
}

function labelReadyRows() {
  return labelAllRows.filter((row) => row._ready);
}

function updateLabelCounts() {
  const ready = labelReadyRows();
  const selected = ready.filter((row) => labelSelection.has(row._key)).length;
  $("labels-ready-count").textContent = ready.length + " ready";
  $("labels-selected-count").textContent = selected + " selected";

  const box = $("labels-select-all");
  box.disabled = ready.length === 0;
  box.checked = ready.length > 0 && selected === ready.length;
  box.indeterminate = selected > 0 && selected < ready.length;
}

function updateLabelEmptyMessage() {
  const msg = $("labels-msg");
  const ready = labelReadyRows().length;
  if (!ready) {
    msg.textContent = labelAllRows.length
      ? "⚠ None of these orders has a customer address. Press Use name + address, " +
        "tick Reprocess on the Dashboard, and run the chats again."
      : "No orders yet — press RUN on the Dashboard first.";
    return;
  }
  // Only the "no addresses" warning is cleared here; a generation result
  // printed by onGenerateLabels stays visible.
  if (msg.textContent.startsWith("⚠ None of these orders")) msg.textContent = "";
}

function renderLabelTable() {
  const tbody = $("labels-tbody");
  tbody.innerHTML = "";
  const term = $("labels-search").value.trim().toLowerCase();
  const visible = labelAllRows.filter((row) => {
    if (!term) return true;
    return [row.internal_id, row.order_id, row.name, row.raw_address, row.status]
      .join(" ").toLowerCase().includes(term);
  });

  if (!visible.length) {
    const tr = document.createElement("tr");
    tr.className = "table-empty";
    const td = document.createElement("td");
    td.colSpan = 5;
    td.textContent = labelAllRows.length
      ? "No order matches this search."
      : "No orders yet — press RUN on the Dashboard first.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    updateLabelCounts();
    updateLabelEmptyMessage();
    return;
  }

  visible.forEach((row) => {
    const tr = document.createElement("tr");
    tr.dataset.key = row._key;
    if (!row._ready) tr.classList.add("labels-row-disabled");

    const pickTd = document.createElement("td");
    pickTd.className = "labels-pick-col";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "labels-row-check";
    box.disabled = !row._ready;
    box.checked = row._ready && labelSelection.has(row._key);
    box.title = row._ready
      ? "Include this order in the PDF"
      : "No usable Order ID or no customer address";
    box.setAttribute(
      "aria-label",
      "Include order " + (row.order_id || "?") + " in the PDF"
    );
    box.addEventListener("change", () => {
      if (box.checked) labelSelection.add(row._key);
      else labelSelection.delete(row._key);
      updateLabelCounts();
      updateLabelEmptyMessage();
    });
    pickTd.appendChild(box);
    tr.appendChild(pickTd);

    const orderTd = document.createElement("td");
    orderTd.className = "cell-order";
    orderTd.textContent = row.order_id;
    tr.appendChild(orderTd);

    tr.appendChild(makeCell("cell-name", row.name || "—"));

    const addressTd = makeCell("cell-address", labelDisplayAddress(row) || "—");
    if (!labelHasAddress(row)) addressTd.classList.add("labels-address-missing");
    tr.appendChild(addressTd);

    const statusTd = document.createElement("td");
    const badge = document.createElement("span");
    if (!labelHasAddress(row)) {
      badge.className = "badge badge-missing";
      badge.textContent = "NO ADDRESS";
    } else {
      badge.className = badgeClass(row.status);
      badge.textContent = row.status || "OK";
    }
    statusTd.appendChild(badge);
    tr.appendChild(statusTd);

    tbody.appendChild(tr);
  });

  updateLabelCounts();
  updateLabelEmptyMessage();
}

async function loadLabelSettings() {
  const cfg = await callBridge((a) => a.get_label_settings());
  if (!cfg) return;
  $("label-brand").value = cfg.brand || "MEESHO";
  $("label-seller-name").value = cfg.seller_name || "";
  $("label-seller-address").value = cfg.seller_address || "";
  $("label-seller-phone").value = cfg.seller_phone || "";
  $("label-seller-gstin").value = cfg.seller_gstin || "";
  $("label-footer").value = cfg.footer || "";
  $("label-copies").value = String(cfg.copies || 1);
  $("label-barcode").checked = cfg.show_barcode !== false;
}

async function loadLabelsView() {
  $("labels-msg").textContent = "";
  const rows = await callBridge((a) => a.get_orders());
  if (rows === undefined) {
    updateLabelEmptyMessage();
    return;
  }
  labelAllRows = mergeLabelRows(Array.isArray(rows) ? rows : []);
  // Opening the view selects every printable order; the seller unticks the
  // few that should not be printed.
  labelSelection = new Set(labelReadyRows().map((row) => row._key));
  renderLabelTable();
  await loadLabelSettings();
}

function labelSettings() {
  return {
    brand: $("label-brand").value.trim(),
    seller_name: $("label-seller-name").value.trim(),
    seller_address: $("label-seller-address").value.trim(),
    seller_phone: $("label-seller-phone").value.trim(),
    seller_gstin: $("label-seller-gstin").value.trim(),
    footer: $("label-footer").value.trim(),
    copies: parseInt($("label-copies").value, 10) || 1,
    show_barcode: $("label-barcode").checked,
  };
}

function onLabelSelectAll() {
  if ($("labels-select-all").checked) {
    labelSelection = new Set(labelReadyRows().map((row) => row._key));
  } else {
    labelSelection.clear();
  }
  renderLabelTable();
}

async function onSaveLabelSettings() {
  const res = await callBridge((a) => a.save_label_settings(labelSettings()));
  if (res && res.ok) {
    $("labels-msg").textContent = "✓ Seller details saved.";
    appendLog("— Labels: seller details saved —");
  } else {
    const msg = (res && res.error) || "could not save the seller details";
    $("labels-msg").textContent = "✗ " + msg;
    appendLog("ERROR: " + msg);
  }
}

async function onCollectAll() {
  const res = await callBridge((a) => a.set_collect_mode("all"));
  if (res && res.ok) {
    $("set-collect").value = "all";
    $("labels-msg").textContent =
      "✓ Collect per row is now All. Tick Reprocess on the Dashboard and run the chats again " +
      "so every row carries the customer name and address; then generate the labels.";
    appendLog("— Settings: Collect per row → All (reprocess the chats to fill name/address) —");
  } else {
    const msg = (res && res.error) || "could not change the collect mode";
    $("labels-msg").textContent = "✗ " + msg;
    appendLog("ERROR: " + msg);
  }
}

async function onGenerateLabels() {
  labelRows = labelReadyRows().filter((row) => labelSelection.has(row._key));
  if (!labelRows.length) {
    $("labels-msg").textContent = "Select at least one order with an address.";
    return;
  }
  const btn = $("btn-labels-generate");
  btn.disabled = true;
  $("labels-msg").textContent = "Generating " + labelRows.length + " label(s)…";

  const res = await callBridge((a) => a.generate_labels(labelRows, labelSettings()));
  btn.disabled = false;

  if (!res) {
    $("labels-msg").textContent = "✗ Generation failed — see the terminal.";
    return;
  }
  if (!res.ok) {
    const msg = res.error || "unknown error";
    $("labels-msg").textContent = "✗ " + msg;
    appendLog("ERROR: labels — " + msg);
    return;
  }

  const skipped = Number(res.skipped || 0);
  const incomplete = Number(res.incomplete || 0);
  $("labels-msg").textContent =
    "✓ " + res.labels + " label(s), " + res.pages + " page(s) → " + res.path;
  appendLog("✓ Labels: " + res.labels + " order(s) → " + res.path);
  if (skipped) {
    appendLog("— Labels: skipped " + skipped + " row(s) without a usable Order ID");
  }
  if (incomplete) {
    appendLog(
      "— Labels: skipped " + incomplete + " row(s) without a customer address — " +
      "set Settings → Collect per row → All, tick Reprocess, and run again"
    );
  }
}

/* --------------------------------------------------------------------
   Row selection — keyboard J / K only (no other shortcuts).
   -------------------------------------------------------------------- */
function visibleOrderRows() {
  return Array.from(document.querySelectorAll("#orders-tbody tr")).filter(
    (tr) => !tr.classList.contains("table-empty") && !tr.classList.contains("hidden")
  );
}

function clearRowSelection() {
  document
    .querySelectorAll("#orders-tbody tr.row-selected")
    .forEach((tr) => tr.classList.remove("row-selected"));
}

function moveRowSelection(delta) {
  if ($("view-orders").classList.contains("hidden")) return;
  const rows = visibleOrderRows();
  if (!rows.length) return;
  const current = document.querySelector("#orders-tbody tr.row-selected");
  let idx = rows.indexOf(current);
  idx = idx < 0 ? (delta > 0 ? 0 : rows.length - 1) : idx + delta;
  idx = Math.max(0, Math.min(rows.length - 1, idx));
  clearRowSelection();
  rows[idx].classList.add("row-selected");
  rows[idx].scrollIntoView({ block: "nearest" });
}

/* --------------------------------------------------------------------
   Inline correction — click the pencil, Enter saves, Esc cancels.
   -------------------------------------------------------------------- */
function startRowEdit(tr) {
  if (!tr || tr.classList.contains("table-empty")) return;
  if (editing) cancelRowEdit();
  const cell = tr.querySelector(".cell-order");
  if (!cell) return;

  const current = tr.dataset.orderId || "";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "row-edit-input";
  input.value = current.toUpperCase().startsWith("TRUNC:") ? current.slice(6) : current;
  input.placeholder = "paste the real Order ID";
  input.setAttribute("aria-label", "Corrected Order ID");

  cell.textContent = "";
  cell.appendChild(input);
  input.focus();
  input.select();
  editing = { tr, input };
}

function cancelRowEdit() {
  if (!editing) return;
  const { tr } = editing;
  editing = null;
  const cell = tr.querySelector(".cell-order");
  if (cell) fillOrderCell(cell, tr);
}

async function saveRowEdit() {
  if (!editing) return;
  const { tr, input } = editing;
  const internalId = tr.dataset.internalId || "";
  const value = input.value.trim();
  /* The value the row holds right now - a chat can carry several orders
     (one row each), so the correction is pinned to this row. */
  const currentValue = tr.dataset.orderId || "";

  if (!internalId || !value) { cancelRowEdit(); return; }

  input.disabled = true;
  editing = null;                      // one save per edit session

  const res = await callBridge((a) => a.apply_correction(internalId, value, currentValue));
  if (!res) {
    // Bridge error (already logged) — reload so the cell leaves edit mode.
    loadOrders();
    return;
  }
  if (res.ok) {
    appendLog(
      "✓ Correction saved: " + internalId + " → " + res.order_id +
      (res.changed ? "" : " (no matching row in the output file)")
    );
  } else {
    appendLog("ERROR: correction failed — " + (res.error || "unknown error"));
  }
  // Orders table refreshes immediately with the server-side rows.
  if (Array.isArray(res.orders)) renderOrders(res.orders);
  else loadOrders();
}

function setStatusRunning(mode) {
  // mode: "idle" | "running" | "paused" | "stopped"
  const pill = $("engine-status");
  const text = $("engine-text");
  pill.classList.remove("running", "paused");
  if (mode === "running") { pill.classList.add("running"); text.textContent = "Running…"; }
  else if (mode === "paused") { pill.classList.add("paused"); text.textContent = "Paused"; }
  else if (mode === "stopped") { text.textContent = "Stopped"; }
  else text.textContent = "Idle";
}

function setRunning(running, paused) {
  const wasRunning = isRunning;
  isRunning = running;
  isPaused = !!paused;
  $("btn-run").disabled = running;
  $("btn-pause").disabled = !running;
  $("btn-stop").disabled = !running;
  $("start-id").disabled = running;
  $("end-id").disabled = running;
  $("dry-run").disabled = running;
  $("reprocess").disabled = running;

  $("btn-pause").innerHTML = running && isPaused
    ? '<i class="fa-solid fa-play"></i>&nbsp;Resume'
    : '<i class="fa-solid fa-pause"></i>&nbsp;Pause';
  $("btn-pause").classList.toggle("btn-paused", running && isPaused);

  setStatusRunning(running ? (isPaused ? "paused" : "running") : "idle");

  // A fresh run starts the progress bar over — the card hides again until the
  // orchestrator's first tick re-reveals it with real numbers.
  if (running && !wasRunning) {
    const fill = $("progress-fill");
    const text = $("progress-text");
    const card = $("progress-card");
    if (fill) fill.style.width = "0%";
    if (text) text.textContent = "0 / 0";
    if (card) card.hidden = true;
  }

  // Keep the Orders table live while the run is writing rows.
  if (running) startOrdersAutoRefresh();
  else stopOrdersAutoRefresh();

  // A run just finished (or was stopped) - refresh the orders table so the
  // freshly extracted rows appear immediately.
  if (wasRunning && !running) loadOrders();
}

/* ----------------------------------------------------------- bridge ---- */
function api() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

async function callBridge(fn) {
  const a = api();
  if (!a) { appendLog("ERROR: Python engine not connected yet."); return undefined; }
  try { return await fn(a); }
  catch (err) { appendLog("ERROR: " + (err && err.message ? err.message : String(err))); return undefined; }
}

/* ------------------------------------------------------------- actions */
function onStart() {
  if (isRunning) return;
  const start = parseInt($("start-id").value, 10);
  const end = parseInt($("end-id").value, 10);

  if (Number.isNaN(start) || Number.isNaN(end)) {
    appendLog("ERROR: Start ID and End ID must be whole numbers.");
    return;
  }
  setRunning(true, false);
  const dryRun = $("dry-run").checked;
  const reprocess = $("reprocess").checked;
  appendLog(
    `▶ Starting extraction ${start} → ${end}` +
    `${dryRun ? " (dry run)" : ""}${reprocess ? " (reprocess)" : ""} …`
  );
  callBridge((a) => a.start_extraction(start, end, dryRun, reprocess));
}

function onPause() {
  if (!isRunning) return;
  callBridge((a) => a.pause());
}

function onStop() {
  if (!isRunning) return;
  appendLog("■ Stop requested — finishing the current chat…");
  callBridge((a) => a.stop());
}

function onClearLog() {
  $("terminal").innerHTML = "";
  const tl = $("terminal-logs");
  if (tl) tl.innerHTML = "";
}

function onSaveSettings() {
  const url = $("set-url").value.trim();
  const key = $("set-key").value;
  const session = $("set-session").value.trim();
  const collect = $("set-collect").value;
  callBridge(async (a) => {
    const res = await a.save_settings(url, key, session, collect);
    $("settings-msg").textContent = res && res.ok ? "✓ Saved to config.yaml" : "✗ Could not save";
    setTimeout(() => { $("settings-msg").textContent = ""; }, 3500);
    if (res && res.ok) {
      appendLog(`— Settings saved (${url}, session="${session}", collect="${collect}") —`);
    }
  });
}

/* --------------------------------------------------------------------
   WhatsApp accounts — tick the ones to collect from.
   -------------------------------------------------------------------- */
let accountRows = [];   /* the accounts as the server knows them */

function accountStatusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "WORKING" || s === "STARTED") return "acc-dot acc-ok";
  if (s === "SCAN_QR_CODE" || s === "SCAN_QR") return "acc-dot acc-warn";
  if (!s) return "acc-dot acc-muted";
  return "acc-dot acc-muted";
}

function renderAccounts(accounts) {
  const list = $("accounts-list");
  list.innerHTML = "";
  accountRows = Array.isArray(accounts) ? accounts.slice() : [];

  if (!accountRows.length) {
    const empty = document.createElement("div");
    empty.className = "dim";
    empty.textContent = "No WhatsApp account yet — add one below.";
    list.appendChild(empty);
    return;
  }

  accountRows.forEach((account, index) => {
    const row = document.createElement("div");
    row.className = "account-row";

    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "account-check";
    box.checked = account.enabled === true;
    box.setAttribute(
      "aria-label", "Collect orders from " + (account.label || account.name)
    );
    box.addEventListener("change", () => {
      accountRows[index].enabled = box.checked;
    });
    row.appendChild(box);

    const dot = document.createElement("span");
    dot.className = accountStatusClass(account.status);
    dot.title = account.status || "not linked";
    row.appendChild(dot);

    const label = document.createElement("span");
    label.className = "account-label";
    label.textContent = account.label || account.name;
    row.appendChild(label);

    const name = document.createElement("span");
    name.className = "account-name dim";
    name.textContent = account.name;
    row.appendChild(name);

    const me = document.createElement("span");
    me.className = "account-me dim";
    me.textContent = account.me || (account.known ? "" : "not on the server");
    row.appendChild(me);

    const link = document.createElement("button");
    link.type = "button";
    link.className = "row-edit";
    link.title = "Show the QR code to link this WhatsApp account";
    link.setAttribute("aria-label", "Link " + (account.label || account.name));
    link.innerHTML = '<i class="fa-solid fa-qrcode"></i>';
    link.addEventListener("click", () => showAccountQr(account));
    row.appendChild(link);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-edit";
    remove.title = "Remove this account";
    remove.setAttribute("aria-label", "Remove " + (account.label || account.name));
    remove.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    remove.addEventListener("click", () => {
      accountRows.splice(index, 1);
      renderAccounts(accountRows);
    });
    row.appendChild(remove);

    list.appendChild(row);
  });
}

async function loadAccounts() {
  await callBridge(async (a) => {
    const info = await a.get_whatsapp_accounts();
    if (info) renderAccounts(info.accounts || []);
  });
}

async function saveAccounts() {
  const payload = accountRows.map((a) => ({
    name: a.name,
    label: a.label,
    enabled: a.enabled === true,
  }));
  const res = await callBridge((a) => a.save_whatsapp_accounts(payload));
  const msg = $("accounts-msg");
  if (res && res.ok) {
    msg.textContent = "✓ Saved";
    appendLog("— WhatsApp accounts saved: " +
      payload.filter((p) => p.enabled).map((p) => p.label).join(", ") + " —");
  } else {
    msg.textContent = "✗ " + ((res && res.error) || "could not save");
  }
  setTimeout(() => { if (msg) msg.textContent = ""; }, 3500);
}

/* Show the QR code of one account so the seller can link the phone. */
async function showAccountQr(account) {
  const name = (account && account.name) || "";
  $("qr-account-name").textContent = account.label || name;
  $("qr-error").textContent = "";
  const img = $("qr-image");
  img.removeAttribute("src");
  // Restarting a dead session takes a few seconds - say so instead of
  // leaving the overlay blank.
  $("qr-error").textContent =
    "Waiting for a fresh QR code — this can take up to 30 seconds…";
  $("qr-overlay").classList.remove("hidden");

  const res = await callBridge((a) => a.get_account_qr(name));
  if (res && res.ok && res.data_url) {
    img.src = res.data_url;
    $("qr-error").textContent = "";
  } else {
    $("qr-error").textContent =
      "No QR code: " + ((res && (res.error || res.hint)) || "unknown error");
  }
}

function closeAccountQr() {
  $("qr-overlay").classList.add("hidden");
  const img = $("qr-image");
  img.removeAttribute("src");
}

async function onAddAccount() {
  const name = $("account-new-name").value.trim();
  const label = $("account-new-label").value.trim() || name;
  if (!name) {
    appendLog("ERROR: give the new account a session name.");
    return;
  }
  if (accountRows.some((a) => a.name === name)) {
    appendLog("ERROR: the account '" + name + "' is already in the list.");
    return;
  }
  // The account is created on the WhatsApp server right away, so its QR code
  // is ready to scan.
  const res = await callBridge((a) => a.add_whatsapp_account(name, label));
  if (!res || !res.ok) {
    appendLog("ERROR: could not add the account — " +
      ((res && res.error) || "unknown error"));
    return;
  }
  $("account-new-name").value = "";
  $("account-new-label").value = "";
  appendLog("— Account '" + res.label + "' added (" + res.name + ")" +
    (res.status ? " — " + res.status : "") + " —");
  loadAccounts();
}

/* ------------------------------------------------- Test Image view --- */
/* Single-image playground: pick a screenshot → OCR + extract → show it. */

const TEST_IMAGE_BADGE = {
  OK: "badge badge-ok",
  TRUNC: "badge badge-trunc",
  NAME: "badge badge-name",
  ADDR: "badge badge-addr",
  FAILED: "badge badge-failed",
};

function setTestImageBadge(status) {
  const badge = $("test-image-badge");
  const key = String(status || "").toUpperCase();
  badge.className = TEST_IMAGE_BADGE[key] || "badge badge-other";
  badge.textContent = status || "—";
}

/* Show exactly one of: loader / result / empty / error. */
function setTestImagePanel(which) {
  $("test-image-loader").classList.toggle("hidden", which !== "loading");
  $("test-image-result").classList.toggle("hidden", which !== "result");
  $("test-image-empty").classList.toggle("hidden", which !== "empty");
  $("test-image-error").classList.toggle("hidden", which !== "error");
}

function clearTestImagePreview() {
  const img = $("test-image-preview");
  img.classList.add("hidden");
  img.removeAttribute("src");
  $("test-image-placeholder").classList.remove("hidden");
}

function showTestImageError(message) {
  setTestImagePanel("error");
  $("test-image-error").textContent = String(message || "Could not process the image.");
}

function resetTestImageView() {
  clearTestImagePreview();
  $("test-image-path").textContent = "";
  $("test-image-order-id").textContent = "—";
  $("test-image-name").textContent = "—";
  $("test-image-address").textContent = "—";
  $("test-image-confidence").textContent = "—";
  $("test-image-raw").textContent = "—";
  $("test-image-error").textContent = "";
  setTestImageBadge("");
  $("btn-select-image").disabled = false;
  setTestImagePanel("empty");
}

async function onSelectImage() {
  const a = api();
  if (!a) { appendLog("ERROR: Python engine not connected yet."); return; }

  let path = null;
  try {
    path = await a.select_image();
  } catch (err) {
    appendLog("ERROR: " + (err && err.message ? err.message : String(err)));
    return;
  }
  // Cancelled file dialog -> silent no-op (no error shown).
  if (!path) return;

  // 1) Preview (data URL: the page is served over http, so file:// is blocked).
  clearTestImagePreview();
  try {
    const dataUrl = await a.get_image_data_url(path);
    if (dataUrl) {
      const img = $("test-image-preview");
      img.src = dataUrl;
      img.classList.remove("hidden");
      $("test-image-placeholder").classList.add("hidden");
    }
  } catch (err) { /* preview is best-effort — ignore */ }

  $("test-image-path").textContent = String(path);

  // 2) Loading state while Python runs OCR in the background.
  $("btn-select-image").disabled = true;
  setTestImagePanel("loading");
  appendLog("▶ Test Image: reading " + String(path).split(/[\\/]/).pop() + " …");

  let result = null;
  try {
    result = await a.test_image(path);
  } catch (err) {
    $("btn-select-image").disabled = false;
    showTestImageError("Error: " + (err && err.message ? err.message : String(err)));
    return;
  }
  $("btn-select-image").disabled = false;

  if (!result || !result.success) {
    showTestImageError((result && result.error) || "Could not process the image.");
    appendLog("ERROR: Test Image failed — " + ((result && result.error) || "unknown error"));
    return;
  }

  // 3) Populate the results panel.
  $("test-image-order-id").textContent = result.order_id || "—";
  $("test-image-name").textContent = result.name || "—";
  $("test-image-address").textContent = result.address || "—";
  $("test-image-confidence").textContent =
    typeof result.confidence === "number"
      ? Math.round(result.confidence * 100) + "%"
      : "—";
  $("test-image-raw").textContent = result.raw_text || "(no text detected)";
  setTestImageBadge(result.status || "FAILED");
  setTestImagePanel("result");
  appendLog("— Test Image " + (result.status || "") + ": " + (result.order_id || "no order id") + " —");
}

/* ----------------------------------------------------- view switching */
const VIEWS = ["dashboard", "orders", "labels", "test-image", "logs", "settings"];

function switchView(name) {
  if (name === "labels" && LABELS_UNDER_DEVELOPMENT) {
    appendLog("— Labels is under development — finish OCR + order-ID extraction first —");
    return;
  }
  document.querySelectorAll(".nav-link").forEach((el) =>
    el.classList.toggle("active", el.dataset.view === name)
  );
  VIEWS.forEach((v) =>
    $("view-" + v).classList.toggle("hidden", v !== name)
  );
  // Load fresh data every time the Orders view is opened.
  if (name === "orders") {
    loadOrders();
    refreshFailedCount();
  }
  // The label picker reads the same order file, plus the seller details.
  if (name === "labels") loadLabelsView();
  // The account list is only worth reading when the Settings view opens.
  if (name === "settings") loadAccounts();
}

/* ------------------------------------------------------- online updates */
/* Python pushes the result with setUpdateInfo(...) (from check_updates), and
   the Settings button / page load ask for a check through the bridge.  Text is
   always inserted with textContent, never innerHTML. */

const UPDATE_DISMISS_KEY = "ordercart.update.dismissed";
let updateInfo = null;

function updateText(info) {
  if (!info) return "not checked yet";
  if (info.disabled) return "automatic checks are off (config.yaml → update.enabled)";
  if (!info.ok) return "check failed — " + (info.error || "unknown error");
  if (info.update_available) {
    return "update available — " + (info.message || "newer commit on GitHub");
  }
  if (!info.current) {
    return "up to date (this build has no commit stamp)";
  }
  return "up to date — checked " + (info.checked_at || "");
}

function updateDismissed(sha) {
  try {
    return localStorage.getItem(UPDATE_DISMISS_KEY) === String(sha || "");
  } catch (e) {
    return false;
  }
}

function renderUpdate() {
  const info = updateInfo;
  if (info && info.version) $("app-version").textContent = info.version;

  // Settings card — reflects the last check whenever it happened.
  if (info) {
    $("update-current").textContent =
      info.current_short || info.current || info.version || "—";
    $("update-latest").textContent = info.latest_short || "—";
    $("update-status").textContent = updateText(info);
    $("btn-apply-update").hidden =
      !(info.ok && info.update_available && info.can_update);
  }

  // Banner — only when a *newer* commit is known and not dismissed.
  const banner = $("update-banner");
  if (!banner) return;
  const show = !!(
    info && info.ok && info.update_available && !updateDismissed(info.latest)
  );
  if (!show) {
    banner.classList.add("hidden");
    return;
  }
  const label = info.latest_short ? "(" + info.latest_short + ") " : "";
  $("update-banner-text").textContent =
    "Update available " + label + (info.message || "a newer commit is on GitHub");
  $("update-banner-apply").hidden = !info.can_update;
  banner.classList.remove("hidden");
}

function setUpdateInfo(info) {
  updateInfo = info || null;
  renderUpdate();
}

function dismissUpdate() {
  if (updateInfo && updateInfo.latest) {
    try {
      localStorage.setItem(UPDATE_DISMISS_KEY, String(updateInfo.latest));
    } catch (e) { /* private mode — the banner just comes back next time */ }
  }
  const banner = $("update-banner");
  if (banner) banner.classList.add("hidden");
}

async function checkUpdates(force) {
  $("updates-msg").textContent = "checking…";
  const res = await callBridge((a) => a.check_updates(!!force));
  $("updates-msg").textContent = "";
  if (!res) return;
  setUpdateInfo(res);
  if (res.disabled) {
    appendLog("Update check is off (config.yaml → update.enabled).");
  } else if (!res.ok) {
    appendLog("WARN: update check — " + (res.error || "unknown error"));
  } else if (res.update_available) {
    appendLog(
      "Update available: " + (res.latest_short || "") + " on " + res.branch +
      " — " + (res.message || "")
    );
  } else {
    appendLog("● OrderCart is up to date (" + (res.current_short || res.version) + ").");
  }
}

async function applyUpdate() {
  $("updates-msg").textContent = "updating…";
  const res = await callBridge((a) => a.apply_update());
  if (!res) { $("updates-msg").textContent = ""; return; }
  if (!res.ok) {
    $("updates-msg").textContent = res.needs_reinstall
      ? "This is a built copy — download the newest release from GitHub."
      : (res.error || "update failed");
    appendLog("WARN: update — " + (res.error || "unknown error"));
    if (res.needs_reinstall) openRepo();
    return;
  }
  $("updates-msg").textContent = "Updated — restart OrderCart to use it.";
  appendLog("Update downloaded — close and reopen OrderCart to use it.");
  const banner = $("update-banner");
  if (banner) banner.classList.add("hidden");
}

function openRepo() {
  callBridge((a) => a.open_repo());
}

/* ------------------------------------------------------- status banner */
/* One non-blocking banner for "WhatsApp API offline" and for config.yaml
   problems.  It never blocks a run - it only tells the seller what is wrong. */

function showBanner(text, kind) {
  const banner = $("app-banner");
  if (!banner) return;
  banner.classList.toggle("error", kind === "error");
  $("app-banner-text").textContent = text;
  banner.classList.remove("hidden");
}

function hideBanner() {
  const banner = $("app-banner");
  if (banner) banner.classList.add("hidden");
}

async function checkConnection(quiet) {
  const res = await callBridge((a) => a.check_connection());
  if (!res) return;
  if (res.ok) {
    hideBanner();
    if (!quiet) appendLog("● WhatsApp API reachable — " + (res.base_url || ""));
    return;
  }
  showBanner(
    "WhatsApp API is not reachable at " + (res.base_url || "the configured URL") +
    ". Start WAHA (or fix the URL in Settings), then press Retry." +
    (res.error ? " (" + res.error + ")" : ""),
    "error"
  );
}

/* --------------------------------------------------------------- wiring */
document.addEventListener("DOMContentLoaded", () => {
  $("btn-run").addEventListener("click", onStart);
  $("btn-pause").addEventListener("click", onPause);
  $("btn-stop").addEventListener("click", onStop);
  $("btn-clear-log").addEventListener("click", onClearLog);
  $("btn-save-settings").addEventListener("click", onSaveSettings);
  $("btn-open-logs-folder").addEventListener("click", () =>
    callBridge((a) => a.open_logs_folder())
  );
  $("btn-refresh-orders").addEventListener("click", loadOrders);
  $("btn-select-image").addEventListener("click", onSelectImage);
  $("btn-clear-image").addEventListener("click", resetTestImageView);
  $("btn-open-csv").addEventListener("click", () =>
    callBridge((a) => a.export_orders())
  );
  $("btn-full-list").addEventListener("click", onFullList);
  $("btn-full-list-close").addEventListener("click", closeFullList);
  $("btn-full-list-copy").addEventListener("click", onFullListCopy);
  $("btn-full-list-download").addEventListener("click", () => onExport("notepad"));
  $("btn-copy-all").addEventListener("click", onCopyAll);
  $("btn-export-xlsx").addEventListener("click", () => onExport("xlsx"));
  $("btn-export-txt").addEventListener("click", () => onExport("notepad"));
  // From Orders, the Labels button jumps to the dedicated view.
  $("btn-labels").addEventListener("click", () => switchView("labels"));
  $("btn-labels-refresh").addEventListener("click", loadLabelsView);
  $("btn-labels-open-folder").addEventListener("click", () =>
    callBridge((a) => a.open_labels_folder())
  );
  $("btn-labels-save").addEventListener("click", onSaveLabelSettings);
  $("btn-labels-collect-all").addEventListener("click", onCollectAll);
  $("btn-labels-generate").addEventListener("click", onGenerateLabels);
  $("labels-select-all").addEventListener("change", onLabelSelectAll);
  $("labels-search").addEventListener("input", renderLabelTable);
  $("btn-refresh-orders-list").addEventListener("click", () => {
    loadOrders();
    refreshFailedCount();
    appendLog("— Orders reloaded —");
  });
  $("btn-open-failed").addEventListener("click", () =>
    callBridge((a) => a.open_failed_folder())
  );
  $("full-list-overlay").addEventListener("click", (e) => {
    if (e.target === $("full-list-overlay")) closeFullList();
  });
  $("orders-search").addEventListener("input", filterOrders);
  $("orders-date").addEventListener("change", filterOrders);
  $("orders-date-mode").addEventListener("change", () => {
    populateDateFilter(allOrderRows);
    filterOrders();
  });
  $("btn-review-filter").addEventListener("click", toggleReviewFilter);
  $("btn-add-account").addEventListener("click", onAddAccount);
  $("btn-save-accounts").addEventListener("click", saveAccounts);
  $("btn-refresh-accounts").addEventListener("click", loadAccounts);
  $("btn-qr-close").addEventListener("click", closeAccountQr);
  $("qr-overlay").addEventListener("click", (e) => {
    if (e.target === $("qr-overlay")) closeAccountQr();
  });
  $("app-banner-close").addEventListener("click", hideBanner);
  $("app-banner-retry").addEventListener("click", () => {
    hideBanner();
    checkConnection();
    loadAccounts();
  });

  // Online updates (GitHub).
  $("btn-check-updates").addEventListener("click", () => checkUpdates(true));
  $("btn-apply-update").addEventListener("click", applyUpdate);
  $("btn-open-github").addEventListener("click", openRepo);
  $("update-banner-apply").addEventListener("click", applyUpdate);
  $("update-banner-notes").addEventListener("click", openRepo);
  $("update-banner-close").addEventListener("click", dismissUpdate);

  document.querySelectorAll(".nav-link").forEach((el) =>
    el.addEventListener("click", (e) => {
      e.preventDefault();
      switchView(el.dataset.view);
    })
  );

  $("dry-run").addEventListener("change", (e) => {
    $("dry-run-text").textContent = e.target.checked ? "On" : "Off";
  });

  $("reprocess").addEventListener("change", (e) => {
    $("reprocess-text").textContent = e.target.checked ? "On" : "Off";
  });

  // Press Enter in the ID fields = RUN
  ["start-id", "end-id"].forEach((id) =>
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") onStart(); })
  );

  /* Review keyboard: J/K move rows, Enter saves, Esc cancels. Nothing else. */
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "Enter") {
      if (editing) { e.preventDefault(); saveRowEdit(); }
      return;
    }
    if (e.key === "Escape") {
      if (editing) { e.preventDefault(); cancelRowEdit(); }
      else if (!$("qr-overlay").classList.contains("hidden")) {
        e.preventDefault(); closeAccountQr();
      } else if (!$("full-list-overlay").classList.contains("hidden")) {
        e.preventDefault(); closeFullList();
      } else clearRowSelection();
      return;
    }

    // Text fields keep every keystroke (search box, or the editor input —
    // where Enter/Esc were handled above).
    const tag = ((e.target && e.target.tagName) || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (editing) return;

    if (e.key === "j" || e.key === "J") { e.preventDefault(); moveRowSelection(1); }
    else if (e.key === "k" || e.key === "K") { e.preventDefault(); moveRowSelection(-1); }
  });
});

/* pywebview fires this on the injected bridge being ready */
function pywebviewReady() {
  const btn = $("btn-run");
  btn.disabled = false;
  btn.innerHTML = '<i class="fa-solid fa-play"></i>&nbsp;RUN';
  appendLog("● Python engine connected.");
  refreshFailedCount();
  // Pull the saved API settings into the Settings view.
  callBridge(async (a) => {
    const cfg = await a.get_settings();
    if (cfg) {
      $("set-url").value = (cfg.base_url || "").trim();
      $("set-key").value = (cfg.api_key || "").trim();
      $("set-session").value = (cfg.session_name || "default").trim();
      $("set-collect").value = (cfg.collect || "single").trim();
      if (Array.isArray(cfg.config_problems) && cfg.config_problems.length) {
        showBanner("config.yaml: " + cfg.config_problems.join(" · "), "warn");
        cfg.config_problems.forEach((p) => appendLog("WARN: config — " + p));
      }
      if (cfg.version) $("app-version").textContent = cfg.version;
      // Ask GitHub once per launch when the seller left automatic checks on.
      if (cfg.update && cfg.update.check_on_start) checkUpdates(false);
      else renderUpdate();
    }
  });
  checkConnection(true);
}

if (window.pywebview && window.pywebview.api) {
  pywebviewReady();
} else {
  window.addEventListener("pywebviewready", pywebviewReady);
  // Safety net: some hosts fire the event before our listener attaches.
  setTimeout(() => { if (window.pywebview && window.pywebview.api) pywebviewReady(); }, 600);
}
