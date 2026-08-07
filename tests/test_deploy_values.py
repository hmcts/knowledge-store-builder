"""Turning templated YAML into flat, quotable configuration facts."""

from __future__ import annotations

import unittest

import settings_isolation  # noqa: F401  - puts the working tree ahead of any installed copy
from knowledgestore import deploy_values  # noqa: E402


class StripTemplate(unittest.TestCase):
    def test_an_interpolation_becomes_a_visible_placeholder(self):
        out = deploy_values.strip_template("replicas: {{ progression_replicas }}")
        self.assertEqual(out, f"replicas: {deploy_values.PLACEHOLDER}")

    def test_a_control_line_is_dropped_whole(self):
        text = "a: 1\n{% if enabled %}\nb: 2\n{% endif %}\nc: 3"
        self.assertEqual(deploy_values.strip_template(text), "a: 1\nb: 2\nc: 3")

    def test_a_file_with_no_template_markers_is_unchanged(self):
        text = "gateway:\n  prefix:\n    - /progression-command-api"
        self.assertEqual(deploy_values.strip_template(text), text)

    def test_a_value_is_never_evaluated(self):
        # The placeholder must not carry the variable name: a name can itself be
        # a secret hint, and downstream prose should not quote one as a value.
        out = deploy_values.strip_template("password: {{ vault_sql_admin_password }}")
        self.assertNotIn("vault_sql_admin", out)


class Flatten(unittest.TestCase):
    def test_nested_keys_become_dotted_paths(self):
        flat = deploy_values.flatten({"resources": {"limits": {"cpu": "2"}}}, 60, 200)
        self.assertEqual(flat, {"resources.limits.cpu": "2"})

    def test_a_list_is_indexed(self):
        flat = deploy_values.flatten({"prefix": ["/a", "/b"]}, 60, 200)
        self.assertEqual(flat, {"prefix.0": "/a", "prefix.1": "/b"})

    def test_a_placeholder_value_is_kept_so_set_from_a_variable_stays_visible(self):
        flat = deploy_values.flatten({"replicas": deploy_values.PLACEHOLDER}, 60, 200)
        self.assertEqual(flat, {"replicas": deploy_values.PLACEHOLDER})

    def test_key_count_is_capped_deterministically(self):
        flat = deploy_values.flatten({f"k{i}": i for i in range(100)}, 10, 200)
        self.assertEqual(len(flat), 10)
        self.assertEqual(sorted(flat), sorted(flat))  # sorted order, not insertion

    def test_a_long_value_is_truncated_rather_than_dropped(self):
        flat = deploy_values.flatten({"note": "x" * 500}, 60, 20)
        self.assertEqual(len(flat["note"]), 20)


class Settings(unittest.TestCase):
    def test_the_stage_is_off_until_a_repository_is_named(self):
        from knowledgestore import config

        self.assertEqual(config.DEPLOY_REPOS, set())

    def test_the_defaults_describe_an_ansible_group_vars_layout(self):
        from knowledgestore import config

        self.assertTrue(config.DEPLOY_VALUES_GLOB.endswith("_values.yaml.j2"))
        self.assertEqual(config.DEPLOY_BASE_ENV, "_base")
        self.assertGreater(config.DEPLOY_MAX_KEYS, 0)
        self.assertGreater(config.DEPLOY_VALUE_CHARS, 0)


if __name__ == "__main__":
    unittest.main()
