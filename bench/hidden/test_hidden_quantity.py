import pytest

from shop.inventory import Inventory
from shop.orders import Order


@pytest.mark.parametrize("quantity", [0, -1, -5])
def test_a_quantity_below_one_is_refused_and_stock_is_untouched(quantity):
    order = Order(Inventory({"mug": 5}))
    with pytest.raises(ValueError):
        order.add_item("mug", quantity, 1200)
    assert order.inventory.available("mug") == 5 and order.lines == []


def test_one_is_fine():
    order = Order(Inventory({"mug": 5}))
    order.add_item("mug", 1, 1200)
    assert order.inventory.available("mug") == 4
