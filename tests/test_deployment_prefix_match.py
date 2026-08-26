"""One repository shipping several deployed components keeps its joins (#88).

`match_services` asked two questions of a name, and both miss the commonest
shape there is: a repository named `alpha` holding the code that ships as
`alpha-agent`, `alpha-backend` and `alpha-frontend`. `alphaagent` is no segment
of `alpha`, and the substring fallback runs the other way round - the stem
inside the repository name - so all three services went unjoined and their
deployment configuration reached no code in the graph.

The rule added here is directional and matches whole segments: the repository
name must equal a leading *run* of the service's hyphen segments. So `alpha`
claims `alpha-agent` and does not claim `alphabet`, which a character prefix
would - and that claim would be a fabricated deployment relationship, valid on
the page and wrong, with nothing in the report to show it. A missing join is a
gap a reader can see; where the two trade off this stage takes the gap.

It applies last, after the segment and substring rules, so every join those two
already made is made unchanged and this change only ever adds.

Pinned here: the joins the rule exists for, the word boundary it will not cross,
the floor on the claimant's name, longest-run-wins, the tiebreak that keeps two
runs identical, that precedence, and the whole thing end to end on a built graph.
"""

from __future__ import annotations

import contextlib
import io as io_module
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Importing this first puts the working tree ahead of any installed copy.
from settings_isolation import SettingsIsolated
from knowledgestore import build_deployments as deployments
from knowledgestore import config

try:  # PyYAML is the `deploy` extra, not a runtime dependency of this library
    import yaml  # noqa: F401

    HAS_YAML = True
except ImportError:  # pragma: no cover - the default-install CI job takes this path
    HAS_YAML = False

needs_yaml = unittest.skipUnless(HAS_YAML, "needs the `deploy` extra (PyYAML)")


