from app import Greeter, format_greeting


def test_format_greeting() -> None:
    assert format_greeting(" ada ") == "Hello, Ada!"


def test_greeter() -> None:
    assert Greeter().greet("lin") == "Hello, Lin!"
