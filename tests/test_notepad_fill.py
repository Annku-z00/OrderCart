"""Tests for the Notepad++ master-list ("fill") writer mode.

A seller keeps a plain list of internal chat ids in Notepad++, one id per line,
and wants the extracted order number pasted *next to* that id — not appended as
a new ``id,value`` line at the bottom.  These tests pin that layout.
"""

from __future__ import annotations

import logging

from output.writer import NotepadWriter


def _read(path) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return [line.rstrip("\n") for line in fh]


def test_fill_places_the_value_next_to_the_matching_id(tmp_path):
    path = tmp_path / "15-10-2026.txt"
    path.write_text("12328\n12329\n12330\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12329, {"type": "OK", "order_id": "341999308084544"})

    assert _read(path) == [
        "12328",
        "12329\t341999308084544",
        "12330",
    ]


def test_fill_keeps_a_bare_id_blank_when_there_is_no_order(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n12329\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12329, {"type": "FAILED"})

    assert _read(path) == ["12328", "12329"]


def test_fill_does_not_duplicate_the_same_value(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\t3420026566347011\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_never_writes_an_order_id_for_a_multi_order_chat(tmp_path):
    """Two nameless orders: no id, nothing else -> blank + a typed warning."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328, {"type": "OK", "order_ids": ["1111222233334444", "5555666677778888"]}
    )

    assert _read(path) == ["12328"]
    assert "2 orders" in writer.last_notice
    assert "no customer name" in writer.last_notice
    assert "1111222233334444" not in writer.last_notice
    assert writer.last_notice_kind == "MULTI: no name"


def test_fill_skips_an_id_that_has_no_line_and_logs_it(tmp_path, caplog):
    """The master list is authoritative: no stray lines are added to it."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    with caplog.at_level(logging.WARNING, logger="output.writer"):
        writer.write_result(12329, {"type": "OK", "order_id": "341999308084544"})

    assert _read(path) == ["12328"]
    assert "event=notepad_line_missing" in caplog.text
    assert "internal_id=12329" in caplog.text


def test_fill_creates_rows_in_a_brand_new_list(tmp_path):
    """First run of a fresh day/week list: the file does not exist yet."""
    path = tmp_path / "new-day.txt"

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12988, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12988\t3420026566347011"]


def test_fill_adds_rows_to_an_empty_list(tmp_path):
    """An existing but still-empty list is also a new list."""
    path = tmp_path / "list.txt"
    path.write_text("", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12988, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12988\t3420026566347011"]


def test_fill_replaces_a_previous_placeholder(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\tTRUNC:342002\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_is_idempotent_and_preserves_blank_lines(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n\n12330\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})
    first = _read(path)
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == first
    assert first[1] == ""


def test_fill_keeps_rows_aligned_when_a_chat_is_missing(tmp_path):
    """A missing chat leaves its own line blank - later rows never shift up."""
    path = tmp_path / "list.txt"
    path.write_text("12988\n12989\n12990\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12988, {"type": "OK", "order_id": "3420026566347011"})
    # 12989 has no chat, so the orchestrator never calls the writer for it.
    writer.write_result(12990, {"type": "OK", "order_id": "341999308084544"})

    assert _read(path) == [
        "12988\t3420026566347011",
        "12989",
        "12990\t341999308084544",
    ]


def test_fill_keeps_rows_aligned_when_a_chat_fails(tmp_path):
    """A failed chat stays blank without disturbing the next row."""
    path = tmp_path / "list.txt"
    path.write_text("12988\n12989\n12990\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12988, {"type": "OK", "order_id": "3420026566347011"})
    writer.write_result(12989, {"type": "FAILED", "order_rows": []})
    writer.write_result(12990, {"type": "OK", "order_id": "341999308084544"})

    assert _read(path) == [
        "12988\t3420026566347011",
        "12989",
        "12990\t341999308084544",
    ]


def test_fill_correction_updates_the_existing_line(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\t3420026566347011\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    changed = writer.apply_corrections(
        {12328: "3420026566347022"},
        matches={12328: "3420026566347011"},
    )

    assert changed == 1
    assert _read(path) == ["12328\t3420026566347022"]


def test_append_mode_is_unchanged(tmp_path):
    path = tmp_path / "orders.txt"
    writer = NotepadWriter(str(path))  # default append mode

    writer.write_result(12988, {"type": "OK", "order_id": "342512705"})

    assert _read(path) == ["12988,342512705"]


# ----------------------------------------------------------------------
# Fallback chain: order id -> name -> address, written plain like an id
# ----------------------------------------------------------------------


def test_fill_writes_the_name_when_there_is_no_order_id(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n12329\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12329,
        {"type": "FALLBACK_NAME", "name": "Priya Sharma", "order_rows": []},
    )

    assert _read(path) == ["12328", "12329\tPriya Sharma"]


def test_fill_writes_the_address_when_there_is_no_id_and_no_name(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {"type": "FALLBACK_ADDRESS", "address": "12 MG Road, Pune", "order_rows": []},
    )

    assert _read(path) == ["12328\t12 MG Road, Pune"]


def test_fill_prefers_the_name_over_the_address(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "FALLBACK_NAME",
            "name": "Priya Sharma",
            "address": "12 MG Road, Pune",
            "order_rows": [],
        },
    )

    assert _read(path) == ["12328\tPriya Sharma"]


def test_fill_prefers_a_real_id_over_a_name(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "OK",
            "order_id": "3420026566347011",
            "name": "Priya Sharma",
            "order_rows": [],
        },
    )

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_keeps_a_failed_chat_blank(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "FAILED", "order_rows": []})

    assert _read(path) == ["12328"]


def test_fill_does_not_displace_a_truncated_id_with_a_name(tmp_path):
    """A cut-off id is still an order number - a name must not replace it."""
    path = tmp_path / "list.txt"
    path.write_text("12328\tTRUNC:3420026566\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {"type": "FALLBACK_NAME", "name": "Priya Sharma", "order_rows": []},
    )

    assert _read(path) == ["12328\tTRUNC:3420026566"]


def test_fill_does_not_overwrite_an_existing_id_with_a_name(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\t3420026566347011\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {"type": "FALLBACK_NAME", "name": "Priya Sharma", "order_rows": []},
    )

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_replaces_a_previous_name_with_a_real_id(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\tPriya Sharma\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_name_is_idempotent(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    result = {"type": "FALLBACK_NAME", "name": "Priya Sharma", "order_rows": []}
    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, result)
    writer.write_result(12328, result)

    assert _read(path) == ["12328\tPriya Sharma"]


def test_fill_address_with_a_comma_is_not_split_on_a_rerun(tmp_path):
    """A tab-separated line must not be split on the commas inside an address."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    result = {
        "type": "FALLBACK_ADDRESS",
        "address": "12 MG Road, Pune, 411001",
        "order_rows": [],
    }
    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, result)
    writer.write_result(12328, result)

    assert _read(path) == ["12328\t12 MG Road, Pune, 411001"]


def test_fill_address_keeps_one_value_when_an_id_is_added_later(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\t12 MG Road, Pune\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_id": "3420026566347011"})

    assert _read(path) == ["12328\t3420026566347011"]


def test_fill_collapses_a_multiline_address_onto_one_line(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "FALLBACK_ADDRESS",
            "address": "12 MG Road\nFlat 4\nPune",
            "order_rows": [],
        },
    )

    assert _read(path) == ["12328\t12 MG Road Flat 4 Pune"]


def test_fill_adds_a_name_to_a_new_list(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12329,
        {"type": "FALLBACK_NAME", "name": "Priya Sharma", "order_rows": []},
    )

    assert _read(path) == ["12329\tPriya Sharma"]


def test_fill_never_writes_two_ids_on_one_line(tmp_path):
    """Two nameless orders: no id, the chat address, and the chat is flagged."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "OK",
            "order_ids": ["1111222233334444", "5555666677778888"],
            "name": "Priya Sharma",
            "address": "12 MG Road, Pune",
            "order_rows": [
                {"value": "1111222233334444", "status": "OK"},
                {"value": "5555666677778888", "status": "OK"},
            ],
        },
    )

    assert _read(path) == ["12328\t12 MG Road, Pune"]
    assert writer.last_notice
    assert "1111222233334444" not in _read(path)[0]
    assert "5555666677778888" not in _read(path)[0]


# ----------------------------------------------------------------------
# Several orders: the name when every order shares it, an error otherwise
# ----------------------------------------------------------------------


def _two_orders(first_name: str, second_name: str) -> dict:
    return {
        "type": "OK",
        "order_ids": ["1111222233334444", "5555666677778888"],
        "order_rows": [
            {"value": "1111222233334444", "status": "OK", "name": first_name},
            {"value": "5555666677778888", "status": "OK", "name": second_name},
        ],
    }


def test_fill_writes_the_name_when_every_order_has_the_same_customer(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, _two_orders("Priya Sharma", "Priya Sharma"))

    assert _read(path) == ["12328\tPriya Sharma"]


def test_fill_uses_the_most_common_name_when_the_names_differ(tmp_path):
    """Mixed names: the name that was actually sent is used, and flagged."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, _two_orders("Priya Sharma", "Ravi Kumar"))

    assert _read(path) == ["12328\tPriya Sharma"]
    assert "names differ" in writer.last_notice
    assert "Priya Sharma" in writer.last_notice
    assert writer.last_notice_kind == "MULTI: names differ"


def test_fill_uses_the_only_name_when_another_order_has_none(tmp_path):
    """One order has a name, the other does not: use the name, do not blank."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, _two_orders("Priya Sharma", ""))

    assert _read(path) == ["12328\tPriya Sharma"]
    assert "without a name" in writer.last_notice
    assert writer.last_notice_kind == "MULTI: name missing"


def test_fill_treats_case_and_spacing_as_the_same_customer(tmp_path):
    """OCR noise in case / spacing must not defeat the rule."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, _two_orders("Priya  Sharma", " PRIYA SHARMA "))

    assert _read(path) == ["12328\tPriya Sharma"]


def test_fill_uses_the_majority_name_when_three_orders_match_and_one_does_not(
    tmp_path,
):
    """3 x Priya + 1 x Ravi -> the majority name, flagged in the app."""
    path = tmp_path / "list.txt"
    path.write_text("12332\n", encoding="utf-8")

    result = {
        "type": "OK",
        "order_ids": ["1111111111111111", "2222222222222222",
                      "3333333333333333", "4444444444444444"],
        "order_rows": [
            {"value": "1111111111111111", "status": "OK", "name": "Priya Sharma"},
            {"value": "2222222222222222", "status": "OK", "name": "Priya Sharma"},
            {"value": "3333333333333333", "status": "OK", "name": "Priya Sharma"},
            {"value": "4444444444444444", "status": "OK", "name": "Ravi Kumar"},
        ],
    }

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12332, result)

    assert _read(path) == ["12332\tPriya Sharma"]
    assert "4 orders" in writer.last_notice
    assert "names differ" in writer.last_notice
    assert writer.last_notice_kind == "MULTI: names differ"


def test_fill_uses_the_address_when_no_order_has_a_name(tmp_path):
    """Several orders, no names: the address, never one of the order ids."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    result = {
        "type": "OK",
        "order_rows": [
            {"value": "1111111111111111", "status": "OK",
             "address": "12 MG Road, Pune"},
            {"value": "2222222222222222", "status": "OK",
             "address": "12 MG Road, Pune"},
        ],
    }

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, result)

    assert _read(path) == ["12328\t12 MG Road, Pune"]
    assert "no customer name detected" in writer.last_notice
    assert writer.last_notice_kind == "MULTI: no name"


