from collections.abc import Callable


def dispatch_order(order_id: str) -> str:
    return order_id.upper()


def invoke(callback: Callable[[str], str]) -> str:
    return callback("order-1")


CALLBACKS = {"order": dispatch_order}
alias = dispatch_order
callback_result = invoke(dispatch_order)
direct_result = dispatch_order("order-2")
