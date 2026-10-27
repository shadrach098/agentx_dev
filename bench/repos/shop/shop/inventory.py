"""Stock levels per product (SKU)."""


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, stock=None):
        self.stock = dict(stock or {})

    def available(self, sku):
        return self.stock.get(sku, 0)

    def reserve(self, sku, quantity):
        """Take ``quantity`` of ``sku`` out of stock for an order."""
        if self.available(sku) < quantity:
            raise OutOfStock(f"only {self.available(sku)} of {sku} left")
        self.stock[sku] = self.available(sku) - quantity

    def restore(self, sku, quantity):
        """Put ``quantity`` of ``sku`` back, for a cancelled order."""
        self.stock[sku] = self.available(sku) + quantity
