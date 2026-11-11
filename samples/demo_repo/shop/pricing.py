"""Order pricing rules."""
from dataclasses import dataclass

COUPONS = {"WELCOME10": 10, "FESTIVE25": 25}
BULK_THRESHOLD = 10


@dataclass
class Item:
    sku: str
    unit_price: float
    qty: int


def subtotal(items: list[Item]) -> float:
    return round(sum(i.unit_price * i.qty for i in items), 2)


def apply_discount(amount: float, percent: float) -> float:
    """percent is 0-100."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(amount * (1 - percent / 100), 2)


def order_total(items: list[Item], coupon: str | None = None) -> float:
    total = subtotal(items)
    # bulk orders get 5% off
    if sum(i.qty for i in items) > BULK_THRESHOLD:
        total = apply_discount(total, 0.05)
    if coupon:
        total = apply_discount(total, COUPONS[coupon.upper()])
    return total
