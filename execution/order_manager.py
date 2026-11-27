class LiveOrderExecutionDisabled(RuntimeError):
    pass

class OrderManager:
    """Version 1 intentionally exposes no live order placement implementation."""
    def place_order(self, *args, **kwargs):
        raise LiveOrderExecutionDisabled(
            "Live order placement is disabled in Version 1. Use paper_trading.PaperExecutionEngine."
        )
    def modify_order(self, *args, **kwargs):
        raise LiveOrderExecutionDisabled("Live order modification is disabled in Version 1.")
    def cancel_order(self, *args, **kwargs):
        raise LiveOrderExecutionDisabled("Live order cancellation is disabled in Version 1.")
