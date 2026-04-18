"""Tests for scripts/refresh_pricing.py — CSV parsing logic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Import the script as a module without executing main().
_SCRIPT = Path(__file__).parent.parent / "scripts" / "refresh_pricing.py"
_spec = importlib.util.spec_from_file_location("refresh_pricing", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_parse_price = _mod._parse_price
_parse_bool = _mod._parse_bool
_parse_cent = _mod._parse_cent
parse = _mod.parse


# ── _parse_price ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("",      None),
    ("-",     None),
    ("  -  ", None),
    ("no public pricing",          None),
    ("No Public Pricing",          None),
    ("0",     0.0),
    ("447",   447.0),
    ("3600",  3600.0),
    ("3600.5", 3600.5),
    ("  500  ", 500.0),
])
def test_parse_price(val, expected):
    assert _parse_price(val) == expected


# ── _parse_bool ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("Yes",      True),
    ("yes",      True),
    ("YES",      True),
    ("No",       False),
    ("no",       False),
    ("insecure", False),
    ("INSECURE", False),
    ("?",        None),
    ("",         None),
    ("n/a",      None),
])
def test_parse_bool(val, expected):
    assert _parse_bool(val) == expected


# ── _parse_cent ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("",     None),
    ("-",    None),
    ("0.0",  0.0),
    ("4.2",  4.2),
    ("11.2", 11.2),
    ("  3.5  ", 3.5),
])
def test_parse_cent(val, expected):
    assert _parse_cent(val) == expected


# ── parse() ────────────────────────────────────────────────────────────────────

# Minimal valid CSV matching the sheet structure (4 header rows + data rows).
_HEADER = (
    ",,All pricing normalised to EUR,,,,,,,,,,,,,\n"
    ",http://peering.exposed/,,,,,,,,85% util,40% util,85% util,40% util,85% util,40% util,\n"
    ",Please report inaccuracies,,,,,,,,,,,,,,,\n"
    ",IXP,Location,Secure Route servers,BCP 214,400GE price,100GE price,10G price,"
    "Ratio 400G:100G,Ratio 100G:10G,Cent/Month/Mbps,Cent/Month/Mbps,"
    "Cent/Month/Mbps,Cent/Month/Mbps,Cent/Month/Mbps,Cent/Month/Mbps,Notes\n"
)


def _make_csv(*data_rows: str) -> str:
    return _HEADER + "".join(data_rows)


def test_parse_single_entry():
    csv_text = _make_csv(
        ",AMS-IX,Amsterdam Netherlands,Yes,n/a,7600,3600,447,2.11,8.05,2.2,4.8,4.2,9.0,5.3,11.2,test note\n"
    )
    entries = parse(csv_text)
    assert len(entries) == 1
    e = entries[0]
    assert e["ixp"] == "AMS-IX"
    assert e["location"] == "Amsterdam Netherlands"
    assert e["secure_route_servers"] is True
    assert e["bcp214"] is None        # "n/a" → None
    assert e["no_public_pricing"] is False
    assert e["price_400g_eur_month"] == 7600.0
    assert e["price_100g_eur_month"] == 3600.0
    assert e["price_10g_eur_month"] == 447.0
    assert e["cost_per_mbps_400g_85pct"] == 2.2
    assert e["cost_per_mbps_100g_85pct"] == 4.2
    assert e["cost_per_mbps_10g_85pct"] == 5.3
    assert e["notes"] == "test note"


def test_parse_no_public_pricing():
    csv_text = _make_csv(
        ",DE-CIX,Global,Yes,no,no public pricing,no public pricing,no public pricing,"
        "-,-,-,-,-,-,-,-,no public pricing available\n"
    )
    entries = parse(csv_text)
    assert len(entries) == 1
    e = entries[0]
    assert e["no_public_pricing"] is True
    assert e["price_400g_eur_month"] is None
    assert e["price_100g_eur_month"] is None
    assert e["price_10g_eur_month"] is None


def test_parse_missing_prices_are_none():
    csv_text = _make_csv(
        ",FREE-IX,Testland,Yes,Yes,-,0,0,,1,,,0.0,0.0,0.0,0.0,\n"
    )
    entries = parse(csv_text)
    assert entries[0]["price_400g_eur_month"] is None
    assert entries[0]["price_100g_eur_month"] == 0.0
    assert entries[0]["price_10g_eur_month"] == 0.0


def test_parse_skips_empty_ixp_rows():
    csv_text = _make_csv(
        ",REAL-IX,Somewhere,Yes,Yes,100,50,10,2.0,5.0,0.1,0.2,0.1,0.2,0.1,0.2,\n"
        ",,,,,,,,,,,,,,,,\n"   # empty IXP name — should be skipped
    )
    entries = parse(csv_text)
    assert len(entries) == 1
    assert entries[0]["ixp"] == "REAL-IX"


def test_parse_skips_footer_note_rows():
    csv_text = _make_csv(
        ",REAL-IX,Somewhere,Yes,Yes,100,50,10,2.0,5.0,0.1,0.2,0.1,0.2,0.1,0.2,\n"
        ",,,,,,1) Assumption: utilize ports up to 85%,,,,,,,,,\n"
        ",,,,,,Notes:,,,,,,,,,\n"
    )
    entries = parse(csv_text)
    assert len(entries) == 1


def test_parse_insecure_route_server():
    csv_text = _make_csv(
        ",BAD-IX,Testland,insecure,Yes,-,200,50,,4.0,,,0.2,0.5,0.6,1.2,\n"
    )
    entries = parse(csv_text)
    assert entries[0]["secure_route_servers"] is False


def test_parse_multiple_entries_order_preserved():
    csv_text = _make_csv(
        ",ALPHA-IX,Aland,Yes,Yes,0,0,0,1,1,0,0,0,0,0,0,\n"
        ",BETA-IX,Bland,No,No,100,50,10,2,5,0.1,0.2,0.1,0.2,0.1,0.2,\n"
    )
    entries = parse(csv_text)
    assert len(entries) == 2
    assert entries[0]["ixp"] == "ALPHA-IX"
    assert entries[1]["ixp"] == "BETA-IX"


def test_parse_short_row_padded():
    # Row has fewer than 17 columns — should not raise.
    csv_text = _make_csv(",TINY-IX,Somewhere,Yes\n")
    entries = parse(csv_text)
    assert len(entries) == 1
    assert entries[0]["price_400g_eur_month"] is None
    assert entries[0]["notes"] == ""
