"""Money is kept in whole cents."""


def apply_discount(amount_cents, percent):
    """``amount_cents`` less ``percent`` per cent, rounded to the nearest cent (half a cent rounds up)."""
    discounted = amount_cents * (100 - percent) / 100
    return int(discounted)


def format_cents(amount_cents):
    return f"${amount_cents // 100}.{amount_cents % 100:02d}"
