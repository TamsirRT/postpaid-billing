"""Compare this app's post-paid lunches with a v1.4 to_invoice CSV, for the parallel run."""
import csv
import io
from collections import Counter
from datetime import date


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def compare_with_v14(repo, institution, to_invoice_bytes, start, end):
    text = to_invoice_bytes.decode("utf-8-sig")
    v14 = {}
    for r in csv.DictReader(io.StringIO(text)):
        d = _d(r["date"])
        if start <= d <= end and r.get("Amount") not in (None, "", "NEEDS RATE"):
            v14[(r["student_id"], d)] = round(float(r["Amount"]) * 100)
    ours = {(str(r["student_id"]), _d(r["service_date"])): int(r["price_cents"])
            for r in repo.post_paid_in_range(institution["id"], start, end)}
    how = {(str(r["student_id"]), _d(r["service_date"])): (r["classification"], r["classification_note"])
           for r in repo.classifications_in_range(institution["id"], start, end)}
    both = v14.keys() & ours.keys()
    return {
        "v14_rows": len(v14), "our_rows": len(ours), "matching_rows": len(both),
        "price_mismatches": sorted((k[0], k[1].isoformat(), v14[k], ours[k]) for k in both if v14[k] != ours[k]),
        "v14_total_cents": sum(v14.values()), "our_total_cents": sum(ours.values()),
        "only_v14": Counter(how.get(k, ("not sorted", None)) for k in v14.keys() - ours.keys()),
        "only_ours": sorted((k[0], k[1].isoformat(), how.get(k, ("?", None))[1]) for k in ours.keys() - v14.keys()),
    }
