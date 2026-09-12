"""Run with python -m unittest discover -s scripts -p test_model_catalog.py."""
import tempfile
import unittest
from pathlib import Path

from backend.model_catalog import _scan_models_uncached


class ModelCatalogTests(unittest.TestCase):
    def scan(self, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            return _scan_models_uncached(root)

    def test_shared_projector_for_quantizations(self):
        rows = self.scan([
            "vendor/gemma-4-12B-it-Q4_K_M.gguf",
            "vendor/gemma-4-12B-it-Q6_K.gguf",
            "vendor/mmproj-gemma-4-12B-it-BF16.gguf",
        ])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["has_mmproj"] for row in rows))

    def test_unrelated_model_does_not_inherit_projector(self):
        rows = self.scan([
            "gemma-4-12B-it-Q4_K_M.gguf",
            "gemma-4-12B-it-abliterated.Q4_K_M.gguf",
            "mmproj-gemma-4-12B-it-BF16.gguf",
        ])
        by_name = {row["label"]: row for row in rows}
        self.assertTrue(by_name["gemma-4-12B-it-Q4_K_M"]["has_mmproj"])
        self.assertFalse(by_name["gemma-4-12B-it-abliterated.Q4_K_M"]["has_mmproj"])

    def test_projector_suffix_and_multiple_families(self):
        rows = self.scan([
            "Huihui-Qwen3.5-4B-abliterated.Q4_K_M.gguf",
            "Huihui-Qwen3.5-4B-abliterated.Q6_K.gguf",
            "Huihui-Qwen3.5-4B-abliterated.mmproj-f16.gguf",
            "text-4B-Q4_K_M.gguf",
        ])
        self.assertEqual(sum(row["has_mmproj"] for row in rows), 2)

    def test_generic_projector_requires_single_model(self):
        names = ["vision-4B-Q4_K_M.gguf", "mmproj-model-bf16.gguf"]
        self.assertTrue(self.scan(names)[0]["has_mmproj"])
        self.assertFalse(any(row["has_mmproj"] for row in self.scan(names + ["text-8B.gguf"])))


if __name__ == "__main__":
    unittest.main()
