"""An estate must be able to see what it already depends on and does not hold.

The method this stage mechanises has already corrected a published finding: a
payload contract was reported to have no readable schema because its references
did not resolve, and they resolved perfectly against a repository the estate did
not hold. Ranking consumed-but-not-built coordinates by reference weight found
that repository without anyone knowing to look for it.

Every test below names the production change that should make it fail. Three
shapes recur, and all three are shapes this repository has shipped: a quantity
that is *correct code answering a different question* (the parent's groupId read
as the module's own, a test-scope dependency counted as main), a check that runs
where the thing it protects cannot occur (a vendored `node_modules` manifest
read as this estate's dependency), and a ranking whose order is really the hash
seed's.

Repository, artefact and namespace names here are invented. This repository is
public, and a real estate's coordinates are its own business.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settings_isolation import SettingsIsolated  # noqa: E402
from knowledgestore import cli, config  # noqa: E402
from knowledgestore import report_ingestion_gaps as gaps  # noqa: E402


# A pom in the format Maven actually writes: default namespace, a comment, a
# `<parent>` the module inherits its group from, `<dependencyManagement>`, a
# test scope, and a dependency whose group is a build property. Verbatim,
# because this format belongs to Maven and a simplified stand-in would let a
# reader of this file believe the parser had been exercised against the real
# thing.
CHILD_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <parent>
    <groupId>com.example.platform.orders</groupId>
    <artifactId>orders-parent</artifactId>
    <version>3.2.1</version>
  </parent>

  <!-- The artifactId below is the module's own; the groupId is inherited. -->
  <artifactId>orders-app</artifactId>
  <packaging>jar</packaging>

  <properties>
    <schema.version>1.4.0</schema.version>
  </properties>

  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.example.platform.framework</groupId>
        <artifactId>test-utils-core</artifactId>
        <version>7.0.0</version>
        <scope>test</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>

  <dependencies>
    <dependency>
      <groupId>com.example.platform.core.domain</groupId>
      <artifactId>shared-schema-model</artifactId>
      <version>${schema.version}</version>
    </dependency>
    <dependency>
      <groupId>${project.groupId}</groupId>
      <artifactId>orders-common</artifactId>
      <version>${project.version}</version>
    </dependency>
    <!-- Published by another repository in this same estate, through Gradle:
         consumed internally and built here, so never a candidate. -->
    <dependency>
      <groupId>com.example.platform.payments</groupId>
      <artifactId>payments-core</artifactId>
      <version>2.0.0</version>
    </dependency>
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>3.14.0</version>
    </dependency>
    <dependency>
      <groupId>com.example.platform.framework</groupId>
      <artifactId>test-utils-core</artifactId>
      <scope>test</scope>
      <exclusions>
        <exclusion>
          <groupId>com.example.platform.framework</groupId>
          <artifactId>excluded-transitive</artifactId>
        </exclusion>
      </exclusions>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""

PARENT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example.platform.orders</groupId>
  <artifactId>orders-parent</artifactId>
  <version>3.2.1</version>
  <packaging>pom</packaging>
  <modules>
    <module>orders-app</module>
  </modules>
</project>
"""

# A module consuming framework plumbing and nothing else, used to make the
# framework namespace outweigh every domain one.
FRAMEWORK_ONLY_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example.platform.orders</groupId>
  <artifactId>orders-{index}</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>com.example.platform.framework</groupId>
      <artifactId>test-utils-core</artifactId>
      <version>7.0.0</version>
    </dependency>
  </dependencies>
</project>
"""

# A Gradle build in the shape estates write, including the two forms whose
# scope is easy to attribute wrongly.
BUILD_GRADLE = """
plugins {
    id 'java-library'
    id 'maven-publish'
}

group = 'com.example.platform.payments'
version = '2.0.0'

repositories {
    mavenCentral()
    maven { url 'https://example.invalid/artefacts' }
}

