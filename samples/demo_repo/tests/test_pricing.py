import pytest

from shop.pricing import Item, apply_discount, order_total, subtotal


def test_subtotal():
    assert subtotal([Item("a", 2.5, 2), Item("b", 1.0, 1)]) == 6.0


def test_discount_bounds():
    with pytest.raises(ValueError):
        apply_discount(100, 120)


def test_welcome_coupon():
    assert order_total([Item("a", 50, 2)], "WELCOME10") == 90.0