class PrefixClaims(unittest.TestCase):
    """The new rule, one property per test."""

    def test_one_repository_claims_the_components_it_ships(self):
        """Break: drop the prefix rule. This is the shape it exists for - one
        repository named `alpha` and three deployed components named after it.
        Neither older rule can see them: the segment rule wants `alphaagent` to be
        a whole segment of `alpha`, and the substring rule wants the stem to sit
        inside the repository name, which a longer stem never does."""
        matched = deployments.match_services(
            {"alpha-agent", "alpha-backend", "alpha-frontend"}, {"alpha", "beta"}
        )
        self.assertEqual(
            matched,
            {
                "alpha-agent": "alpha",
                "alpha-backend": "alpha",
                "alpha-frontend": "alpha",
            },
        )

    def test_a_namesake_does_not_claim_a_service_it_merely_starts(self):
        """Break: match a character prefix instead of whole segments, which is what
        this rule did when it first landed. `alpha` starts `alphabet`, so
        `alphabet-agent` was claimed by `alpha` whenever the repository named
        `alphabet` was absent from the graph - a fabricated deployment
        relationship, mechanically valid and entirely wrong, and counted as a join
        so it raised the reported match rate. A leading run is whole segments, and
        `alphabet` is not `alpha` plus anything."""
        matched = deployments.match_services({"alphabet-agent"}, {"alpha"})
        self.assertEqual(matched, {})

    def test_a_repository_does_not_claim_a_service_its_own_name_extends(self):
        """Break: reverse the test, to `_norm(r).startswith(stem)`. Reversed, a
        repository named `alphaagent` claims a service named `alp` - and every
        other short name that happens to begin it - which is the unfloored
        coincidence the segment rule exists to refuse. Neither older rule can see
        this pair either: `alp` is no segment of `alphaagent` and is below the
        substring floor, so the assertion is the new rule's own direction."""
        self.assertEqual(deployments.match_services({"alp-service"}, {"alphaagent"}), {})

    def test_the_service_is_segmented_before_it_is_normalised(self):
        """Break: normalise the service name before splitting it, as
        `_norm(name).split("-")`. `_norm` strips every separator, so after it there
        is no boundary left to segment on and "whole segments" quietly means
        nothing - the split returns one piece, every leading run disappears, and
        the repair someone reaches for is a character prefix, which is the
        fabrication this rule replaced. Both halves are asserted in one call
        because the pair is what a boundary means: `alpha` claims the service whose
        first segment is `alpha`, and never the service that merely starts with
        those five letters."""
        matched = deployments.match_services({"alpha-agent", "alphabet-agent"}, {"alpha"})
        self.assertEqual(matched, {"alpha-agent": "alpha"})

    def test_a_separator_inside_the_repository_name_is_not_a_difference(self):
        """Break: segment the repository name as well, rather than normalising it
        whole. `alpha-core` and `alphacore` are one name here, decided rather than
        inherited: `_norm` exists for exactly this, and the segment rule above
        already treats `svc-unifiedsearch-query` and `unifiedsearchquery` as the
        same name. What is load-bearing is the boundary in the *service* name,
        because that is where a namesake could fabricate a join; punctuation inside
        the repository's own name says nothing about identity. Asserted so the
        answer is visible instead of an accident of where normalisation happens."""
        matched = deployments.match_services({"alphacore-agent"}, {"alpha-core"})
        self.assertEqual(matched, {"alphacore-agent": "alpha-core"})

    def test_the_longest_repository_name_claims_the_service(self):
        """Break: sort the claimants shortest-first, which is the order the
        segment and substring rules use two lines above. Both `alpha` and
        `alpha-agent` are leading runs of `alpha-agent-worker`, and the longer of
        the two is the more
        specific claim - handing the worker's production configuration to the
        other real repository is exactly the wrong join this matcher is built to
        avoid, and it would be counted as a success."""
        matched = deployments.match_services({"alpha-agent-worker"}, {"alpha", "alpha-agent"})
        self.assertEqual(matched, {"alpha-agent-worker": "alpha-agent"})

    def test_a_repository_name_below_the_floor_claims_nothing(self):
        """Break: drop the floor. `ops` is a whole leading segment of both
        `ops-alpha-agent` and `ops-beta-frontend`, so without the floor a
        repository with that name collects every service in an estate that
        prefixes a convention with those three characters - and a wrong join is
        counted as a join, so it raises the reported match rate instead of showing
        as a gap. Whole segments removed the coincidence the constant was
        originally measured against; what is left is that a claimant this short
        leaves the segments naming the component entirely unexamined."""
        matched = deployments.match_services({"ops-alpha-agent", "ops-beta-frontend"}, {"ops"})
        self.assertEqual(matched, {})

    def test_the_segment_and_substring_rules_keep_precedence(self):
        """Break: consult the prefix rule before the segment or substring rule.
        The new rule is additive only because it runs last, and both older rules
        have a case here that the prefix rule would answer differently:
        `delta-agent` matches the segment `deltaagent` in `svc-deltaagent` and has
        `delta` as a leading run, and `beta-core` matches `svc-beta-core-api` as a
        substring and has `beta` as a leading run. Whichever rule is asked first
        owns the answer, so this is the assertion that the change adds joins
        rather than moving them."""
        self.assertEqual(
            deployments.match_services({"delta-agent"}, {"svc-deltaagent", "delta"}),
            {"delta-agent": "svc-deltaagent"},
        )
        self.assertEqual(
            deployments.match_services({"beta-core"}, {"svc-beta-core-api", "beta"}),
            {"beta-core": "svc-beta-core-api"},
        )

    def test_two_processes_with_different_hash_seeds_name_the_same_claimant(self):
        """Break: sort the claimants on length alone. Claimants can only tie on
        length by normalising to the same string, so the tie is real:
        `alpha-core`, `alpha.core`, `alpha_core` and `alphacore` all normalise to
        `alphacore`, which is one of the leading
        runs of `alphacore-agent`. `repos` is a set, so with no tiebreak the
        winner is whatever the hash seed happened to put first, and two runs of
        the same build write the deployment edge to different nodes. Run in
        subprocesses on purpose: PYTHONHASHSEED is fixed at interpreter start, so
        a same-process comparison cannot see the defect at all."""
        repos = ("alpha-core", "alpha.core", "alpha_core", "alphacore")
        claimants = {
            seed: _claimant_in_subprocess(seed, "alphacore-agent", repos)
            for seed in ("0", "1", "2", "3", "4", "5")
        }
        # Hand-derived: all four normalise to `alphacore`, so the tie breaks on
        # the raw name, and `-` (45) sorts before `.` (46), `_` (95) and `c` (99).
        self.assertEqual(set(claimants.values()), {"alpha-core"}, claimants)


_CLAIMANT = (
    "import sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "from knowledgestore import build_deployments as deployments;"
    "print(deployments.match_services({sys.argv[2]}, set(sys.argv[3:]))"
    ".get(sys.argv[2], '<none>'))"
)


