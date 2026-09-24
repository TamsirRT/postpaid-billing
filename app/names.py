"""Name normalization, ported line for line from SH_Invoicing1.4.html's normalizeName().

"Mary-Kate O'Brien Jr." -> "mary|o'brien": first token + last token, lowercased,
with _ - , treated as spaces, periods removed, and generational suffixes
(Jr, Sr, II-VIII) dropped when more than two tokens remain.
"""
import re

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi", "vii", "viii"}


def normalize_name(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("_", " ").replace("-", " ").replace(",", " ").replace(".", "")
    tokens = [t for t in re.split(r"\s+", s) if t]
    if not tokens:
        return None
    while len(tokens) > 2 and tokens[-1].lower() in _SUFFIXES:
        tokens = tokens[:-1]
    return tokens[0].lower() + "|" + tokens[-1].lower()


def student_name_key(first, last):
    return normalize_name(f"{first or ''} {last or ''}")
