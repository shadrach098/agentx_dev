import pytest

from shop.inventory import Inventory, OutOfStock
from shop.orders import Order
from shop.pricing import apply_discount, format_cents


def make_order():
    return Order(Inventory({"mug": 5, "tee": 2}))


def test_adding_an_item_takes_it_out_of_stock():
    order = make_order()
    order.add_item("mug", 2, 1200)
    assert order.inventory.available("mug") == 3


def test_cannot_order_more_than_is_in_stock():
    with pytest.raises(OutOfStock):
        make_order().add_item("tee", 3, 2500)


def test_subtotal():
    order = make_order()
    order.add_item("mug", 2, 1200)
    order.add_item("tee", 1, 2500)
    assert order.subtotal() == 4900


def test_total_with_discount():
    order = make_order()
    order.add_item("mug", 1, 999)
    assert order.total(discount_percent=15) == 849          # 849.15 cents rounds to 849
    order.add_item("mug", 1, 1)
    assert order.total(discount_percent=15) == 850          # 850.0
    assert apply_discount(1001, 15) == 851                  # 850.85 rounds up to 851


def test_cancel_puts_stock_back():
    order = make_order()
    order.add_item("mug", 2, 1200)
    order.cancel()
    assert order.inventory.available("mug") == 5 and order.status == "cancelled"


def test_format_cents():
    assert format_cents(4900) == "$49.00" and format_cents(5) == "$0.05"