def _claimant_in_subprocess(seed: str, service: str, repos: tuple[str, ...]) -> str:
    src = str(Path(__file__).resolve().parent.parent / "src")
    completed = subprocess.run(
        [sys.executable, "-c", _CLAIMANT, src, service, *repos],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    return completed.stdout.strip()


DEFAULT_SERVICES = ("alpha-agent", "alpha-backend", "alpha-frontend", "omega-agent")


def _deploy_repo(tmp: Path, services: tuple[str, ...] = DEFAULT_SERVICES) -> Path:
    """One environment holding the named components, by default four."""
    repo = tmp / "repositories" / "estate-deploy"
    environment = repo / "ansible" / "group_vars" / "prd"
    (repo / ".git").mkdir(parents=True)
    environment.mkdir(parents=True)
    for service in services:
        (environment / f"{service}_values.yaml.j2").write_text(
            f"replicas: 2\nimage:\n  name: {service}\n", encoding="utf-8"
        )
    return repo


def _graph(tmp: Path) -> None:
    """A graph holding code in `alpha` and nothing that could hold `omega-agent`."""
    (tmp / "graphify-out").mkdir(parents=True, exist_ok=True)
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "alpha::AgentMain",
                "label": "AgentMain",
                "repo": "alpha",
                "source_file": "src/main/java/AgentMain.java",
            }
        ],
        "links": [],
    }
    (tmp / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


@needs_yaml
class DeploymentEdges(SettingsIsolated, unittest.TestCase):
    def test_each_component_reaches_its_repository_and_the_stranger_is_reported(self):
        """Break: drop the prefix rule, or stop reporting what it did not join.
        End to end on a built graph, because the join only matters as the edge it
        produces: all three components of `alpha` must carry an edge to the code
        in `alpha`, which before this change none of them did, and `omega-agent`
        must still be named in the report - an unmatched service that goes
        unreported reads as a complete join."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            said = io_module.StringIO()
            with contextlib.redirect_stdout(said):
                self.assertEqual(deployments.main(), 0)
            graph = json.loads((tmp / "graphify-out" / "graph.json").read_text())
        deploying = {e["source"] for e in graph["links"] if e["relation"] == "deploys"}
        self.assertEqual(
            deploying,
            {
                "estate-deploy::deploy:prd:alpha-agent",
                "estate-deploy::deploy:prd:alpha-backend",
                "estate-deploy::deploy:prd:alpha-frontend",
            },
        )
        targets = {e["target"] for e in graph["links"] if e["relation"] == "deploys"}
        self.assertEqual(targets, {"alpha::AgentMain"})
        report = said.getvalue()
        self.assertIn("joined to a repository in the graph: 3 of 4", report)
        self.assertIn("omega-agent", report)


class Reachability(unittest.TestCase):
    """A service that matched nothing is two different facts, not one.

    Either a repository is in the graph and the join missed it, which is matcher
    work someone can do, or the estate holds no repository for that service at
    all, which no name rule can reach. Reporting both as "matched no repository"
    sends an operator to tune a matcher against services it cannot win, and it
    reads as though the matcher were missing half its joins when the denominator
    holds what it cannot reach.
    """

    def test_a_service_no_repository_shares_a_word_with_is_out_of_reach(self):
        """Break: report every unmatched service the same way. `omega-agent` has
        no repository in the graph sharing any word with it, so no name rule -
        segment, substring or leading run - can ever join it. Naming that as a
        match failure is the wrong instruction: what closes it is a repository,
        not a matcher."""
        out_of_reach = deployments.unreachable_services({"omega-agent"}, {"alpha", "beta-core"})
        self.assertEqual(out_of_reach, {"omega-agent"})

    def test_a_service_a_repository_shares_a_word_with_is_a_matcher_gap(self):
        """Break: call everything unmatched out of reach, which would bury the
        gaps worth chasing. `agent-alpha` matches no rule - `agentalpha` is no
        segment of `alpha`, is not inside it, and the leading run is `agent` - but
        the repository `alpha` shares a whole word with it, so a reader has
        somewhere to look and this is not a scope fact."""
        self.assertEqual(deployments.match_services({"agent-alpha"}, {"alpha"}), {})
        self.assertEqual(deployments.unreachable_services({"agent-alpha"}, {"alpha"}), set())

    def test_a_word_too_short_to_identify_is_not_a_candidate(self):
        """Break: count any shared word. `api` is shared vocabulary rather than a
        name, so `api-omega` and a repository named `api-alpha` having it in
        common says nothing - and counting it hides the scope fact, which is the
        direction that costs an operator time. Same floor as the matching rules,
        so the library holds one answer to "too short to be evidence"."""
        self.assertEqual(
            deployments.unreachable_services({"api-omega"}, {"api-alpha"}), {"api-omega"}
        )


@needs_yaml
class ReachabilityReport(SettingsIsolated, unittest.TestCase):
    """The distinction has to reach the page the operator reads."""

    def _run(self, services: tuple[str, ...]) -> str:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _graph(tmp)
            _deploy_repo(tmp, services)
            config.configure(root=tmp, DEPLOY_REPOS={"estate-deploy"})
            said = io_module.StringIO()
            with contextlib.redirect_stdout(said):
                self.assertEqual(deployments.main(), 0)
        return said.getvalue()

    def test_the_run_names_the_services_no_repository_could_match(self):
        """Break: print the unmatched count and stop, which is what it did.
        `omega-agent` has no repository in the graph and `agent-alpha` has one
        that shares a word, so the run must separate them: both unmatched, one out
        of reach. Reported as one number, an operator reads a matcher that missed
        two joins."""
        report = self._run(("alpha-agent", "agent-alpha", "omega-agent"))
        self.assertIn("2 service(s) matched no repository", report)
        self.assertIn("1 of those share no name with any repository", report)
        out_of_reach = report[report.index("share no name with any repository") :]
        self.assertIn("omega-agent", out_of_reach)
        self.assertNotIn("agent-alpha", out_of_reach)

    def test_a_fully_joined_run_says_nothing_about_reach(self):
        """Sensitivity control on the test above: with every service joined there
        is no unmatched line and no reach line, so a run cannot report a scope
        fact it does not have. A check that prints its line unconditionally would
        pass the test above and be worthless."""
        report = self._run(("alpha-agent", "alpha-backend"))
        self.assertIn("joined to a repository in the graph: 2 of 2", report)
        self.assertNotIn("matched no repository", report)
        self.assertNotIn("share no name", report)


if __name__ == "__main__":
    unittest.main()