def test_fill_uses_the_address_when_the_rows_carry_no_usable_id(tmp_path):
    """Only a short crop and an address: the address is the last resort."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    result = {
        "type": "TRUNC",
        "order_rows": [
            {"value": "34218", "status": "TRUNC",
             "address": "12 MG Road, Pune"},
        ],
    }

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, result)

    assert _read(path) == ["12328\t12 MG Road, Pune"]


def test_fill_uses_the_name_when_four_orders_share_one_customer(tmp_path):
    """4 orders, all Priya Sharma -> the name, no order ids."""
    path = tmp_path / "list.txt"
    path.write_text("12332\n", encoding="utf-8")

    result = {
        "type": "OK",
        "order_ids": ["1111111111111111", "2222222222222222",
                      "3333333333333333", "4444444444444444"],
        "order_rows": [
            {"value": "1111111111111111", "status": "OK", "name": "Priya Sharma"},
            {"value": "2222222222222222", "status": "OK", "name": "Priya Sharma"},
            {"value": "3333333333333333", "status": "OK", "name": "Priya  Sharma"},
            {"value": "4444444444444444", "status": "OK", "name": "PRIYA SHARMA"},
        ],
    }

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12332, result)

    assert _read(path) == ["12332\tPriya Sharma"]
    assert writer.last_notice == ""


def test_fill_keeps_a_single_order_id_even_with_a_name(tmp_path):
    """The rule needs two orders - one order keeps its id."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "OK",
            "order_ids": ["1111222233334444"],
            "order_rows": [
                {"value": "1111222233334444", "status": "OK", "name": "Priya"},
            ],
        },
    )

    assert _read(path) == ["12328\t1111222233334444"]


