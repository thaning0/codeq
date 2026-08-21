from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeq.topology import extract_imports, resolve_import_specifier


class TopologyTests(unittest.TestCase):
    def test_python_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(
                "import os\nfrom app.services.foo import Foo, bar as baz\n",
                encoding="utf-8",
            )
            imports = extract_imports(path)
            self.assertEqual([item["specifier"] for item in imports], ["os", "app.services.foo"])
            self.assertEqual(imports[1]["names"], ["Foo", "baz"])

    def test_typescript_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.ts"
            path.write_text(
                "import { fetchBars } from '@/features/market/api';\n"
                "export { Thing } from './thing';\n"
                "const lazy = import('./lazy');\n",
                encoding="utf-8",
            )
            imports = extract_imports(path)
            self.assertEqual(
                [item["specifier"] for item in imports],
                ["@/features/market/api", "./thing", "./lazy"],
            )

    def test_typescript_tsconfig_path_alias_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tsconfig.json").write_text(
                '{"compilerOptions":{"paths":{"@/*":["./src/*"]}}}',
                encoding="utf-8",
            )
            target = root / "src/features/market/api.ts"
            target.parent.mkdir(parents=True)
            target.write_text("export const fetchBars = 1;\n", encoding="utf-8")
            importer = root / "src/use.ts"
            importer.write_text("import { fetchBars } from '@/features/market/api';\n", encoding="utf-8")
            self.assertEqual(
                resolve_import_specifier(importer, "@/features/market/api", root),
                [target.resolve()],
            )

    def test_python_src_module_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src/app/services/foo.py"
            target.parent.mkdir(parents=True)
            target.write_text("class Foo: pass\n", encoding="utf-8")
            importer = root / "src/app/use.py"
            importer.write_text("from app.services.foo import Foo\n", encoding="utf-8")
            self.assertEqual(
                resolve_import_specifier(importer, "app.services.foo", root),
                [target.resolve()],
            )


if __name__ == "__main__":
    unittest.main()
