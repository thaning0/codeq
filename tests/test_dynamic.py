from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeq.dynamic import classify_dynamic_reference


class DynamicReferenceTests(unittest.TestCase):
    def _classify(self, suffix: str, source: str, symbol: str, line: int, column: int = 1):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"sample{suffix}"
            path.write_text(source, encoding="utf-8")
            return classify_dynamic_reference(
                {"path": str(path), "line": line, "column": column},
                symbol,
            )

    def test_python_callback_argument(self):
        result = self._classify(
            ".py",
            "def handler():\n    pass\n\nschedule(handler)\n",
            "handler",
            4,
            10,
        )
        assert result is not None
        self.assertEqual(result["reason"], "callback_argument")
        self.assertEqual(result["confidence"], "possible")

    def test_python_mapping_value(self):
        result = self._classify(
            ".py",
            "def handler():\n    pass\n\nHANDLERS = {'open': handler}\n",
            "handler",
            4,
            21,
        )
        assert result is not None
        self.assertEqual(result["reason"], "mapping_value")

    def test_python_direct_call_is_not_dynamic(self):
        result = self._classify(
            ".py",
            "def handler():\n    pass\n\nhandler()\n",
            "handler",
            4,
            1,
        )
        self.assertIsNone(result)

    def test_python_direct_method_call_is_not_confused_with_same_named_argument(self):
        source = (
            "class Runner:\n"
            "    def update(self, value):\n"
            "        pass\n\n"
            "def feed(runner, update):\n"
            "    runner.update(update)\n"
        )
        result = self._classify(".py", source, "update", 6, 12)
        self.assertIsNone(result)

    def test_python_typing_cast_is_not_dynamic(self):
        source = (
            "from typing import cast\n"
            "class Runner:\n"
            "    pass\n\n"
            "def f(value):\n"
            "    return cast(Runner, value)\n"
        )
        result = self._classify(".py", source, "Runner", 6, 17)
        self.assertIsNone(result)

    def test_typescript_event_callback(self):
        result = self._classify(
            ".ts",
            "function onChange() {}\nwindow.addEventListener('change', onChange);\n",
            "onChange",
            2,
            35,
        )
        assert result is not None
        self.assertEqual(result["reason"], "callback_argument")

    def test_typescript_mapping_value(self):
        result = self._classify(
            ".ts",
            "function handleFoo() {}\nconst handlers = { foo: handleFoo };\n",
            "handleFoo",
            2,
            25,
        )
        assert result is not None
        self.assertEqual(result["reason"], "mapping_value")

    def test_typescript_direct_call_is_not_dynamic(self):
        result = self._classify(
            ".ts",
            "function handleFoo() {}\nhandleFoo();\n",
            "handleFoo",
            2,
            1,
        )
        self.assertIsNone(result)

    def test_typescript_type_annotation_is_not_dynamic(self):
        result = self._classify(
            ".ts",
            "type Handler = () => void;\nconst value: Handler = () => {};\n",
            "Handler",
            2,
            14,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