def test_fill_name_never_overwrites_an_existing_id(tmp_path):
    """A line that already holds a real id keeps it."""
    path = tmp_path / "list.txt"
    path.write_text("12328\t1111222233334444\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, _two_orders("Priya Sharma", "Priya Sharma"))

    assert _read(path) == ["12328\t1111222233334444"]


# ----------------------------------------------------------------------
# Cropped order ids: the id is kept from six digits on
# ----------------------------------------------------------------------


def test_fill_writes_a_cropped_id_of_six_digits(tmp_path):
    """A screenshot cut at the back still carries the order id."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "TRUNC",
            "truncated_ids": ["342187"],
            "order_rows": [
                {"value": "342187", "status": "TRUNC", "name": "Priya Sharma"},
            ],
        },
    )

    assert _read(path) == ["12328\t342187"]
    assert writer.last_notice == ""


def test_fill_ignores_a_crop_shorter_than_six_digits(tmp_path):
    """Five digits are not an id: the name fills the line instead."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "TRUNC",
            "truncated_ids": ["34218"],
            "order_rows": [
                {"value": "34218", "status": "TRUNC", "name": "Priya Sharma"},
            ],
        },
    )

    assert _read(path) == ["12328\tPriya Sharma"]


def test_fill_uses_the_one_name_when_the_other_orders_are_cropped(tmp_path):
    """1 name + 2 cropped screenshots: use the name, flag it in the app."""
    path = tmp_path / "list.txt"
    path.write_text("12328\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(
        12328,
        {
            "type": "OK",
            "order_rows": [
                {"value": "3420026566347011", "status": "OK",
                 "name": "Priya Sharma"},
                {"value": "3420087259994548", "status": "TRUNC"},
                {"value": "3420080037557384", "status": "TRUNC"},
            ],
        },
    )

    assert _read(path) == ["12328\tPriya Sharma"]
    assert "2 without a name" in writer.last_notice
    assert "Priya Sharma" in writer.last_notice
    assert writer.last_notice_kind == "MULTI: name missing"


# ----------------------------------------------------------------------
# The seller's own id text is authoritative: only the value is added
# ----------------------------------------------------------------------


def test_fill_keeps_the_sellers_own_id_text(tmp_path):
    """Indentation, leading zeros and the separator are left exactly as typed."""
    path = tmp_path / "list.txt"
    path.write_text("  12328\n12329,old\n012330\n", encoding="utf-8")

    writer = NotepadWriter(str(path), mode="fill")
    writer.write_result(12328, {"type": "OK", "order_ids": ["3420026566347011"]})
    writer.write_result(12329, {"type": "OK", "order_ids": ["341999308084544"]})
    writer.write_result(12330, {"type": "FALLBACK_NAME", "name": "punitha"})

    assert _read(path) == [
        "  12328\t3420026566347011",   # leading spaces kept
        "12329,341999308084544",       # comma separator kept
        "012330\tpunitha",             # leading zero kept
    ]
