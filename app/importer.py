"""Parse the ordering platform's "ALL ORDERS" CSV export into rows for billing.imported_orders.

Only the institution's own rows are kept (Location Name + Module Name). Every
line of an order that has ANY line marked "Refund" is flagged is_refunded, as
v1.4 did: a refunded order doesn't count as an order.
"""
import csv
import hashlib
import io
from datetime import date

from .names import normalize_name

REQUIRED = ("Order ID", "Order Date", "Order or Refund", "Location Name", "Module Name",
            "User Name", "User ID", "Product Name")


class OrdersFileError(Exception):
    """The file can't be imported. The message is safe to show staff."""


def _clean_header(h):
    return (h or "").strip().lstrip("﻿")


def parse_orders_csv(data, location_name, module_name):
    """data: bytes or str. Returns {"rows": [...], "stats": {...}}. Raises OrdersFileError."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise OrdersFileError("The file isn't UTF-8 text. Export it again as CSV from the ordering platform.")
    else:
        text = data
    reader = csv.reader(io.StringIO(text))
    try:
        header = [_clean_header(h) for h in next(reader)]
    except StopIteration:
        raise OrdersFileError("The file is empty.")
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise OrdersFileError("This doesn't look like the ALL ORDERS export. Missing column(s): " + ", ".join(missing))
    idx = {name: header.index(name) for name in REQUIRED}

    raw = [r for r in reader if any((v or "").strip() for v in r)]
    if not raw:
        raise OrdersFileError("The file has a header but no orders.")

    def val(r, col):
        i = idx[col]
        return (r[i] if i < len(r) else "").strip()

    refunded_ids = {val(r, "Order ID") for r in raw if val(r, "Order or Refund").lower() == "refund"}

    rows, bad_dates, other_places = [], [], 0
    for r in raw:
        if val(r, "Location Name") != location_name or val(r, "Module Name") != module_name:
            other_places += 1
            continue
        d = val(r, "Order Date")
        try:
            service_date = date.fromisoformat(d)
        except ValueError:
            bad_dates.append(d)
            continue
        user_name = val(r, "User Name")
        rows.append({
            "external_order_id": val(r, "Order ID"),
            "ordering_user_id": val(r, "User ID"),
            "raw_user_name": user_name,
            "name_key": normalize_name(user_name),
            "service_date": service_date.isoformat(),
            "product_name": val(r, "Product Name") or None,
            "is_refunded": val(r, "Order ID") in refunded_ids,
            "source_row_hash": hashlib.sha256("\x1f".join(r).encode("utf-8")).hexdigest(),
        })

    if bad_dates:
        sample = ", ".join(sorted(set(bad_dates))[:3])
        raise OrdersFileError(f"{len(bad_dates)} row(s) have an Order Date that isn't YYYY-MM-DD (e.g. {sample}). "
                              "Nothing was imported.")
    if not rows:
        raise OrdersFileError(f"No rows for “{location_name}” / “{module_name}” in this file. "
                              "Check it's the right export.")
    missing_ids = sum(1 for r in rows if not r["ordering_user_id"])
    if missing_ids:
        raise OrdersFileError(f"{missing_ids} order row(s) have no User ID. Nothing was imported.")

    dates = [r["service_date"] for r in rows]
    return {"rows": rows, "stats": {
        "file_rows": len(raw), "school_rows": len(rows), "other_schools_rows": other_places,
        "refunded_rows": sum(1 for r in rows if r["is_refunded"]),
        "first_date": min(dates), "last_date": max(dates),
    }}
