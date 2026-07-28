"""Unit tests for update_app_spec_with_new_hpa_config."""

import logging
import os
import sys
import unittest

# Allow importing modules from src/ when run directly or via unittest discovery
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from argocd_updater import (
    ArgoAppUpdateStatus,
    find_helm_source,
    update_app_spec_with_new_hpa_config,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("test")


def _param_values_from_source(source):
    return {p["name"]: p["value"] for p in source["helm"]["parameters"]}


def _param_values(spec):
    return _param_values_from_source(spec["source"])


class TestUpdateAppSpecWithNewHpaConfig(unittest.TestCase):
    def test_dedupes_and_updates_helm_parameters(self):
        app_spec = {
            "source": {
                "helm": {
                    "parameters": [
                        {"name": "autoscaling.maxReplicas", "value": "4"},
                        {"name": "autoscaling.minReplicas", "value": "2"},
                        {"name": "autoscaling.enabled", "value": "true"},
                        {"name": "autoscaling.minReplicas", "value": "2"},
                        {"name": "autoscaling.maxReplicas", "value": "4"},
                        {"name": "someOther.param", "value": "keep-me"},
                    ]
                }
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "name": "in-cluster",
            },
        }
        new_hpa_config = {"minReplicas": 3, "maxReplicas": 10}

        result = update_app_spec_with_new_hpa_config(
            "my-test-app", app_spec, new_hpa_config, logger
        )

        params = result["source"]["helm"]["parameters"]
        names = [p["name"] for p in params]
        self.assertEqual(len(names), len(set(names)), f"duplicate parameter names: {names}")

        values = _param_values(result)
        self.assertEqual(values["autoscaling.minReplicas"], "3")
        self.assertEqual(values["autoscaling.maxReplicas"], "10")
        self.assertEqual(values["someOther.param"], "keep-me")

        # Both destination.server and .name were present → .server is removed
        self.assertNotIn("server", result["destination"])
        self.assertEqual(result["destination"]["name"], "in-cluster")

    def test_creates_helm_parameters_when_missing(self):
        app_spec = {
            "source": {},
            "destination": {"server": "https://kubernetes.default.svc"},
        }
        new_hpa_config = {"minReplicas": 3, "maxReplicas": 10}

        result = update_app_spec_with_new_hpa_config(
            "no-helm-app", app_spec, new_hpa_config, logger
        )

        values = _param_values(result)
        self.assertEqual(values["autoscaling.minReplicas"], "3")
        self.assertEqual(values["autoscaling.maxReplicas"], "10")
        # Only server was present → keep it
        self.assertEqual(
            result["destination"]["server"], "https://kubernetes.default.svc"
        )

    def test_multi_source_updates_helm_source_not_values_ref(self):
        """AppSet-style multi-source: helm chart + ref-only values repo."""
        app_spec = {
            "sources": [
                {
                    "repoURL": "https://github.com/org/values.git",
                    "targetRevision": "main",
                    "ref": "values",
                },
                {
                    "repoURL": "https://github.com/org/charts.git",
                    "path": "charts/demo-app",
                    "targetRevision": "main",
                    "helm": {
                        "valueFiles": ["$values/overlays/prod/values.yaml"],
                        "parameters": [
                            {"name": "autoscaling.enabled", "value": "true"},
                            {"name": "autoscaling.minReplicas", "value": "1"},
                            {"name": "autoscaling.maxReplicas", "value": "2"},
                            {"name": "nameOverride", "value": "keep-me"},
                        ],
                    },
                },
            ],
            "destination": {
                "server": "https://kubernetes.default.svc",
                "name": "in-cluster",
            },
        }
        new_hpa_config = {"minReplicas": 3, "maxReplicas": 10}

        result = update_app_spec_with_new_hpa_config(
            "multi-source-app", app_spec, new_hpa_config, logger
        )

        # Values ref source untouched
        self.assertEqual(result["sources"][0].get("ref"), "values")
        self.assertNotIn("helm", result["sources"][0])

        helm_source = result["sources"][1]
        values = _param_values_from_source(helm_source)
        self.assertEqual(values["autoscaling.minReplicas"], "3")
        self.assertEqual(values["autoscaling.maxReplicas"], "10")
        self.assertEqual(values["nameOverride"], "keep-me")
        self.assertEqual(
            helm_source["helm"]["valueFiles"],
            ["$values/overlays/prod/values.yaml"],
        )
        self.assertNotIn("server", result["destination"])

    def test_multi_source_prefers_source_with_existing_autoscale_params(self):
        app_spec = {
            "sources": [
                {
                    "repoURL": "https://github.com/org/charts.git",
                    "chart": "other",
                    "helm": {"parameters": [{"name": "replicaCount", "value": "1"}]},
                },
                {
                    "repoURL": "https://github.com/org/charts.git",
                    "path": "charts/demo-app",
                    "helm": {
                        "parameters": [
                            {"name": "autoscaling.minReplicas", "value": "1"},
                            {"name": "autoscaling.maxReplicas", "value": "2"},
                        ]
                    },
                },
            ],
            "destination": {"name": "in-cluster"},
        }

        result = update_app_spec_with_new_hpa_config(
            "multi-helm-app", app_spec, {"minReplicas": 5, "maxReplicas": 8}, logger
        )

        # First source unchanged
        self.assertEqual(
            result["sources"][0]["helm"]["parameters"],
            [{"name": "replicaCount", "value": "1"}],
        )
        values = _param_values_from_source(result["sources"][1])
        self.assertEqual(values["autoscaling.minReplicas"], "5")
        self.assertEqual(values["autoscaling.maxReplicas"], "8")

    def test_multi_source_adds_helm_params_to_chart_source(self):
        app_spec = {
            "sources": [
                {"repoURL": "https://github.com/org/values.git", "ref": "values"},
                {
                    "repoURL": "https://github.com/org/charts.git",
                    "path": "charts/demo-app",
                },
            ],
            "destination": {"name": "in-cluster"},
        }

        result = update_app_spec_with_new_hpa_config(
            "no-helm-yet", app_spec, {"minReplicas": 2, "maxReplicas": 4}, logger
        )

        self.assertIs(find_helm_source(result), result["sources"][1])
        values = _param_values_from_source(result["sources"][1])
        self.assertEqual(values["autoscaling.minReplicas"], "2")
        self.assertEqual(values["autoscaling.maxReplicas"], "4")
        self.assertNotIn("helm", result["sources"][0])

    def test_missing_spec_behaves_like_empty_spec(self):
        result = update_app_spec_with_new_hpa_config(
            "missing-spec", None, {"minReplicas": 2, "maxReplicas": 4}, logger
        )

        self.assertIn("source", result)
        values = _param_values(result)
        self.assertEqual(values["autoscaling.minReplicas"], "2")
        self.assertEqual(values["autoscaling.maxReplicas"], "4")

    def test_non_dict_source_is_rejected_without_keyerror_shape(self):
        app_spec = {"source": "not-a-dict", "destination": {"name": "in-cluster"}}

        with self.assertRaises(ValueError):
            update_app_spec_with_new_hpa_config(
                "bad-source", app_spec, {"minReplicas": 2, "maxReplicas": 4}, logger
            )

    def test_non_dict_sources_entries_are_ignored(self):
        app_spec = {
            "sources": [
                "bad-entry",
                {"ref": "values", "repoURL": "https://github.com/org/values.git"},
                {"path": "charts/demo-app"},
            ],
            "destination": {"name": "in-cluster"},
        }

        result = update_app_spec_with_new_hpa_config(
            "bad-sources-entry", app_spec, {"minReplicas": 2, "maxReplicas": 4}, logger
        )

        values = _param_values_from_source(result["sources"][2])
        self.assertEqual(values["autoscaling.minReplicas"], "2")
        self.assertEqual(values["autoscaling.maxReplicas"], "4")


if __name__ == "__main__":
    unittest.main()
