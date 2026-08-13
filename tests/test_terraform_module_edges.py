"""Terraform module reuse is a cross-repository dependency, and was invisible.

`packages` looks for language manifests. An infrastructure estate declares its
shared dependencies in Terraform `source` arguments instead, so one reported
"0 shared across repositories" while actually being built almost entirely from
shared modules (issue #108).

Two things this must get right, both learned from real data rather than the
issue text:

The scp-style `git@github.com:org/repo` form is not a variant to mop up - on the
estate that reported this it outnumbered the documented `git::https://` form five
to one, so handling only the documented one would have missed most of the reuse.

A reference that names no repository must produce nothing. A registry reference
(`hashicorp/consul/aws`) and a local path both name a module without naming a
repository to link to, and inventing one would be a guess presented as evidence.
"""

from __future__ import annotations

import unittest

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import build_package_edges as packages  # noqa: E402


class TerraformReferenceTest(SettingsIsolated):
    def test_the_scp_style_form_is_recognised(self):
        """The majority form on a real estate."""
        self.assertEqual(
            packages.terraform_references('source = "git@github.com:hmcts/cnp-module-key-vault"'),
            {"cnp-module-key-vault"},
        )

    def test_a_ref_parameter_does_not_become_part_of_the_name(self):
        self.assertEqual(
            packages.terraform_references('source = "git@github.com:org/mod?ref=master"'),
            {"mod"},
        )

    def test_the_documented_git_https_form_is_recognised(self):
        self.assertEqual(
            packages.terraform_references(
                'source = "git::https://github.com/org/terraform-module-sql.git?ref=main"'
            ),
            {"terraform-module-sql"},
        )

    def test_the_dot_git_suffix_is_stripped(self):
        self.assertEqual(
            packages.terraform_references('source = "git::https://github.com/org/mod.git"'),
            {"mod"},
        )

    def test_ssh_form_is_recognised(self):
        self.assertEqual(
            packages.terraform_references('source = "ssh://git@github.com/org/mod"'),
            {"mod"},
        )

    def test_whitespace_alignment_does_not_defeat_it(self):
        """Terraform files align `=`, so the spacing varies from file to file."""
        self.assertEqual(
            packages.terraform_references('source             = "git@github.com:org/mod"'),
            {"mod"},
        )

    def test_references_naming_no_repository_produce_nothing(self):
        """A guess presented as evidence is worse than a gap."""
        for source in (
            'source = "../modules/network"',
            'source = "./local"',
            'source = "hashicorp/consul/aws"',
            'source = "app.terraform.io/example/vpc/aws"',
        ):
            with self.subTest(source=source):
                self.assertEqual(packages.terraform_references(source), set())

    def test_several_references_in_one_file_are_all_found(self):
        text = (
            'module "a" {\n  source = "git@github.com:org/one"\n}\n'
            'module "b" {\n  source = "git::https://github.com/org/two.git?ref=v1"\n}\n'
        )
        self.assertEqual(packages.terraform_references(text), {"one", "two"})


if __name__ == "__main__":
    unittest.main()
