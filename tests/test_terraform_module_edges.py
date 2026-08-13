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
from pathlib import Path

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

    def test_the_scheme_less_form_is_recognised(self):
        """Terraform's GitHub detector accepts a bare host and rewrites it to
        HTTPS. Requiring a scheme made 5 shared modules invisible on a real
        estate - no node, no edges, nothing (#125)."""
        for source, want in (
            (
                'source = "github.com/hmcts/ccd-module-elastic-search.git?ref=main"',
                {"ccd-module-elastic-search"},
            ),
            (
                'source = "github.com/hmcts/terraform-module-vnet-peering?ref=main"',
                {"terraform-module-vnet-peering"},
            ),
            ('source = "github.com/org/mod"', {"mod"}),
        ):
            with self.subTest(source=source):
                self.assertEqual(packages.terraform_references(source), want)

    def test_a_lookalike_host_does_not_match(self):
        """Making the scheme optional must not widen which hosts count."""
        for source in (
            'source = "notgithub.com/org/mod"',
            'source = "mygithub.com/org/mod"',
            'source = "raw.githubusercontent.com/org/mod"',
        ):
            with self.subTest(source=source):
                self.assertEqual(packages.terraform_references(source), set())

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


class EveryConsumerIsRepresentedTest(SettingsIsolated):
    """The evidence cap must drop surplus evidence, never whole repositories.

    Applied per provider instead of per (consumer, provider) pair, it dropped 290
    of 388 relationships on a real estate - and alphabetically, so the 98 that
    survived looked like a complete answer. That is the failure this file exists
    to prevent, and it is why the assertion is on the set of consumers rather
    than on a count.
    """

    def _tree(self, root, layout):
        for rel, text in layout.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for repo in {root / rel.split("/")[0] for rel in layout}:
            (repo / ".git").mkdir(parents=True, exist_ok=True)
        return sorted(d for d in root.iterdir() if (d / ".git").is_dir())

    def test_no_consuming_repository_is_dropped(self):
        import tempfile

        shared = 'source = "git@github.com:org/shared-module"\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # More consumers than MAX_EVIDENCE_FILES, deliberately, and named so
            # that alphabetical truncation would keep only the first few.
            layout = {f"repo-{i:02d}/main.tf": shared for i in range(12)}
            clones = self._tree(root, layout)
            refs = packages._module_references(clones)
            consumers = {consumer for consumer, provider in refs if provider == "shared-module"}
            self.assertEqual(
                len(consumers),
                12,
                "a consuming repository was dropped, so the estate would look less "
                "coupled than it is",
            )

    def test_surplus_evidence_within_one_pair_is_what_gets_capped(self):
        import tempfile

        shared = 'source = "git@github.com:org/shared-module"\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = {f"one-repo/f{i:02d}.tf": shared for i in range(9)}
            clones = self._tree(root, layout)
            refs = packages._module_references(clones)
            self.assertEqual(list(refs), [("one-repo", "shared-module")])
            self.assertEqual(len(refs[("one-repo", "shared-module")]), 9, "all files recorded")

    def test_terraform_download_cache_is_ignored(self):
        """.terraform holds copies of upstream modules; their sources are not this
        repository's dependencies."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clones = self._tree(
                root,
                {
                    "app/main.tf": 'source = "git@github.com:org/real-dep"\n',
                    "app/.terraform/modules/x/main.tf": 'source = "git@github.com:org/not-ours"\n',
                },
            )
            providers = {provider for _, provider in packages._module_references(clones)}
            self.assertEqual(providers, {"real-dep"})


class EstateMembershipTest(SettingsIsolated):
    """A referenced module is not automatically a repository the estate holds.

    On a real estate 3 of 33 referenced modules were not synced repositories.
    Naming one in a node's `repo` adds it to every per-repository aggregate - the
    community digests count it, and `deepdive` would offer a dossier on a single
    synthetic node - so the store would claim to hold something it has never seen.
    The reference itself is kept, because depending on something the estate does
    not hold is a finding rather than noise.
    """

    def test_a_provider_inside_the_estate_keeps_its_repo(self):
        node = packages._module_node("cnp-module-key-vault", in_estate=True)
        self.assertEqual(node["repo"], "cnp-module-key-vault")
        self.assertTrue(node["metadata"]["provider_in_estate"])

    def test_a_provider_outside_the_estate_claims_no_repository(self):
        node = packages._module_node("aks-module-genesis", in_estate=False)
        self.assertEqual(node["repo"], "", "the store would claim a repository it has not synced")
        self.assertFalse(node["metadata"]["provider_in_estate"])

    def test_the_reference_is_still_recorded_either_way(self):
        """Dropping it would lose the dependency evidence, which is the point."""
        for in_estate in (True, False):
            with self.subTest(in_estate=in_estate):
                node = packages._module_node("some-module", in_estate=in_estate)
                self.assertEqual(node["label"], "some-module")
                self.assertEqual(node["metadata"]["provider_repo"], "some-module")


if __name__ == "__main__":
    unittest.main()