dependencies {
    implementation 'com.example.platform.core.domain:shared-schema-model:1.4.0'
    // implementation 'com.example.platform.core.domain:commented-out:1.0.0'
    testImplementation platform("com.example.platform.framework:test-bom:9.1.0")
    implementation libs.jackson.databind
    implementation group: 'org.slf4j', name: 'slf4j-api', version: '2.0.9'
}
"""

SETTINGS_GRADLE = """
rootProject.name = 'payments-api'
include ':payments-core'
"""

MAIN_TF = """
module "network" {
  source = "github.com/example-org/tf-module-network?ref=v1.4.0"
}

module "payments" {
  source = "git@github.com:example-org/payments.api.git"
}

module "legacy_dns" {
  source = "git::https://github.com/example-org/tf-module-legacy-dns.git"
}
"""

DECLARATION = """
searched the example-org GitHub organisation
active tf-module-network
decommissioned tf-module-legacy-dns
alias payments.api payments-api
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _package(name: str, dependencies: dict, dev: dict | None = None) -> str:
    body: dict = {"name": name, "version": "1.0.0", "dependencies": dependencies}
    if dev is not None:
        body["devDependencies"] = dev
    return json.dumps(body, indent=2)


class GapsTestCase(SettingsIsolated):
    """A store root per test, so nothing reads the developer's own store."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config.configure(root=self.tmp, GITHUB_ORG="example-org")

    def clone(self, name: str) -> Path:
        clone = config.REPOSITORIES_DIR / name
        (clone / ".git").mkdir(parents=True, exist_ok=True)
        return clone

    def estate(self) -> None:
        """The fixture estate: Maven, Gradle, npm and Terraform in one store.

        Built by hand so every count below can be re-derived from these files
        rather than from the code that reads them.
        """
        orders = self.clone("orders-service")
        _write(orders / "pom.xml", PARENT_POM)
        _write(orders / "orders-app" / "pom.xml", CHILD_POM)
        # Build output: a generated copy of a pom, carrying a dependency that
        # exists nowhere in the source tree.
        _write(
            orders / "target" / "pom.xml",
            PARENT_POM.replace("orders-parent", "generated-copy").replace(
                "<modules>",
                "<dependencies><dependency><groupId>com.example.platform.core.domain"
                "</groupId><artifactId>generated-only</artifactId></dependency>"
                "</dependencies><modules>",
            ),
        )

        payments = self.clone("payments-api")
        _write(payments / "build.gradle", BUILD_GRADLE)
        _write(payments / "settings.gradle", SETTINGS_GRADLE)

        portal = self.clone("web-portal")
        _write(portal / "package.json", _package("@example/web-portal", {"react": "18.2.0"}))
        _write(
            portal / "packages" / "ui-kit" / "package.json",
            _package("@example/ui-kit", {"@example/shared-ui": "^2.0.0"}, dev={"jest": "29.0.0"}),
        )
        # Vendored: every dependency's own manifest lives here.
        _write(
            portal / "node_modules" / "vendored" / "package.json",
            _package("@example/vendored", {"@example/must-not-appear": "1.0.0"}),
        )

        infra = self.clone("infra-live")
        _write(infra / "main.tf", MAIN_TF)
        # Terraform's download cache holds copies of the upstream modules.
        _write(infra / ".terraform" / "modules" / "cached.tf", MAIN_TF.replace("network", "cached"))

    def declare(self, text: str = DECLARATION) -> None:
        _write(config.BOUNDARY_PATH, text)

    def evidence(self) -> gaps.Evidence:
        clones = sorted(d for d in config.REPOSITORIES_DIR.iterdir() if (d / ".git").is_dir())
        return gaps.read_estate(clones)

    def rendered(self, limit: int = 0) -> str:
        from knowledgestore import boundary

        return "\n".join(gaps.report(self.evidence(), boundary.read(), limit))


class PomReading(unittest.TestCase):
    def test_a_module_pom_builds_its_own_artefact_under_its_parents_group(self):
        """Break: read the first `<groupId>` in the pom text. That is the parent's in
        a module pom and a dependency's in a flat one, so the estate's built side
        gains a coordinate it does not build and loses the one it does - after which
        an artefact it publishes is ranked as something to ingest."""
        built, _, _ = gaps.pom_coordinates(CHILD_POM)
        self.assertEqual(built, gaps.Coordinate("com.example.platform.orders", "orders-app"))

    def test_a_flat_pom_uses_its_own_group(self):
        built, _, _ = gaps.pom_coordinates(PARENT_POM)
        self.assertEqual(built, gaps.Coordinate("com.example.platform.orders", "orders-parent"))

    def test_dependencies_are_read_with_their_scope_and_nothing_else_is(self):
        """Break: drop the scope, or read `<exclusion>` and `<plugin>` blocks as
        dependencies. A test-scope dependency counted as main promotes plumbing into
        the column that means "the estate's product needs this", and an excluded
        transitive is a dependency the build explicitly refuses."""
        _, consumed, _ = gaps.pom_coordinates(CHILD_POM)
        self.assertEqual(
            sorted((str(coordinate), scope) for coordinate, scope in consumed),
            [
                ("com.example.platform.core.domain:shared-schema-model", "main"),
                ("com.example.platform.framework:test-utils-core", "test"),
                ("com.example.platform.framework:test-utils-core", "test"),
                ("com.example.platform.payments:payments-core", "main"),
                ("org.apache.commons:commons-lang3", "main"),
            ],
        )

    def test_a_property_group_is_counted_not_taken_literally(self):
        """Break: accept `${project.groupId}` as a group. It would become its own
        namespace, appear as an artefact nothing builds, and rank as a candidate to
        ingest - a repository that cannot exist."""
        _, consumed, unresolved = gaps.pom_coordinates(CHILD_POM)
        self.assertEqual(unresolved, 1)
        self.assertNotIn("${", "".join(str(coordinate) for coordinate, _ in consumed))

    def test_a_commented_out_dependency_is_not_read(self):
        """Break: stop stripping XML comments. `"<dependency>" in text` stays true
        when the block is commented out - the same shape as reading a commented
        `export X=0` as set."""
        pom = PARENT_POM.replace(
            "<modules>",
            "<!-- <dependencies><dependency><groupId>com.example.platform.core.domain"
            "</groupId><artifactId>commented</artifactId></dependency></dependencies> -->"
            "<modules>",
        )
        _, consumed, _ = gaps.pom_coordinates(pom)
        self.assertEqual(consumed, [])


class GradleReading(unittest.TestCase):
    def test_the_test_configuration_is_read_from_the_line_not_the_nearest_token(self):
        """Break: take the scope from the token immediately before the coordinate.
        `testImplementation platform("g:a:1")` reads as `platform`, and a test BOM is
        then counted in the main column - the blended figure this stage exists to
        refuse."""
        found = dict(gaps.gradle_dependencies(BUILD_GRADLE))
        self.assertEqual(
            found[gaps.Coordinate("com.example.platform.framework", "test-bom")], "test"
        )
        self.assertEqual(
            found[gaps.Coordinate("com.example.platform.core.domain", "shared-schema-model")],
            "main",
        )

    def test_a_commented_line_and_a_repository_url_are_not_dependencies(self):
        """Break: match anywhere. A commented dependency is not declared, and
        `url 'https://...'` is not a coordinate - both would invent consumption."""
        coordinates = {str(coordinate) for coordinate, _ in gaps.gradle_dependencies(BUILD_GRADLE)}
        self.assertNotIn("com.example.platform.core.domain:commented-out", coordinates)
        self.assertFalse([c for c in coordinates if "example.invalid" in c], coordinates)

    def test_the_map_form_is_skipped_rather_than_guessed_at(self):
        """Break: half-read `group: 'x', name: 'y'` into a coordinate. It would
        arrive as a partial pair, and a partial coordinate cannot be subtracted."""
        coordinates = {str(coordinate) for coordinate, _ in gaps.gradle_dependencies(BUILD_GRADLE)}
        self.assertFalse([c for c in coordinates if "slf4j" in c], coordinates)


class GradleIdentity(GapsTestCase):
    def test_the_published_group_and_project_names_are_the_built_coordinates(self):
        """Break: fall back to the directory name for the artefact when
        `settings.gradle` names the projects. A subproject the estate publishes would
        be missing from the built side and ranked as a candidate to ingest."""
        clone = self.clone("payments-api")
        _write(clone / "build.gradle", BUILD_GRADLE)
        _write(clone / "settings.gradle", SETTINGS_GRADLE)
        self.assertEqual(
            sorted(str(coordinate) for coordinate in gaps.gradle_identity(clone)),
            [
                "com.example.platform.payments:payments-api",
                "com.example.platform.payments:payments-core",
            ],
        )

    def test_a_repository_declaring_no_group_publishes_nothing_knowable(self):
        """Break: invent a group from the directory name. The built side would gain a
        coordinate no build declares, and subtracting an invented coordinate hides a
        real candidate."""
        clone = self.clone("scripts-only")
        _write(clone / "build.gradle", "plugins { id 'base' }\n")
        self.assertEqual(gaps.gradle_identity(clone), set())


class NamespaceDerivation(unittest.TestCase):
    def test_an_unscoped_built_package_does_not_make_every_dependency_internal(self):
        """Break: allow an empty group into the namespace derivation. An estate that
        publishes one unscoped npm package would treat the empty namespace as its
        own, and every public dependency it has - the whole of npm - would be
        reported as something to ingest."""
        built = {gaps.Coordinate("", "portal"), gaps.Coordinate("", "admin")}
        self.assertEqual(gaps.internal_namespaces(built), ())
        self.assertFalse(gaps.is_internal("", gaps.internal_namespaces(built)))

    def test_one_built_artefact_does_not_establish_a_namespace(self):
        """Break: drop the threshold to one. A single vendored fork carrying an
        upstream group id would make that upstream namespace 'internal', and every
        third-party artefact under it a candidate."""
        self.assertEqual(
            gaps.internal_namespaces({gaps.Coordinate("io.upstream.tool", "forked")}), ()
        )
        self.assertEqual(
            gaps.internal_namespaces(
                {
                    gaps.Coordinate("com.example.platform.a", "one"),
                    gaps.Coordinate("com.example.platform.b", "two"),
                }
            ),
            ("com.example.platform",),
        )

    def test_a_neighbouring_namespace_is_not_read_as_inside_one(self):
        """Break: compare with a bare `startswith`. `com.example.platformer` is a
        different organisation's namespace that happens to share a prefix, and
        reporting its artefacts as this estate's dependencies is the confident
        nonsense this stage refuses to produce."""
        self.assertTrue(gaps.is_internal("com.example.platform.core", ("com.example.platform",)))
        self.assertFalse(gaps.is_internal("com.example.platformer", ("com.example.platform",)))


class Classification(unittest.TestCase):
    def test_a_marker_inside_a_longer_word_does_not_demote_a_domain_artefact(self):
        """Break: match markers as substrings. `attestation-service` contains `test`,
        so a domain artefact would be classified as plumbing and ranked below every
        framework row - which is exactly the promotion the split exists to prevent,
        running backwards."""
        self.assertEqual(
            gaps.classify("com.example.platform.legal", ("attestation-service",)), "domain"
        )
        self.assertEqual(
            gaps.classify("com.example.platform.legal", ("test-support",)), "framework"
        )

    def test_a_framework_namespace_is_framework_whatever_it_holds(self):
        self.assertEqual(
            gaps.classify("com.example.platform.framework", ("orders-model",)), "framework"
        )

    def test_a_tie_between_marked_and_unmarked_artefacts_counts_as_framework(self):
        """Break: flip the tie to domain. The costly error is plumbing promoted above
        a domain row, so the tie is resolved towards the demotion deliberately."""
        self.assertEqual(
            gaps.classify("com.example.platform.shared", ("mocks", "orders")), "framework"
        )


class RankingAndSubtraction(GapsTestCase):
    def test_the_estate_reads_every_build_format_it_holds(self):
        """The floor under every count below. A reader that silently matched nothing
        would let the subtraction tests pass over an empty estate - and a report
        derived from nothing reads exactly like an estate with no gaps."""
        self.estate()
        evidence = self.evidence()
        self.assertEqual(
            dict(evidence.scanned), {"pom.xml": 2, "build.gradle": 1, "package.json": 2, ".tf": 1}
        )
        self.assertEqual(
            evidence.held, {"orders-service", "payments-api", "web-portal", "infra-live"}
        )
        self.assertEqual(evidence.unresolved, 1)
        self.assertEqual(evidence.unscoped, {"react", "jest"})

    def test_generated_and_vendored_directories_are_not_read_as_this_estate(self):
        """Break: stop pruning `target/`, `node_modules/` and `.terraform/`. Each
        holds someone else's declarations - a generated pom, every dependency's own
        manifest, and copies of the upstream Terraform modules - so the report would
        rank another project's dependencies as this estate's gaps."""
        self.estate()
        rendered = self.rendered()
        for invented in ("generated-only", "must-not-appear", "cached"):
            self.assertNotIn(invented, rendered, f"{invented} came from a directory of copies")

    def test_a_coordinate_the_estate_builds_is_not_a_candidate(self):
        """Break: skip the subtraction. Every internal dependency would be reported
        as something to ingest, including the artefacts the estate publishes itself -
        which is a list nobody can act on and the whole point of this stage."""
        self.estate()
        evidence = self.evidence()
        rows, consumed = gaps.unbuilt(evidence, gaps.internal_namespaces(evidence.built))
        self.assertEqual(
            consumed,
            5,
            "shared-schema-model, test-utils-core, test-bom, shared-ui, payments-core",
        )
        self.assertIn(
            gaps.Coordinate("com.example.platform.payments", "payments-core"),
            evidence.built,
            "the fixture must consume something the estate builds, or the subtraction "
            "below has nothing to remove and this test cannot fail",
        )
        self.assertEqual(
            [row.group for row in rows],
            ["com.example.platform.core.domain", "@example", "com.example.platform.framework"],
        )

    def test_the_weights_are_declaring_files_split_by_scope(self):
        """Break: sum main and test into one weight, or count declarations rather
        than files. A namespace referenced only by test utilities would then outrank
        one the product depends on, and the reader could not see which question the
        number answered."""
        self.estate()
        evidence = self.evidence()
        rows, _ = gaps.unbuilt(evidence, gaps.internal_namespaces(evidence.built))
        by_group = {row.group: row for row in rows}
        domain = by_group["com.example.platform.core.domain"]
        self.assertEqual((domain.main, domain.test, domain.repos), (2, 0, 2))
        framework = by_group["com.example.platform.framework"]
        self.assertEqual((framework.main, framework.test, framework.repos), (0, 2, 2))
        self.assertEqual(framework.artefacts, ("test-bom", "test-utils-core"))

    def test_a_domain_namespace_ranks_above_a_heavier_framework_one(self):
        """Break: sort by weight alone. On the estate this was measured against two
        thirds of all reference weight was framework plumbing, so a weight-ordered
        ranking puts test utilities at the top and the repository worth adding
        somewhere below the fold."""
        self.estate()
        # Make the framework namespace outweigh everything by main-scope files.
        for index in range(9):
            _write(
                config.REPOSITORIES_DIR / "orders-service" / f"mod{index}" / "pom.xml",
                FRAMEWORK_ONLY_POM.format(index=index),
            )
        evidence = self.evidence()
        rows, _ = gaps.unbuilt(evidence, gaps.internal_namespaces(evidence.built))
        self.assertEqual(rows[0].kind, "domain")
        framework = next(row for row in rows if row.kind == "framework")
        self.assertGreater(framework.main, rows[0].main, "the fixture must make framework heavier")

    def test_namespaces_of_equal_weight_are_ordered_by_name(self):
        """Break: leave equal-weight rows in dict order. Two runs of the same store
        would then differ, because the order is really the hash seed's - and a stage
        whose output is committed evidence must be byte-identical across runs."""
        evidence = gaps.Evidence()
        # Inserted worst-name-first, so a ranking that kept insertion order would
        # produce the opposite answer and this test would notice.
        for group in ("com.example.platform.zebra", "com.example.platform.alpha"):
            evidence.declared[(gaps.Coordinate(group, "thing"), "main")] = {("a-repo", "pom.xml")}
        rows, _ = gaps.unbuilt(evidence, ("com.example.platform",))
        self.assertEqual(
            [row.group for row in rows],
            ["com.example.platform.alpha", "com.example.platform.zebra"],
        )


class BoundaryIntegration(GapsTestCase):
    def test_a_module_consumed_under_an_off_host_alias_of_a_held_repository_is_no_gap(self):
        """Break: stop resolving aliases here. A module source written against the
        off-host name of a repository the store *does* hold would be reported as
        something to ingest - a false absence invented inside the report whose whole
        subject is false absence."""
        self.estate()
        self.declare()
        from knowledgestore import boundary

        found = dict(gaps.module_gaps(self.evidence(), boundary.read()))
        self.assertNotIn("payments-api", found)
        self.assertNotIn("payments.api", found)

    def test_a_ruling_says_whether_an_absence_is_a_decision(self):
        """Break: report every unheld module identically. A repository the estate has
        ruled `decommissioned` is a decision, one ruled `active` and not held is the
        exact shape of the published finding that was drawn honestly and was false,
        and telling an operator to weigh them the same wastes the ranking."""
        self.estate()
        self.declare()
        from knowledgestore import boundary

        found = dict(gaps.module_gaps(self.evidence(), boundary.read()))
        self.assertIn("declared active and not held", found["tf-module-network"])
        self.assertIn("not a gap", found["tf-module-legacy-dns"])

    def test_an_undeclared_boundary_is_stated_rather_than_assumed_away(self):
        """Break: print the ranking with no membership statement. Absence of evidence
        in a store is a fact about the store's membership, and a report that ranks
        what is missing without saying what was never searched invites the reader to
        treat its silence as the estate's."""
        self.estate()
        rendered = self.rendered()
        self.assertIn("No boundary is declared", rendered)
        self.assertIn("may be built somewhere nobody has read", rendered)

    def test_sources_declared_unsearched_are_named_in_the_report(self):
        """Break: ignore `unsearched`. An artefact built on a host nobody read is
        unbuilt here by construction, so ranking it as a candidate without saying so
        sends an operator hunting a repository the estate already knows about."""
        self.estate()
        self.declare(DECLARATION + "unsearched an internal forge no build machine reaches\n")
        rendered = self.rendered()
        self.assertIn("1 source nobody read (an internal forge no build machine reaches)", rendered)
        self.assertIn("unbuilt here by construction", rendered)


