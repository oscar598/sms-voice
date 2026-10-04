"""The one SMS length rule. Every outbound message passes through fit().

SMS_MAX_CHARS (.env, default 160) is the longest SMS any part of the code may
send: one GSM-7 segment. Messages are written to fit; fit() is the last line of
defence. It maps characters outside the GSM-7 alphabet (curly quotes, dashes,
emoji) to plain ones, since a single such character turns the whole SMS into
UCS-2 at 70 characters per segment, and shortens at a word boundary if needed.
"""

import os
import re
import unicodedata

GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
REPLACE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", " ": " ", "\t": " "}


def limit():
    """Read when called, so a value set in .env applies even if .env loads late."""
    return int(os.environ.get("SMS_MAX_CHARS", "160"))


def to_gsm(text):
    out = []
    for ch in text:
        ch = REPLACE.get(ch, ch)
        if all(c in GSM7 for c in ch):
            out.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch)[:1]  # "ç" -> "c", "ő" -> "o"; emoji -> dropped
        out.append(base if base in GSM7 else "")
    return re.sub(r" {2,}", " ", "".join(out)).strip()


def fit(text, max_chars=None):
    """GSM-7 text of at most max_chars (default limit()), shortened at a word if it has to be."""
    max_chars = max_chars or limit()
    text = to_gsm(text or "")
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 3]
    if " " in cut[max_chars // 2:]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "..."
