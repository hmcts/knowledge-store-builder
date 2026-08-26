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

    def test_key_count_is_capped_by_sorted_key_not_insertion_order(self):
        # The cap has to be stable across builds or the committed graph churns for
        # no reason. Insertion order would give k0..k9; sorted order gives
        # k0, k1, k10, k11 ... because these are strings, and that difference is
        # the whole point of the assertion.
        flat = deploy_values.flatten({f"k{i}": i for i in range(100)}, 10, 200)
        self.assertEqual(len(flat), 10)
        self.assertEqual(
            sorted(flat), ["k0", "k1", "k10", "k11", "k12", "k13", "k14", "k15", "k16", "k17"]
        )

    def test_a_long_value_is_truncated_rather_than_dropped(self):
        flat = deploy_values.flatten({"note": "x" * 500}, 60, 20)
        self.assertEqual(len(flat["note"]), 20)


class StripTemplateDollarDialect(unittest.TestCase):
    """The second interpolation dialect, `${ }` (#88)."""

    def test_a_dollar_brace_interpolation_loses_the_variable_name(self):
        """Fails if `${ }` stops being stripped, which publishes the variable name.

        The property is the *absence* of the name, not the presence of the
        placeholder: a substitution that inserted the placeholder while leaving
        `${name}` beside it would satisfy a presence check and fail this one.
        """
        out = deploy_values.strip_template("password: ${service_admin_credential}")
        self.assertEqual(out, f"password: {deploy_values.PLACEHOLDER}")
        self.assertNotIn("service_admin_credential", out)

    def test_both_dialects_withhold_the_same_name_identically(self):
        """Fails if one dialect is handled and the other is not - the #88 defect.

        One equivalence assertion rather than two per-dialect ones, so the two
        cannot drift: a change that strips Jinja and forgets `${ }` fails here
        even if every other assertion in this file is updated to match it.
        """
        jinja = "credentials:\n  password: {{ service_admin_credential }}\n"
        dollar = "credentials:\n  password: ${service_admin_credential}\n"
        stripped_jinja = deploy_values.strip_template(jinja)
        stripped_dollar = deploy_values.strip_template(dollar)
        self.assertEqual(stripped_jinja, stripped_dollar)
        self.assertNotIn("service_admin_credential", stripped_jinja)
        self.assertNotIn("service_admin_credential", stripped_dollar)

    def test_an_interpolation_spanning_lines_is_stripped_whole(self):
        """Fails if the `${ }` pattern is compiled without re.DOTALL.

        Without it the match stops at the newline, so the name on the next line
        survives - the same allowance the Jinja patterns already make.
        """
        out = deploy_values.strip_template("password: ${\n  service_admin_credential\n}")
        self.assertEqual(out, f"password: {deploy_values.PLACEHOLDER}")
        self.assertNotIn("service_admin_credential", out)

    def test_an_escaped_interpolation_is_withheld_rather_than_kept_literal(self):
        """Fails if `$${ }` is exempted as an escaped literal.

        Deliberate choice: some tools read `$${x}` as the literal text `${x}`,
        deferring interpolation to whatever consumes the rendered file. Deferred
        is not cancelled - the name still says where something is held - so the
        whole run including both dollars is withheld, leaving no stray `$`.
        """
        out = deploy_values.strip_template("token: $${deferred_signing_key}")
        self.assertEqual(out, f"token: {deploy_values.PLACEHOLDER}")
        self.assertNotIn("deferred_signing_key", out)

    def test_a_bare_dollar_name_is_left_alone(self):
        """Fails if bare `$VAR` is brought into scope, rewriting ordinary values.

        The sensitivity control for this dialect: `$` followed by word
        characters has no terminator and occurs in shell fragments, regexes and
        prices, so matching it would redact configuration facts that are not
        interpolation at all.
        """
        text = "command: echo $HOME\npattern: 'trailing$'\nnote: costs $5 per unit"
        self.assertEqual(deploy_values.strip_template(text), text)


