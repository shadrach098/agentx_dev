from shop.inventory import Inventory
from shop.orders import Order


def test_cancelling_twice_restores_stock_once():
    order = Order(Inventory({"mug": 5}))
    order.add_item("mug", 2, 1200)
    order.cancel()
    order.cancel()
    assert order.inventory.available("mug") == 5
    assert order.status == "cancelled"


def test_one_cancel_still_restores():
    order = Order(Inventory({"mug": 5}))
    order.add_item("mug", 3, 1200)
    order.cancel()
    assert order.inventory.available("mug") == 5