class TheReport(GapsTestCase):
    def test_the_scope_of_what_was_read_is_stated(self):
        """Break: print the ranking without the funnel. A number whose scope the
        reader cannot see is worse than no number - they cannot tell a small list
        from a parser that read almost nothing."""
        self.estate()
        rendered = self.rendered()
        self.assertIn("Read 1 .tf, 1 build.gradle, 2 package.json, 2 pom.xml", rendered)
        self.assertIn("across 4 repositories", rendered)
        self.assertIn("Consumed internally: 5 coordinates, 3 namespaces", rendered)

    def test_the_built_side_says_how_much_of_it_is_convention(self):
        """Break: report built coordinates as one total. A missing built coordinate
        manufactures a candidate, and Gradle's publication name is a convention
        rather than a declaration - so a reader who cannot see the Gradle share
        cannot tell a real candidate from a weakly-subtracted one."""
        self.estate()
        rendered = self.rendered()
        self.assertIn("2 from gradle convention", rendered)
        self.assertIn("2 from pom.xml", rendered)
        self.assertIn("2 from package.json", rendered)
        self.assertIn("not a declaration", rendered)

    def test_unresolved_and_unscoped_declarations_are_disclosed(self):
        """Break: drop them silently. Both are declarations this stage cannot
        classify, and a report that omits what it could not read implies it read
        everything."""
        self.estate()
        rendered = self.rendered()
        self.assertIn("Not counted: 1 declaration naming a build property", rendered)
        self.assertIn("Not classified: 2 npm dependencies carrying no scope", rendered)

    def test_the_report_refuses_to_resolve_a_coordinate_to_a_repository(self):
        """Break: remove the footer, or add forge resolution. Name matching against a
        large organisation returns confident nonsense from unrelated programmes, and
        an operator who does not know a coordinate is unresolved will read the
        namespace as a repository name."""
        self.estate()
        rendered = self.rendered()
        self.assertIn("never resolved to a repository", rendered)
        self.assertIn("<scm> URL", rendered)

    def test_nothing_read_is_said_plainly_rather_than_reported_as_no_dependencies(self):
        """Break: print an empty ranking. A store whose clones hold no build file at
        all, or whose reader has stopped matching, produces the same empty list as an
        estate with no gaps - and the second reads as a clean bill of health."""
        self.clone("docs-only")
        _write(config.REPOSITORIES_DIR / "docs-only" / "README.md", "# nothing to build\n")
        rendered = self.rendered()
        self.assertIn("No build file was read at all", rendered)
        self.assertIn("not the same as it consuming nothing", rendered)

    def test_an_estate_with_no_derivable_namespace_ranks_nothing_and_says_why(self):
        """Break: rank against an empty namespace list and report `0 namespaces` as
        though the estate consumed nothing internal. The honest statement is that
        nothing could be called internal, which is a fact about the instrument."""
        clone = self.clone("lone-service")
        _write(clone / "pom.xml", PARENT_POM)
        rendered = self.rendered()
        self.assertIn("No internal namespace could be derived", rendered)

    def test_the_limit_says_how_much_it_hid(self):
        """Break: truncate silently. A list cut at 20 rows with no note reads as the
        whole answer, and on a real estate the tail is where the unexpected
        repository was."""
        self.estate()
        self.assertIn("... and 2 further namespaces", self.rendered(limit=1))
        self.assertNotIn("further namespaces", self.rendered(limit=0))


