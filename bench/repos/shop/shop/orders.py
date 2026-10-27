"""An order: lines of (sku, quantity, unit price in cents), placed against an inventory."""

from shop.pricing import apply_discount


class Order:
    def __init__(self, inventory):
        self.inventory = inventory
        self.lines = []
        self.status = "open"

    def add_item(self, sku, quantity, unit_price_cents):
        self.inventory.reserve(sku, quantity)
        self.lines.append((sku, quantity, unit_price_cents))

    def subtotal(self):
        return sum(quantity * price for _, quantity, price in self.lines)

    def total(self, discount_percent=0):
        return apply_discount(self.subtotal(), discount_percent)

    def pay(self):
        if self.status != "open":
            raise ValueError(f"cannot pay an order that is {self.status}")
        self.status = "paid"

    def cancel(self):
        for sku, quantity, _ in self.lines:
            self.inventory.restore(sku, quantity)
        self.status = "cancelled"
