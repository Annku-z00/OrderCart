"""
whatsapp/api_client.py
======================

REST client for a **free, self-hosted** WhatsApp HTTP API.

Supported providers
-------------------
* ``waha``      — WhatsApp HTTP API  (https://github.com/devlikeapro/waha)
* ``evolution`` — Evolution API      (https://github.com/EvolutionAPI/evolution-api)

Both work the same way: you scan a QR code once (like WhatsApp Web), the
server keeps the session alive and exposes a REST API.  No Meta approval,
no business verification, **no monthly fee**.

Design goals
------------
* Connection pooling (one ``requests.Session`` reused for every call).
* Automatic retries with exponential back-off for transient failures.
* Graceful handling of ``429 Too Many Requests`` (pause, then resume).
* Lightweight chat-list caching so the batch loop can hit ~1 chat/second.

NOTE: This module never *sends* messages.  It only reads chats / messages and
downloads media.  See README.md for the legal / ethical notes.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class WhatsAppAPIError(Exception):
    """Raised for any unrecoverable error talking to the WhatsApp API."""


class ChatNotFoundError(WhatsAppAPIError):
    """Raised when a chat / message cannot be located (HTTP 404)."""


# ---------------------------------------------------------------------------
# Message helpers (tolerant to WAHA vs. Evolution shapes)
# ---------------------------------------------------------------------------
_IMAGE_MIME_HINTS = ("image/",)
_IMAGE_EXT_HINTS = ("jpeg", "jpg", "png", "webp")

#: Timeout (seconds) for a single ``/contacts/{id}`` lookup (Phase 4.5).
_CONTACT_LOOKUP_TIMEOUT = 15

#: Timeout (seconds) for the one-shot global contacts list (Phase 4.6).
_ALL_CONTACTS_TIMEOUT = 30

#: The contacts pass only *warns* past this many non-group chats; it still
#: runs, and the per-instance cache keeps any repeat lookups cheap.
_CONTACT_PASS_WARN_SIZE = 500


def message_id(msg: Dict[str, Any]) -> Optional[str]:
    """Return the message id regardless of provider shape.

    WAHA returns ``msg["id"]`` as a nested dict::

        {"fromMe": False, "remote": "66645292789868@lid",
         "id": "AC2860C51EAF654DC31D14E37816A1FE",
         "$1": "false_66645292789868@lid_AC2860C51EAF654DC31D14E37816A1FE",
         "_serialized": "false_66645292789868@lid_AC2860C51EAF654DC31D14E37816A1FE"}

    Only the raw ``id`` hash is a usable identifier (asset file names, log
    lines, state DB keys).  The ``$1`` and ``_serialized`` fields include a
    ``fromMe_<chatId>_`` prefix and must never be used to build request paths.

    Note: since WAHA's two-step media flow (``downloadMedia=true`` +
    ``media.url``) this function is *not* involved in downloading media any
    more; :meth:`WhatsAppAPIClient.download_media` takes the message dict.

    Raises:
        ValueError: if ``msg["id"]`` is a dict but has no raw ``"id"`` key.
    """
    if not isinstance(msg, dict):
        return None
    id_val = msg.get("id")
    if isinstance(id_val, dict):
        # WAHA shape — extract the raw hash, never _serialized or $1.
        raw = id_val.get("id")
        if raw:
            return str(raw)
        raise ValueError(f"Message is missing raw 'id' field: {msg!r}")
    if id_val:
        return str(id_val)
    key = msg.get("key") or {}
    if isinstance(key, dict) and key.get("id"):
        return str(key["id"])
    return None


def message_text(msg: Dict[str, Any]) -> str:
    """Return the textual body of a message ('' when it is not text)."""
    if not isinstance(msg, dict):
        return ""
    # WAHA: {"body": "..."} ; Evolution/Baileys: {"message": {"conversation": "..."}}
    for candidate in (
        msg.get("body"),
        msg.get("text"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    inner = msg.get("message")
    if isinstance(inner, dict):
        for key in ("conversation", "extendedTextMessage"):
            value = inner.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                return value["text"].strip()

    # WAHA sometimes nests the payload under "_data".
    data = msg.get("_data")
    if isinstance(data, dict) and isinstance(data.get("body"), str):
        return data["body"].strip()
    return ""


def _payload_is_image(data: Any) -> bool:
    """True when a WAHA ``_data`` payload describes an image."""
    if not isinstance(data, dict):
        return False
    # WAHA stores the engine-level type under ``_data.type`` (e.g. "image",
    # "ptt" for a voice note, "video" ...).  This is the reliable signal.
    dtype = str(data.get("type") or "").lower()
    if dtype == "image":
        return True
    if dtype:  # an explicit non-image type wins (audio / video / document ...)
        return False
    dmime = str(data.get("mimetype") or "").lower()
    return bool(dmime) and any(dmime.startswith(h) for h in _IMAGE_MIME_HINTS)


def is_image_message(msg: Dict[str, Any]) -> bool:
    """True when ``msg`` carries an image payload (WAHA or Evolution shape)."""
    if not isinstance(msg, dict):
        return False

    # --- 1. top-level explicit type / mimetype ------------------------
    mtype = str(msg.get("type") or msg.get("messageType") or "").lower()
    if mtype in ("image", "image/jpeg", "image/png", "image/webp"):
        return True

    mimetype = str(msg.get("mimetype") or "").lower()
    if mimetype and any(mimetype.startswith(h) for h in _IMAGE_MIME_HINTS):
        return True

    # --- 2. the payload WAHA actually returns lives under "_data" -----
    if _payload_is_image(msg.get("_data")):
        return True

    media = msg.get("media")

    # --- 3. Evolution / Baileys shape ---------------------------------
    inner = msg.get("message") or {}
    if isinstance(inner, dict) and "imageMessage" in inner:
        return True

    # WAHA's "media" summary (only filled once the media was downloaded).
    if isinstance(media, dict):
        media_mime = str(media.get("mimetype") or "").lower()
        if media_mime and any(media_mime.startswith(h) for h in _IMAGE_MIME_HINTS):
            return True
        if media_mime and not any(h in media_mime for h in _IMAGE_EXT_HINTS):
            return False

    # --- 4. fallback: hasMedia flag -----------------------------------
    if msg.get("hasMedia") or media:
        media_mime = ""
        if isinstance(media, dict):
            media_mime = str(media.get("mimetype") or "").lower()
        return mtype in ("", "image") or any(h in media_mime for h in _IMAGE_EXT_HINTS)
    return False


def extract_text_messages(messages: Iterable[Dict[str, Any]], limit: int = 5) -> List[str]:
    """Return the last ``limit`` *incoming* text messages (newest first)."""
    texts: List[str] = []
    for msg in messages or []:
        if msg.get("fromMe"):
            # We only trust what the *customer* sent.
            continue
        text = message_text(msg)
        if text:
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def extract_image_messages(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return every *incoming* image message (newest first, as provided).

    Outgoing (``fromMe``) messages are skipped: the seller's own automated
    "send a photo / share the order screenshot" prompts and Shorts links are
    never customer order screenshots.
    """
    return [
        m for m in (messages or [])
        if is_image_message(m) and not m.get("fromMe")
    ]


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------
class WhatsAppAPIClient:
    """Thin, resilient wrapper around the WAHA / Evolution REST endpoints."""

    # WAHA's /chats endpoint defaults to 20 records per page; the real seller
    # account has hundreds of chats, so the list is paged explicitly.
    _WAHA_CHATS_PAGE = 500
    _WAHA_CHATS_MAX = 20_000

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        provider: str = "waha",
        session_name: str = "default",
        api_timeout: int = 30,
        media_timeout: int = 60,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        verify_ssl: bool = True,
        chat_cache_ttl: float = 120.0,
        chat_name_overrides: Optional[Dict[str, str]] = None,
    ) -> None:
        if not base_url:
            raise WhatsAppAPIError("base_url is required (set whatsapp_api.base_url in config.yaml)")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.provider = (provider or "waha").lower()
        self.session_name = session_name or "default"
        self.api_timeout = int(api_timeout)
        self.media_timeout = int(media_timeout)
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = float(retry_backoff)
        self.verify_ssl = verify_ssl

        # --- pooled HTTP session (reused for every request) ---
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.verify = verify_ssl

        # --- chat list cache ---
        self._chats_cache: Optional[List[Dict[str, Any]]] = None
        self._chats_cache_time: float = 0.0
        self.chat_cache_ttl = float(chat_cache_ttl)

        # --- per-instance contact-record cache (Phase 4.5) ---------------
        #      serialized chat id -> /contacts/{id} record; None marks an
        #    authoritative miss (HTTP 404). Transient failures are NOT cached
        #    so they can succeed on a later attempt.
        self._contact_cache: Dict[str, Optional[Dict[str, Any]]] = {}

        # --- global contacts list cache (Phase 4.6) -----------------------
        #    One ``/api/contacts/all`` response per client instance.  The
        #    chat list can be partial (WAHA hides chats without a
        #    conversation timestamp), so this is the authoritative fallback
        #    for a saved contact whose chat is missing from ``/chats``.
        self._all_contacts_cache: Optional[List[Dict[str, Any]]] = None

        # --- optional id -> chat-name overrides (e.g. trial on a named contact) ---
        self.chat_name_overrides: Dict[str, str] = {
            str(k).strip(): str(v).strip()
            for k, v in (chat_name_overrides or {}).items()
            if str(k).strip() and str(v).strip()
        }

        logger.debug("WhatsAppAPIClient ready: provider=%s base_url=%s", self.provider, self.base_url)

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------
    @property
    def headers(self) -> Dict[str, str]:
        """Auth headers differ slightly per provider."""
        if self.provider == "evolution":
            return {"apikey": self.api_key, "Content-Type": "application/json"}
        # WAHA default
        return {"X-Api-Key": self.api_key, "Accept": "application/json"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> requests.Response:
        """Perform a request with retries + exponential back-off.

        * 429  -> sleep and retry (rate limit)
        * 404  -> raise :class:`ChatNotFoundError` immediately
        * 5xx / network errors -> retry up to ``max_retries`` times

        ``retries`` overrides the client's retry count — pass ``0`` for a
        request whose answer will not change by asking again (a 4xx).  It
        is the number of *retries*, so there is always at least one
        attempt.
        """
        url = f"{self.base_url}{path}"
        timeout = timeout or self.api_timeout
        last_exc: Optional[Exception] = None
        attempts = self.max_retries if retries is None else max(1, int(retries))

        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=self.headers,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                )

                if resp.status_code == 429:
                    wait = self.retry_backoff * attempt
                    logger.warning("Rate limited (429) on %s — sleeping %.1fs", url, wait)
                    time.sleep(wait)
                    continue

                if resp.status_code == 404:
                    raise ChatNotFoundError(f"404 Not Found: {url}")

                resp.raise_for_status()
                return resp

            except ChatNotFoundError:
                raise
            except requests.RequestException as exc:  # network / HTTP error
                last_exc = exc
                if attempt < attempts:
                    wait = self.retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "API call failed (attempt %d/%d) %s — retrying in %.1fs (%s)",
                        attempt, attempts, url, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    # Last attempt: say so, instead of promising a retry that
                    # will not happen (retries=0 made every log misleading).
                    logger.warning(
                        "API call failed (attempt %d/%d) %s — giving up (%s)",
                        attempt, attempts, url, exc,
                    )

        raise WhatsAppAPIError(
            f"Request to {url} failed after {attempts} attempts: {last_exc}"
        )

    def _get_with_retries(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        """GET ``url`` retrying transient failures (429 / 5xx / network).

        Mirrors :meth:`_request`'s retry policy but goes through
        ``requests.Session.get`` so query-string callers (``get_messages``,
        ``download_media``) keep working unchanged.  The final response is
        returned for the caller to interpret (404 detection stays with the
        caller); :class:`WhatsAppAPIError` is only raised when the network
        itself keeps failing across every attempt.
        """
        timeout = timeout or self.api_timeout
        kwargs: Dict[str, Any] = {"headers": self.headers, "timeout": timeout}
        if params is not None:
            kwargs["params"] = params

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = self.retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "GET %s failed (attempt %d/%d) — retrying in %.1fs (%s)",
                        url, attempt, self.max_retries, wait, exc,
                    )
                    time.sleep(wait)
                    continue
                raise WhatsAppAPIError(
                    f"Request to {url} failed after {self.max_retries} attempts: {exc}"
                ) from exc

            status = int(getattr(resp, "status_code", 0) or 0)
            transient = status == 429 or 500 <= status < 600
            if transient and attempt < self.max_retries:
                wait = (
                    self.retry_backoff * attempt
                    if status == 429
                    else self.retry_backoff * (2 ** (attempt - 1))
                )
                logger.warning(
                    "GET %s returned HTTP %s (attempt %d/%d) — retrying in %.1fs",
                    url, status, attempt, self.max_retries, wait,
                )
                time.sleep(wait)
                continue
            return resp

        # Unreachable in practice: the loop either returns or raises.
        raise WhatsAppAPIError(f"Request to {url} failed: {last_exc}")

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------
    def get_all_chats(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Return the full chat list (cached for ``chat_cache_ttl`` seconds).

        WAHA pages ``/api/{session}/chats`` with a **default limit of 20**.
        Asking without a limit therefore returns only the 20 most recent
        conversations — on a real seller account the order chats live far
        beyond that window and every lookup would report MISSING.  The list
        is fetched page by page until WAHA returns a short page.
        """
        now = time.time()
        if (
            not force_refresh
            and self._chats_cache is not None
            and (now - self._chats_cache_time) < self.chat_cache_ttl
        ):
            return self._chats_cache

        if self.provider == "evolution":
            path = f"/chat/findChats/{quote(self.session_name, safe='')}"
            resp = self._request("GET", path)
            data = resp.json()
            chats = data if isinstance(data, list) else data.get("data", [])
        else:  # waha
            path = f"/api/{quote(self.session_name, safe='')}/chats"
            chats: List[Dict[str, Any]] = []
            offset = 0
            while True:
                resp = self._request(
                    "GET",
                    path,
                    params={"limit": self._WAHA_CHATS_PAGE, "offset": offset},
                )
                data = resp.json()
                batch = data if isinstance(data, list) else data.get("chats", [])
                batch = [c for c in (batch or []) if isinstance(c, dict)]
                chats.extend(batch)
                if len(batch) < self._WAHA_CHATS_PAGE:
                    break
                offset += self._WAHA_CHATS_PAGE
                if offset >= self._WAHA_CHATS_MAX:
                    logger.warning(
                        "[get_all_chats] stopped at %d chats (safety cap)",
                        self._WAHA_CHATS_MAX,
                    )
                    break

        self._chats_cache = chats or []
        self._chats_cache_time = now
        logger.info("Fetched %d chats from %s", len(self._chats_cache), self.provider)
        return self._chats_cache

    @staticmethod
    def _chat_names(chat: Dict[str, Any]) -> List[str]:
        """All plausible names for a chat, lower-cased for comparison."""
        names: List[str] = []
        for key in ("name", "pushName", "subject", "verifiedName"):
            value = chat.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
        # The chat id (e.g. "919999999999@c.us") also carries a usable handle.
        cid = chat.get("id") or chat.get("remoteJid") or ""
        if isinstance(cid, str):
            names.append(cid)
            names.append(cid.split("@")[0])
        return names

    @staticmethod
    def _chat_serial_id(chat: Dict[str, Any]) -> str:
        """Serialized chat id (``66645292789868@lid``) usable in URLs, or ''."""
        if not isinstance(chat, dict):
            return ""
        cid = chat.get("id")
        if isinstance(cid, dict):
            return str(cid.get("_serialized") or "")
        return str(cid or "")

    def _first_chat_match(
        self,
        chats: Iterable[Dict[str, Any]],
        target: str,
        relaxed: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """First non-group chat in ``chats`` whose name matches ``target``.

        ``target`` is stripped and lower-cased.  Exact phase matches any
        known name form (name / pushName / subject / serialized id) or the
        chat's ``id.user`` number.  The relaxed phase only runs after the
        exact pass failed and keeps the Phase 4.3 behaviour of matching
        ``"12988"`` inside ``"12988 (Meesho)"``; it is restricted to
        whole-word matches so a numeric prefix or ``id.user`` substring can
        never resolve the wrong chat.
        """
        if not target:
            return None
        relaxed_re = (
            re.compile(rf"(?<!\w){re.escape(target)}(?!\w)", re.IGNORECASE)
            if relaxed else None
        )
        for chat in chats or []:
            if not isinstance(chat, dict) or chat.get("isGroup"):
                continue
            for cand in self._chat_names(chat):
                if relaxed:
                    # Whole-word match only (e.g. "12988" in "12988 (Meesho)").
                    if relaxed_re.search(cand):
                        return chat
                elif cand.strip().lower() == target:
                    return chat
            if not relaxed:
                cid = chat.get("id")
                if isinstance(cid, dict):
                    user = str(cid.get("user") or "").strip().lower()
                    if user and user == target:
                        return chat
        return None

    def _search_chats(self, name: str) -> List[Dict[str, Any]]:
        """WAHA server-side chat search — Pass 1 of :meth:`get_chat_by_name`.

        GET ``/api/{session}/chats?search=<name>``.  The response may be a
        bare list, ``{"chats": [...]}`` or ``{"messages": [...]}``; all are
        accepted defensively.  Any failure (unsupported endpoint, network
        error, bad payload) yields ``[]`` so the lookup continues with the
        chat-list pass.  Never raises.
        """
        url = f"{self.base_url}/api/{quote(self.session_name, safe='')}/chats"
        try:
            resp = self.session.get(
                url,
                headers=self.headers,
                params={"search": name},
                timeout=self.api_timeout,
            )
            if resp.status_code != 200:
                logger.debug(
                    "[get_chat_by_name] search endpoint unavailable (HTTP %s)",
                    resp.status_code,
                )
                return []
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug("[get_chat_by_name] search failed (%s)", exc.__class__.__name__)
            return []
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
        if isinstance(data, dict):
            for key in ("chats", "messages"):
                if isinstance(data.get(key), list):
                    return [c for c in data[key] if isinstance(c, dict)]
        return []

    # ------------------------------------------------------------------
    # Contacts — Phase 4.5 (saved-name resolution for @lid chats)
    # ------------------------------------------------------------------
    def get_contact_by_id(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the resolved contact record for a chat id.

        WAHA's ``/chats`` endpoint reports the WhatsApp display name for
        ``@lid`` chats (a formatted phone number), while ``/contacts/{id}``
        returns the address-book record with the **saved contact name** —
        the name FrameFlow's internal ids are matched against.

        Accepts lid form (``66645292789868@lid``), c.us form
        (``918740023243@c.us``) or their serialized string.

        Returns ``None`` when there is no contact record, and *never*
        raises: any failure (HTTP 404, network error, malformed payload)
        becomes a ``None`` result so the chat lookup can fall back to
        MISSING instead of crashing the batch.

        Results are cached per instance — including authoritative misses —
        so the orchestrator pays at most one HTTP call per chat per run.
        Transient failures (unreachable server, non-404 HTTP status,
        malformed body) are deliberately *not* cached: a later call can
        succeed once the API recovers.
        """
        if not isinstance(chat_id, str):
            return None
        chat_id = chat_id.strip()
        if not chat_id:
            return None

        if chat_id in self._contact_cache:
            return self._contact_cache[chat_id]

        url = (
            f"{self.base_url}/api/{quote(self.session_name, safe='')}"
            f"/contacts/{quote(chat_id, safe='')}"
        )
        try:
            resp = self.session.get(
                url, headers=self.headers, timeout=_CONTACT_LOOKUP_TIMEOUT
            )
        except requests.RequestException as exc:
            logger.debug(
                "[get_contact_by_id] lookup failed (%s) — result not cached",
                exc.__class__.__name__,
            )
            return None
        try:
            if resp.status_code == 404:
                # The server confirms no contact exists for this id — an
                # authoritative miss, safe (and cheap) to cache.
                self._contact_cache[chat_id] = None
                return None
            if resp.status_code != 200:
                logger.debug(
                    "[get_contact_by_id] unexpected HTTP status %s — result not cached",
                    resp.status_code,
                )
                return None
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug(
                "[get_contact_by_id] invalid response (%s)", exc.__class__.__name__
            )
            return None
        if not isinstance(data, dict) or not data:
            # Empty or malformed payload — treat as an authoritative miss.
            self._contact_cache[chat_id] = None
            return None
        self._contact_cache[chat_id] = data
        return data

    def get_all_contacts(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Return every saved contact from WAHA's global contacts endpoint.

        ``GET /api/contacts/all?session=<session>`` returns the address book
        in one response.  This is the fallback for the case where WAHA's
        ``/chats`` list is partial (the server omits chats whose
        ``conversationTimestamp`` is null), which would otherwise hide a
        saved contact from the per-chat ``/contacts/{id}`` pass.

        The result is cached per client instance.  This method *never*
        raises: any failure (unsupported endpoint, network error, malformed
        payload) yields ``[]`` so the lookup can fall back to the per-chat
        contacts pass.
        """
        if self.provider != "waha":
            return []
        if not force_refresh and self._all_contacts_cache is not None:
            return self._all_contacts_cache

        url = f"{self.base_url}/api/contacts/all"
        try:
            resp = self.session.get(
                url,
                headers=self.headers,
                params={"session": self.session_name},
                timeout=_ALL_CONTACTS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.debug(
                "[get_all_contacts] lookup failed (%s) — fallback not cached",
                exc.__class__.__name__,
            )
            return []
        if resp.status_code != 200:
            logger.debug(
                "[get_all_contacts] unexpected HTTP status %s — fallback unavailable",
                resp.status_code,
            )
            return []
        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug(
                "[get_all_contacts] invalid response (%s)", exc.__class__.__name__
            )
            return []

        contacts = data if isinstance(data, list) else data.get("contacts", [])
        contacts = [c for c in (contacts or []) if isinstance(c, dict)]
        self._all_contacts_cache = contacts
        logger.debug("[get_all_contacts] fetched %d contacts", len(contacts))
        return contacts

    @staticmethod
    def _contact_chat_id(contact: Dict[str, Any]) -> str:
        """Best chat id for a global contact record.

        The phone JID (``...@s.whatsapp.net``) is preferred over the
        ``@c.us`` form because that is what the messages endpoint accepts on
        WAHA CORE 2026.9+; both are accepted in practice, but the phone JID
        is the one FrameFlow already stores for resolved chats.
        """
        phone = str(contact.get("phoneNumber") or "").strip()
        if phone:
            return phone
        cid = contact.get("id")
        if isinstance(cid, dict):
            return str(cid.get("_serialized") or "")
        return str(cid or "")

    @staticmethod
    def _contact_matches(contact: Any, target: str) -> bool:
        """Saved-contact record matches ``target`` (stripped, lower-cased)?

        Matches the saved contact ``name``, the canonical ``number`` or the
        contact's WhatsApp ``pushname`` — all case-insensitive.
        """
        if not isinstance(contact, dict):
            return False
        if str(contact.get("name") or "").strip().lower() == target:
            return True
        if str(contact.get("number") or "").strip() == target:
            return True
        if str(contact.get("phoneNumber") or "").strip() == target:
            return True
        if str(contact.get("pushname") or "").strip().lower() == target:
            return True
        return False

    def get_chat_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find the chat whose *saved contact name* equals the internal id.

        Resolution ladder (fastest first; groups are never returned):

        0.  Explicit ``chat_name_overrides`` entry (kept from Phase 4.3).
        1.  Server-side search ``GET /api/{session}/chats?search=<name>``.
        2.  Full chat list (cached) — WhatsApp display names, saved contact
            names known to the device, and ``id.user`` numbers.
        3.  Contacts resolution — every non-group chat id is resolved through
            ``/contacts/{id}`` (cached per instance, misses included) and
            matched against the saved contact ``name`` / ``number`` /
            ``pushname``.

        The final pass is what makes ``"12988"`` resolve on WAHA CORE
        2026.9+: for ``@lid`` chats the saved name only lives on the
        contacts endpoint, the chat list shows a formatted phone number.

        Returns ``None`` when nothing matches (caller logs MISSING and moves
        on).  INFO is logged only once, on a successful contact resolution;
        everything else is DEBUG.  The contact JSON payload, phone numbers
        and the API key are never logged at INFO+ (debug output carries only
        the ``@lid`` chat id and matched name).
        """
        name = str(name).strip()
        target = name.lower()
        if not target:
            return None
        logger.debug("[get_chat_by_name] target=%r", name)

        # --- Pass 0 — explicit id -> chat-name override (Phase 4.3) -------
        override = self.chat_name_overrides.get(name)
        if override:
            chats = self.get_all_chats()
            logger.debug("[get_chat_by_name] pass 0 (override): %r -> %r", name, override)
            ov = override.strip().lower()
            for chat in chats:
                if chat.get("isGroup"):
                    continue
                for cand in self._chat_names(chat):
                    if cand.strip().lower() == ov:
                        return chat
            for chat in chats:
                if chat.get("isGroup"):
                    continue
                for cand in self._chat_names(chat):
                    if ov in cand.lower():
                        return chat

        # --- Pass 1 — server-side search (fastest) ------------------------
        search_hits = self._search_chats(name)
        logger.debug("[get_chat_by_name] pass 1 (search): hits=%d", len(search_hits))
        hit = self._first_chat_match(search_hits, target)
        if hit is not None:
            logger.debug(
                "[get_chat_by_name] RESOLVED via search (pass 1): name=%r chat_id=%r",
                name, self._chat_serial_id(hit),
            )
            return hit

        # --- Pass 2 — full chat list (display names / id.user) ------------
        chats = self.get_all_chats()
        non_group = [c for c in chats if not c.get("isGroup")]
        logger.debug(
            "[get_chat_by_name] pass 2 (chat list): non_group_count=%d", len(non_group)
        )
        hit = self._first_chat_match(non_group, target)
        if hit is not None:
            logger.debug(
                "[get_chat_by_name] RESOLVED via chat list (pass 2): name=%r chat_id=%r",
                name, self._chat_serial_id(hit),
            )
            return hit
        # Phase 4.3 relaxed fallback kept: "12988" matches "12988 (Meesho)".
        hit = self._first_chat_match(non_group, target, relaxed=True)
        if hit is not None:
            logger.debug(
                "[get_chat_by_name] RESOLVED via chat list (pass 2, relaxed): "
                "name=%r chat_id=%r",
                name, self._chat_serial_id(hit),
            )
            return hit

        # --- Pass 3a — global contacts list (WAHA) ------------------------
        # WAHA's ``/chats`` list can be partial: the server omits chats
        # whose ``conversationTimestamp`` is null, which is common for old
        # ``@lid`` chats after a re-sync.  The per-chat contacts pass below
        # only walks chats that made it into that list, so a saved contact
        # whose chat is missing would be unreachable.  One cached call to
        # the global contacts endpoint closes that gap.
        if self.provider == "waha":
            all_contacts = self.get_all_contacts()
            for contact in all_contacts:
                if not self._contact_matches(contact, target):
                    continue
                chat_id = self._contact_chat_id(contact)
                if not chat_id:
                    continue
                logger.info(
                    "[get_chat_by_name] RESOLVED via all-contacts: "
                    "name=%r chat_id=%r",
                    name, chat_id,
                )
                return {"id": chat_id, "name": name}
            logger.debug(
                "[get_chat_by_name] pass 3a (all contacts): checked=%d, matched=0",
                len(all_contacts),
            )

        # --- Pass 3 — contacts (address book) resolution ------------------
        if self.provider != "waha":
            # Evolution exposes contacts through a different endpoint
            # entirely; its path stays untouched (Phase 4.4 contract).
            logger.debug(
                "[get_chat_by_name] pass 3 skipped: contacts endpoint is "
                "WAHA-only (provider=%s)",
                self.provider,
            )
            logger.debug("[get_chat_by_name] target=%r NOT FOUND after all 3 passes", name)
            return None

        if len(non_group) > _CONTACT_PASS_WARN_SIZE and not getattr(
            self, "_contact_pass_warned", False
        ):
            # One-time notice: the scan costs a few seconds, then every
            # contact (misses included) is cached and later lookups are
            # in-memory.  Logging it on every id would drown the run log.
            self._contact_pass_warned = True
            logger.warning(
                "[get_chat_by_name] large chat list (%d non-group chats) — "
                "contacts pass may be slow on the first call (cached afterwards)",
                len(non_group),
            )

        started = time.perf_counter()
        checked = 0
        try:
            for chat in non_group:
                chat_id = self._chat_serial_id(chat)
                if not chat_id:
                    continue
                checked += 1
                contact = self.get_contact_by_id(chat_id)
                if self._contact_matches(contact, target):
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    logger.debug(
                        "[get_chat_by_name] pass 3 (contacts): checked=%d, "
                        "matched=1, elapsed_ms=%d",
                        checked, elapsed_ms,
                    )
                    logger.info(
                        "[get_chat_by_name] RESOLVED via contacts: name=%r chat_id=%r",
                        name, chat_id,
                    )
                    return chat
        except Exception as exc:  # pragma: no cover - contacts pass must never crash
            logger.warning(
                "[get_chat_by_name] contacts pass aborted (%s)", exc.__class__.__name__
            )
            logger.debug("[get_chat_by_name] target=%r NOT FOUND after all 3 passes", name)
            return None
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "[get_chat_by_name] pass 3 (contacts): checked=%d, matched=0, elapsed_ms=%d",
            checked, elapsed_ms,
        )
        logger.debug("[get_chat_by_name] target=%r NOT FOUND after all 3 passes", name)
        return None

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def get_messages(
        self,
        chat_id: str,
        limit: int = 10,
        download_media: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent messages for ``chat_id`` (newest first when available).

        WAHA CORE 2026.9+ has **no** per-message media download endpoint any
        more.  The supported flow is two-step:

        1. Fetch messages with ``downloadMedia=true`` — WAHA downloads the
           media server-side (from WhatsApp's CDN) and adds a
           ``media.url`` pointing at ``/api/files/<name>`` on each message.
        2. Download those bytes by GET-ing ``media.url`` (see
           :meth:`download_media`).

        The ``download_media`` argument therefore defaults to *on*: only an
        explicit ``False`` turns the flag off (which leaves image messages
        with ``hasMedia: true`` but no URL).  The longer timeout matters
        because the server-side media fetch can take a while the first time.
        """
        if self.provider == "evolution":
            path = f"/chat/findMessages/{quote(self.session_name, safe='')}"
            body = {
                "where": {"key": {"remoteJid": chat_id}},
                "limit": int(limit),
            }
            resp = self._request("POST", path, json_body=body)
            data = resp.json()
            records = data
            if isinstance(data, dict):
                records = (
                    data.get("messages", {}).get("records")
                    if isinstance(data.get("messages"), dict)
                    else data.get("messages", data.get("data", []))
                )
            return records or []

        # WAHA
        path = (
            f"/api/{quote(self.session_name, safe='')}"
            f"/chats/{quote(chat_id, safe='')}/messages"
        )
        params: Dict[str, Any] = {
            "limit": int(limit),
            "downloadMedia": "true" if download_media is not False else "false",
        }
        # Media is fetched server-side with this call, so give it the media
        # timeout (not the fast API timeout).  Transient 429/5xx/network
        # failures are retried so a single blip cannot fail the whole id.
        url = f"{self.base_url}{path}"
        resp = self._get_with_retries(url, params=params, timeout=self.media_timeout)
        if resp.status_code == 404:
            raise ChatNotFoundError(f"404 Not Found: {url}")
        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise WhatsAppAPIError(f"Request to {url} failed: {exc}")
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("messages", data.get("data", []))
        return []

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------
    def download_media(self, msg: Dict[str, Any]) -> bytes:
        """Download the **original** media bytes for a message.

        Requires the message to have been fetched with ``downloadMedia=true``
        so that ``msg["media"]["url"]`` is populated — WAHA serves media as a
        static file under ``/api/files/<filename>``.  We do not build any URL
        from message ids here; :func:`message_id` remains available for
        logging, state keys and asset file names only.

        Progress note: :meth:`_rebase_url` is applied so WAHA urls that were
        built from the container's *internal* base (wrong host port) still
        resolve on the base url we actually talk to.
        """
        if not isinstance(msg, dict):
            raise WhatsAppAPIError(
                "download_media expects the full message dict "
                "(fetched with downloadMedia=true)"
            )

        try:
            msg_ref = message_id(msg) or "?"
        except ValueError:
            msg_ref = "?"

        if self.provider == "evolution":
            path = f"/chat/getBase64FromMediaMessage/{quote(self.session_name, safe='')}"
            body = {
                "message": {
                    "key": {
                        "id": msg_ref if msg_ref != "?" else "",
                        "remoteJid": (msg.get("key") or {}).get("remoteJid")
                        or msg.get("chatId")
                        or "",
                    }
                }
            }
            resp = self._request("POST", path, json_body=body, timeout=self.media_timeout)
            data = resp.json()
            b64 = (
                data.get("base64")
                or data.get("media")
                or (data.get("data") or {}).get("base64")
            )
            if not b64:
                raise WhatsAppAPIError(f"Evolution API returned no media for message {msg_ref}")
            # Evolution usually returns a full data URI; strip the prefix.
            if isinstance(b64, str) and "," in b64 and b64.strip().startswith("data:"):
                b64 = b64.split(",", 1)[1]
            return base64.b64decode(b64)

        # WAHA — media was materialised during get_messages(downloadMedia=…);
        # the only thing left to do is GET the ``media.url`` it gave us.
        media = msg.get("media") or {}
        url = self._rebase_url(media.get("url"))

        if not url:
            raise WhatsAppAPIError(
                f"Message has no media.url. Did you fetch with downloadMedia=true? "
                f"msg_id={msg_ref}"
            )

        resp = self._get_with_retries(url, timeout=self.media_timeout)
        if resp.status_code != 200:
            raise WhatsAppAPIError(
                f"Media download failed ({resp.status_code}) for {url}: {resp.text[:200]}"
            )
        if not resp.content:
            raise WhatsAppAPIError(f"Media download returned empty body for {url}")

        logger.debug(
            "Downloaded media for message %s (%d bytes) from %s",
            msg_ref, len(resp.content), url,
        )
        return resp.content

    def _rebase_url(self, url: str) -> str:
        """Rewrite a WAHA-issued absolute url onto the base we actually use.

        WAHA builds ``media.url`` from its *internal* base url (usually
        ``http://localhost:3000``), which is wrong as soon as the container is
        published on a different host port (e.g. ``3001``).  We keep only the
        ``/api/files/...`` suffix and re-attach our own ``base_url``.  When the
        url already points at our own base this is a no-op.
        """
        if not url or not isinstance(url, str):
            return url or ""
        if url.startswith("/"):
            return f"{self.base_url}{url}"
        marker = "/api/files/"
        idx = url.find(marker)
        if idx != -1:
            rebased = f"{self.base_url}{url[idx:]}"
            if rebased != url:
                logger.debug("Rebased WAHA media url %s -> %s", url, rebased)
            return rebased
        return url

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """Best-effort connectivity check. Returns True when the API answers."""
        try:
            self.get_all_chats(force_refresh=True)
            return True
        except WhatsAppAPIError as exc:
            logger.error("WhatsApp API unreachable: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Unexpected error during ping: %s", exc)
            return False

    #: The session config the browser-free ``NOWEB`` engine needs: without its
    #: store the session cannot list chats at all (WAHA answers
    #: ``400 Enable NOWEB store ...``).
    NOWEB_SESSION_CONFIG = {"noweb": {"store": {"enabled": True, "fullSync": True}}}

    def create_session(self, name: str) -> bool:
        """Create a WhatsApp account (a WAHA session) on the server.

        The session is created with the configuration the browser-free
        ``NOWEB`` engine needs, and falls back to a plain session when the
        server does not accept it.  Returns ``True`` when the session exists
        afterwards.
        """
        session = str(name or "").strip()
        if not session:
            return False
        known = {str(s.get("name") or "") for s in self.list_sessions()}
        if session in known:
            return True
        for config in (self.NOWEB_SESSION_CONFIG, None):
            body: Dict[str, Any] = {"name": session}
            if config:
                body["config"] = config
            try:
                self._request("POST", "/api/sessions", json_body=body, retries=0)
                return True
            except Exception as exc:
                logger.warning("Could not create the session %s: %s", session, exc)
        return False

    def start_session(self, name: str) -> bool:
        """Start a WAHA session (it then shows its QR code)."""
        session = str(name or "").strip()
        if not session:
            return False
        try:
            self._request(
                "POST", f"/api/sessions/{quote(session, safe='')}/start", retries=0
            )
            return True
        except Exception as exc:
            logger.warning("Could not start the session %s: %s", session, exc)
            return False

    def restart_session(self, name: str) -> bool:
        """Restart a WAHA session.

        A session whose pairing code was never scanned stops itself
        ("QR refresh attempts ended") and goes to ``FAILED``.  Only a
        *restart* brings it back with a fresh code — a plain *start* does
        not.  The session's configuration is kept.
        """
        session = str(name or "").strip()
        if not session:
            return False
        try:
            self._request(
                "POST", f"/api/sessions/{quote(session, safe='')}/restart", retries=0
            )
            return True
        except Exception as exc:
            logger.warning("Could not restart the session %s: %s", session, exc)
            return False

    def check_connection(self) -> Optional[str]:
        """Ping the API without the retry policy.

        Returns ``None`` when the server answers, or the error text when it
        does not.  Used by the GUI to show an "API offline" banner - the
        difference from :meth:`list_sessions` is that a dead server is
        reported instead of silently becoming an empty list.
        """
        try:
            self._request("GET", "/api/sessions", retries=0)
        except Exception as exc:
            return str(exc)
        return None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """The WhatsApp accounts (WAHA sessions) the server knows about.

        Every session is one linked WhatsApp account.  The list is what the
        Settings view shows: the seller ticks the accounts they want to
        collect orders from.

        Returns an empty list when the server does not answer — the caller
        then falls back to the configured accounts.
        """
        path = "/api/sessions"
        try:
            resp = self._request("GET", path)
            data = resp.json()
        except Exception as exc:
            logger.warning("Could not list the WAHA sessions: %s", exc)
            return []
        if isinstance(data, dict):
            data = data.get("sessions", data.get("data", []))
        if not isinstance(data, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            me = item.get("me") or {}
            out.append({
                "name": str(item.get("name") or ""),
                "status": str(item.get("status") or ""),
                "me": str(me.get("pushName") or me.get("id") or ""),
            })
        return out

    def close(self) -> None:
        """Close the underlying HTTP session (free the connection pool)."""
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # Context-manager sugar -------------------------------------------------
    def __enter__(self) -> "WhatsAppAPIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(
        cls, config: Dict[str, Any], session_name: Optional[str] = None
    ) -> "WhatsAppAPIClient":
        """Build a client from a parsed ``config.yaml`` dictionary.

        ``session_name`` overrides the configured session — that is how the
        app talks to more than one WhatsApp account: every account is a
        WAHA session, so one client per session.
        """
        api = (config or {}).get("whatsapp_api", {})
        timeouts = (config or {}).get("timeouts", {})
        processing = (config or {}).get("processing", {})
        return cls(
            base_url=api.get("base_url", "http://localhost:3000"),
            api_key=api.get("api_key", ""),
            provider=api.get("provider", "waha"),
            session_name=(session_name or api.get("session_name", "default")),
            chat_name_overrides=api.get("chat_name_overrides") or {},
            api_timeout=timeouts.get("api_call", 30),
            media_timeout=timeouts.get("media_download", 60),
            max_retries=processing.get("max_retries", 3),
            retry_backoff=processing.get("retry_backoff", 2.0),
        )
