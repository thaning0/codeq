from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.dynamic import (
    _python_index,
    classify_dynamic_reference,
    classify_python_call_reference,
    is_python_property_definition,
)


class DynamicReferenceTests(unittest.TestCase):
    def _classify(self, suffix: str, source: str, symbol: str, line: int, column: int = 1):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"sample{suffix}"
            path.write_text(source, encoding="utf-8")
            return classify_dynamic_reference(
                {"path": str(path), "line": line, "column": column},
                symbol,
            )

    def _classify_python_call(self, source: str, symbol: str, line: int, column: int):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(source, encoding="utf-8")
            return classify_python_call_reference(
                {"path": str(path), "line": line, "column": column},
                symbol,
            )

    def test_python_direct_call_is_reusable_as_call_edge(self):
        result = self._classify_python_call(
            "def run():\n    pass\n\ndef caller():\n    run()\n",
            "run",
            5,
            5,
        )
        self.assertIs(result, True)

    def test_python_import_reference_is_a_safe_non_call(self):
        result = self._classify_python_call("from service import run\n", "run", 1, 21)
        self.assertIs(result, False)

    def test_python_callable_alias_requires_call_hierarchy_fallback(self):
        result = self._classify_python_call(
            "def run():\n    pass\n\nalias = run\n",
            "run",
            4,
            9,
        )
        self.assertIsNone(result)

    def test_python_decorator_reference_requires_call_hierarchy_fallback(self):
        result = self._classify_python_call(
            "def decorate(fn):\n    return fn\n\n@decorate\ndef run():\n    pass\n",
            "decorate",
            4,
            2,
        )
        self.assertIsNone(result)

    def test_python_call_in_default_value_requires_call_hierarchy_fallback(self):
        result = self._classify_python_call(
            "def factory():\n    return 1\n\ndef run(value=factory()):\n    return value\n",
            "factory",
            4,
            15,
        )
        self.assertIsNone(result)

    def test_python_property_definition_is_detected_from_cached_ast(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(
                "class Service:\n    @property\n    def value(self):\n        return 1\n",
                encoding="utf-8",
            )
            self.assertTrue(is_python_property_definition(path, 3, "value"))
            self.assertFalse(is_python_property_definition(path, 3, "missing"))

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

    def test_python_cached_index_avoids_repeated_ast_walks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text("handlers = {'one': handler, 'two': handler}\n", encoding="utf-8")
            _python_index.cache_clear()
            with patch("codeq.dynamic.ast.walk", wraps=__import__("ast").walk) as walk:
                first = classify_dynamic_reference(
                    {"path": str(path), "line": 1, "column": 20},
                    "handler",
                )
                second = classify_dynamic_reference(
                    {"path": str(path), "line": 1, "column": 36},
                    "handler",
                )
            self.assertEqual(first and first["reason"], "mapping_value")
            self.assertEqual(second and second["reason"], "mapping_value")
            # One full walk builds the cached candidate index; the remaining
            # walks are bounded ancestor-subtree checks for the two results.
            self.assertEqual(walk.call_count, 4)

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

    def test_typescript_generic_type_argument_is_not_dynamic(self):
        result = self._classify(
            ".ts",
            "type BarData = { close: number };\nfunction f(value: Map<string, BarData[]>): void {}\n",
            "BarData",
            2,
            31,
        )
        self.assertIsNone(result)

    def test_typescript_return_generic_type_is_not_dynamic(self):
        result = self._classify(
            ".ts",
            "type BarData = { close: number };\nfunction f(): Promise<BarData[]> { throw new Error(); }\n",
            "BarData",
            2,
            23,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
