import unittest

from skill_runner.config import ModelCatalog


class ModelCatalogTests(unittest.TestCase):
    def test_normalizes_hierarchical_and_flat_models(self):
        catalog = ModelCatalog.from_mapping(
            {
                "default_model": "provider:model-a",
                "providers": {
                    "provider": {
                        "models": [
                            {
                                "id": "provider:model-a",
                                "alias": "model-a",
                                "description": "Primary",
                            }
                        ]
                    }
                },
                "models": [{"id": "legacy:model-b", "alias": "model-b"}],
            }
        )

        self.assertEqual(catalog.default_model, "provider:model-a")
        self.assertEqual(catalog.resolve("model-a"), "provider:model-a")
        self.assertEqual(catalog.resolve("model-b"), "legacy:model-b")
        self.assertEqual(catalog.resolve("unknown"), "unknown")


if __name__ == "__main__":
    unittest.main()