class TheStageIsWired(GapsTestCase):
    def test_the_cli_runs_the_stage_and_reports_a_finding_without_failing(self):
        """Break: leave `gaps` out of the CLI, or return non-zero on a finding. An
        unwired stage is this repository's most repeated escape - the helper is
        tested, the report is right, and nothing a user can type reaches it. And a
        non-zero exit turns "you depend on something you do not hold" into a build
        failure, when it is a decision for an operator."""
        self.estate()
        self.declare()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = cli.main(["--root", str(self.tmp), "gaps"])
        printed = captured.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn("shared-schema-model", printed)
        self.assertIn("tf-module-network", printed)

    def test_an_unsynced_store_is_a_setup_error(self):
        """Break: report an empty estate as no gaps. Nothing is synced, so the report
        would describe an estate that was never read - the difference between "no
        candidates" and "no evidence"."""
        code = gaps.main([])
        self.assertEqual(code, 1)

    def test_the_stage_reaches_no_network(self):
        """Break: resolve coordinates through the forge. Code search returns nothing
        for an artefact published to a binary repository, rate-limits while doing it,
        and name matching against a large organisation returns confident nonsense.
        Asserted against the module source, because the temptation is a later
        change."""
        source = Path(gaps.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "socket", "http.client", "subprocess"):
            self.assertNotIn(f"import {forbidden}", source)
        self.assertIn("never resolved to a repository", source)

    def test_two_processes_with_different_hash_seeds_render_the_same_report(self):
        """Break: iterate a set or a dict keyed by coordinate without sorting. Run in
        subprocesses on purpose: PYTHONHASHSEED is fixed at interpreter start, so a
        same-process comparison cannot see the defect at all."""
        self.estate()
        self.declare()
        renders = {seed: _render_in_subprocess(self.tmp, seed) for seed in ("0", "1")}
        self.assertIn("shared-schema-model", renders["0"], "the render must not be empty")
        self.assertEqual(renders["0"], renders["1"])
        self.assertLess(
            renders["0"].index("com.example.platform.core.domain"),
            renders["0"].index("com.example.platform.framework"),
            "domain before framework, not insertion order",
        )


_RENDER = (
    "import sys;"
    "sys.path.insert(0, {src!r});"
    "from knowledgestore import config;"
    "config.configure(root=sys.argv[1]);"
    "from knowledgestore import report_ingestion_gaps as gaps;"
    "raise SystemExit(gaps.main(['--limit', '0']))"
)


def _render_in_subprocess(root: Path, seed: str) -> str:
    src = str(Path(__file__).resolve().parent.parent / "src")
    completed = subprocess.run(
        [sys.executable, "-c", _RENDER.format(src=src), str(root)],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
