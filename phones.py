"""Turn the many ways people type an Indian mobile number into one format: +91XXXXXXXXXX (E.164)."""
import re


def normalize_indian_mobile(raw: str) -> str:
    # shortcut: Indian mobiles only (Huvo's market); use the `phonenumbers` library if UAE/other countries are needed.
    digits = re.sub(r"\D", "", raw)  # drop spaces, dashes, brackets, '+'

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]  # 919876543210 or +91 98765 43210
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # 09876543210 (trunk prefix)

    # Indian mobile numbers are 10 digits and start with 6, 7, 8 or 9.
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ValueError(f"not a valid Indian mobile number: {raw!r}")
    return "+91" + digits