class SecretReferences(unittest.TestCase):
    """Keep the variable, withhold where the secret lives (#88)."""

    def test_a_reference_keeps_the_variable_and_withholds_store_and_entry(self):
        """Fails if a store name or entry path reaches the flattened output.

        That output is committed, so publishing both halves publishes a map of
        where a credential lives. The variable name is kept deliberately: that a
        service takes this value from a store is an architectural fact.
        """
        flat = deploy_values.flatten(
            {
                "env": {
                    "DATABASE_PASSWORD": {
                        "secretStore": "shared-platform-store",
                        "key": "billing/production/database#password",
                    }
                }
            },
            60,
            200,
        )
        self.assertEqual(
            flat,
            {
                "env.DATABASE_PASSWORD.key": deploy_values.PLACEHOLDER,
                "env.DATABASE_PASSWORD.secretStore": deploy_values.PLACEHOLDER,
            },
        )
        published = "\n".join(f"{key}: {value}" for key, value in flat.items())
        self.assertNotIn("shared-platform-store", published)
        self.assertNotIn("billing/production/database", published)
        self.assertIn("DATABASE_PASSWORD", published)

    def test_a_nested_store_reference_is_withheld_whole(self):
        """Fails if a store named by a nested mapping is walked into and published.

        The store half is often a mapping of its own, so withholding only scalar
        values would leave the store's name one level down, reachable and
        keyed under a name that reads as harmless structure.
        """
        flat = deploy_values.flatten(
            {
                "OUTBOUND_API_TOKEN": {
                    "secretStoreRef": {"name": "shared-platform-store", "kind": "ClusterStore"},
                    "path": "billing/production/outbound#token",
                }
            },
            60,
            200,
        )
        self.assertEqual(
            flat,
            {
                "OUTBOUND_API_TOKEN.path": deploy_values.PLACEHOLDER,
                "OUTBOUND_API_TOKEN.secretStoreRef": deploy_values.PLACEHOLDER,
            },
        )
        published = "\n".join(f"{key}: {value}" for key, value in flat.items())
        self.assertNotIn("shared-platform-store", published)
        self.assertNotIn("billing/production/outbound", published)

    def test_an_ordinary_mapping_is_untouched(self):
        """Fails if the policy widens to mappings that name no store.

        The sensitivity control: a policy that redacts everything is as useless
        as one that redacts nothing. `path` and `key` are ordinary
        configuration words on their own, and a store named with no entry beside
        it is not a map of where a credential lives.
        """
        flat = deploy_values.flatten(
            {
                "gateway": {"path": "/billing", "key": "x-request-id"},
                "artifacts": {"artifactStore": "registry.example"},
                "resources": {"limits": {"cpu": "2"}},
            },
            60,
            200,
        )
        self.assertEqual(
            flat,
            {
                "artifacts.artifactStore": "registry.example",
                "gateway.key": "x-request-id",
                "gateway.path": "/billing",
                "resources.limits.cpu": "2",
            },
        )

    def test_the_primitive_is_callable_without_flattening(self):
        """Fails if the policy is reachable only through `flatten`.

        A second deployment format may keep the nested structure rather than
        flatten it, and #88's point is that both formats inherit one answer
        rather than each parser making its own.
        """
        withheld = deploy_values.withhold_secret_locations(
            {
                "DATABASE_PASSWORD": {"store": "shared-platform-store", "entry": "billing/db"},
                "replicas": 2,
            }
        )
        self.assertEqual(
            withheld,
            {
                "DATABASE_PASSWORD": {
                    "store": deploy_values.PLACEHOLDER,
                    "entry": deploy_values.PLACEHOLDER,
                },
                "replicas": 2,
            },
        )

    def test_the_policy_keeps_the_sorted_cap_and_ignores_insertion_order(self):
        """Fails if the policy rebuilds mappings in a way the cap then depends on.

        `flatten` caps by sorted key so two runs keep the same keys; a policy
        that dropped or renamed keys, or that let insertion order through, would
        churn the committed graph between builds.
        """
        forwards = {
            "env": {
                "B_TOKEN": {"secretStore": "one", "key": "billing/b"},
                "A_TOKEN": {"secretStore": "two", "key": "billing/a"},
            }
        }
        backwards = {
            "env": {
                "A_TOKEN": {"key": "billing/a", "secretStore": "two"},
                "B_TOKEN": {"key": "billing/b", "secretStore": "one"},
            }
        }
        expected = {
            "env.A_TOKEN.key": deploy_values.PLACEHOLDER,
            "env.A_TOKEN.secretStore": deploy_values.PLACEHOLDER,
            "env.B_TOKEN.key": deploy_values.PLACEHOLDER,
        }
        self.assertEqual(deploy_values.flatten(forwards, 3, 200), expected)
        self.assertEqual(deploy_values.flatten(backwards, 3, 200), expected)


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

    def test_the_deploy_extra_declares_pyyaml(self):
        """CI pins PyYAML directly, so nothing else would notice the extra changing.

        The pin exists because a resolved-at-install dependency makes a CI run
        prove something different each time. The cost is that the pin and the
        extra can drift apart silently - CI would keep installing PyYAML and
        passing while the extra named something else, or nothing.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r"^deploy\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(declared, "pyproject.toml declares no `deploy` extra")
        self.assertIn("PyYAML", declared.group(1))

        workflow = (root / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("PyYAML==", workflow, "CI must pin the version it installs")


if __name__ == "__main__":
    unittest.main()
