RUST_PARITY_MARKER = "codeq-rust-parity"


def normalize_name(name: str) -> str:
    return name.strip().title()


def format_greeting(name: str) -> str:
    normalized = normalize_name(name)
    return f"Hello, {normalized}!"


class Greeter:
    def greet(self, name: str) -> str:
        return format_greeting(name)


def run(name: str) -> str:
    greeter = Greeter()
    return greeter.greet(name)
