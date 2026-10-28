"""The contract with the library: `minimum_standard` over the layer
knowledge-store-builder v0.19.1 wrote, read with the query index and without it.

Every expected record is derived by hand, before the store is read, from what
`library_store/generate.py` writes and from v0.19.1's `build_minimum_standard.py`
(which item each point holds, and each reason's text) and
`build_minimum_standard_settings.py` (what the snapshot records):

- The snapshot is `github_responses.json`'s answers, read at 2026-10-20 (the
  clock `pipeline.py` passes to the stage's `now=` seam). alpha-lib, beta-app
  and eta-portal are not archived, on `main`, with secret scanning and
  Dependabot alerts on; alpha-lib's and eta-portal's `main` is protected and
  beta-app's is not. Every call for delta-deploy is 404, so each of its five
  settings is unread (`null`). `example-org/.github` is private, so the
  organisation supplies no default security policy.
- `owner`: the first CODEOWNERS found; beta-app's `* @example-org/payments-team`
  names a root owner. `security_contact`: the first SECURITY.md found; beta-app's
  names a mailbox. A repository with none is judged by its organisation's
  default, and a private `.github` gives none, so it is `absent`.
- `no_secrets`: a scan of the content-set files, and every tracked key file by
  name. alpha-lib's and beta-app's files hold nothing secret-shaped, so the scan
  is clean and the point `not_determinable`. delta-deploy's `password:` setting
  is a `key-name` finding (line 1 of `config/application.yml`); its empty
  `deploy/signing.pem` is a `key-file` finding with no line, a value shape, so
  the point is `absent`. v0.19.1 (#511) reads a `.pem` for its `-----BEGIN`
  labels; an empty one has none, and a `.pem` or `.key` with no boundary stays a
  key file (`is_key_file`), so the finding is unchanged.
- `hygiene` holds six items in this order: `branch_protection`,
  `code_scanning_workflow`, `dependency_updates`, `secret_scanning_setting`,
  `secret_scanning_workflow`, `vulnerability_alerts`. No clone has a
  dependency-update config but beta-app (`.github/dependabot.yml`), so the point
  is `absent` everywhere; beta-app's CodeQL workflow is its code-scanning
  evidence. An unread setting is `not_determinable`, "no access".
- `secure_by_design` (no threat model) and `patching` are `not_determinable`
  everywhere; `unmaintained` is `not_determinable` with the last commit cited:
  "not archived" for alpha-lib, beta-app and eta-portal, "no access" for
  delta-deploy.
- eta-portal holds only a `package.json`, with the same snapshot answers as
  alpha-lib, so its record is alpha-lib's with its own name and last commit.
- `as_of` is each clone's `main` HEAD, its date as git's `%cI` writes it: the
  generator's git, recorded as `# git:` in `library_store/requirements.txt`,
  writes UTC as `Z` (2.55). The shas are `inputs.json`'s.
"""

import unittest
from typing import Any

from knowledgestore.server import tools_minimum_standard
from knowledgestore.server.tools_graph import ArgumentRefused
from library_store import harness
from library_store.harness import SHAS

READ_AT = "2026-10-20T00:00:00+00:00"
NOT_ARCHIVED = "not archived; maintenance and a patching route are not judged from the repository"
NO_ACCESS = "no access"
CLEAN_SCAN = "pattern scan found nothing; a scan cannot show absence"
FOUND_SECRETS = (
    "pattern scan of the current tree found these; values are not recorded "
    "and history is not scanned"
)
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SECURITY_PATHS = (".github/SECURITY.md", "SECURITY.md", "docs/SECURITY.md")
WORKFLOWS = [".github/workflows/"]
UPDATE_CONFIGS = [
    ".github/dependabot.yaml",
    ".github/dependabot.yml",
    ".github/renovate.json",
    ".renovaterc*",
    "renovate.json",
]


def point(state: str, evidence: list[dict[str, Any]], reason: str | None = None) -> dict:
    return {"state": state, "evidence": evidence, "reason": reason}


NO_OWNER = point(
    "absent",
    [{"path": path, "reason": "not found"} for path in CODEOWNERS_PATHS],
    "no CODEOWNERS file names an owner",
)
# No SECURITY.md, and `example-org/.github` private: no default applies.
NO_CONTACT = point(
    "absent",
    [
        *({"path": path, "reason": "not found"} for path in SECURITY_PATHS),
        {
            "path": "example-org/.github",
            "reason": "not public, so GitHub applies no default from it",
        },
    ],
    "no SECURITY.md",
)
NO_THREAT_MODEL = point(
    "not_determinable", [], "no threat model found; secure-by-design is not judged from code"
)
PATCHING = point("not_determinable", [], "patching timelines are not recorded in a repository")


def setting(item: str, field: str, state: str, reason: str) -> dict:
    return {"item": item, "setting": field, "state": state, "reason": reason}


def file_item(item: str, checked: list[str], paths: list[str], state: str, reason: str) -> dict:
    return {"item": item, "checked": checked, "paths": paths, "state": state, "reason": reason}


NO_CODE_SCANNING = file_item(
    "code_scanning_workflow",
    WORKFLOWS,
    [],
    "not_determinable",
    "no code-scanning workflow; default setup is not visible",
)
NO_UPDATES = file_item(
    "dependency_updates", UPDATE_CONFIGS, [], "absent", "no dependency update config"
)
NO_SECRET_WORKFLOW = file_item(
    "secret_scanning_workflow", WORKFLOWS, [], "absent", "no secret-scanning workflow"
)
SECRET_SCANNING_ON = setting(
    "secret_scanning_setting", "secret_scanning", "present", "secret scanning is enabled"
)
ALERTS_ON = setting(
    "vulnerability_alerts", "dependabot_alerts", "present", "Dependabot alerts are enabled"
)


def last_commit(repo: str, date: str) -> dict:
    return {"commit": SHAS[repo], "date": date, "reason": "last commit"}


def live(repo: str, date: str) -> dict:
    """`unmaintained` for a repository the snapshot reads as not archived."""
    return point(
        "not_determinable",
        [last_commit(repo, date), {"reason": "not archived", "setting": "archived"}],
        NOT_ARCHIVED,
    )


ALPHA = {
    "repository": "alpha-lib",
    "as_of": {"commit": SHAS["alpha-lib"], "date": "2026-10-04T09:00:00Z"},
    "settings_read_at": READ_AT,
    "points": {
        "owner": NO_OWNER,
        "security_contact": NO_CONTACT,
        "no_secrets": point("not_determinable", [], CLEAN_SCAN),
        "secure_by_design": NO_THREAT_MODEL,
        "hygiene": point(
            "absent",
            [
                setting(
                    "branch_protection",
                    "branch_protection",
                    "present",
                    "the default branch is protected",
                ),
                NO_CODE_SCANNING,
                NO_UPDATES,
                SECRET_SCANNING_ON,
                NO_SECRET_WORKFLOW,
                ALERTS_ON,
            ],
        ),
        "patching": PATCHING,
        "unmaintained": live("alpha-lib", "2026-10-04T09:00:00Z"),
    },
}
# eta-portal reuses ALPHA's points: their snapshot answers and files are
# identical, so only the last commit, in `unmaintained`, differs.
ETA = {
    "repository": "eta-portal",
    "as_of": {"commit": SHAS["eta-portal"], "date": "2026-10-05T12:00:00Z"},
    "settings_read_at": READ_AT,
    "points": {
        **ALPHA["points"],
        "unmaintained": live("eta-portal", "2026-10-05T12:00:00Z"),
    },
}
BETA = {
    "repository": "beta-app",
    "as_of": {"commit": SHAS["beta-app"], "date": "2026-10-06T09:00:00Z"},
    "settings_read_at": READ_AT,
    "points": {
        "owner": point(
            "present",
            [{"path": ".github/CODEOWNERS", "reason": "names an owner for the repository root"}],
        ),
        "security_contact": point(
            "present",
            [
                {
                    "path": "SECURITY.md",
                    "reason": "carries a contact; whether it is monitored is not recorded",
                }
            ],
        ),
        "no_secrets": point("not_determinable", [], CLEAN_SCAN),
        "secure_by_design": NO_THREAT_MODEL,
        "hygiene": point(
            "absent",
            [
                setting(
                    "branch_protection",
                    "branch_protection",
                    "absent",
                    "the default branch is not protected",
                ),
                file_item(
                    "code_scanning_workflow",
                    WORKFLOWS,
                    [".github/workflows/codeql.yml"],
                    "present",
                    "code-scanning workflow found",
                ),
                file_item(
                    "dependency_updates",
                    UPDATE_CONFIGS,
                    [".github/dependabot.yml"],
                    "present",
                    "dependency update config found",
                ),
                SECRET_SCANNING_ON,
                NO_SECRET_WORKFLOW,
                ALERTS_ON,
            ],
        ),
        "patching": PATCHING,
        "unmaintained": live("beta-app", "2026-10-06T09:00:00Z"),
    },
}
DELTA = {
    "repository": "delta-deploy",
    "as_of": {"commit": SHAS["delta-deploy"], "date": "2026-10-08T09:00:00Z"},
    # The snapshot holds an entry for it, every field unread.
    "settings_read_at": READ_AT,
    "points": {
        "owner": NO_OWNER,
        "security_contact": NO_CONTACT,
        "no_secrets": point(
            "absent",
            [
                {"path": "config/application.yml", "line": 1, "rule": "key-name"},
                {"path": "deploy/signing.pem", "rule": "key-file"},
            ],
            FOUND_SECRETS,
        ),
        "secure_by_design": NO_THREAT_MODEL,
        "hygiene": point(
            "absent",
            [
                setting("branch_protection", "branch_protection", "not_determinable", NO_ACCESS),
                NO_CODE_SCANNING,
                NO_UPDATES,
                setting(
                    "secret_scanning_setting", "secret_scanning", "not_determinable", NO_ACCESS
                ),
                NO_SECRET_WORKFLOW,
                setting("vulnerability_alerts", "dependabot_alerts", "not_determinable", NO_ACCESS),
            ],
        ),
        "patching": PATCHING,
        "unmaintained": point(
            "not_determinable",
            [last_commit("delta-deploy", "2026-10-08T09:00:00Z")],
            NO_ACCESS,
        ),
    },
}

NOTES = ["archived repositories are not in the store"]


def known(name: str) -> dict:
    return {"answer": "in the estate", "exact": name, "matched_case_insensitively": False}


def hygiene_item(record: dict, item: str) -> dict:
    return next(e for e in record["points"]["hygiene"]["evidence"] if e["item"] == item)


class MinimumStandardContract(harness.Contract):
    def test_the_whole_layer_is_each_repository_s_record(self):
        """Catches: a v0.19.1 record misread - a renamed hygiene item, a setting
        citation, a key-file finding with no line, the organisation-default
        citation or the last-commit citation dropped by the evidence filter - or a
        repository missing from the whole-layer answer, or the summary's notes
        not served."""
        found = tools_minimum_standard.minimum_standard(self.session)
        self.assertEqual(found["items"], [ALPHA, BETA, DELTA, ETA])
        self.assertEqual(found["total"], 4)
        self.assertEqual(found["scope"], "estate")
        self.assertEqual(found["notes"], NOTES)
        self.assertNotIn("no_record", found)
        self.assertNotIn("records_not_read", found)

    def test_a_product_is_the_repositories_the_summary_declares_for_it(self):
        """Catches: the summary's `products` map not read as the library writes it
        (product -> sorted names), so `payments` is answered as not declared, or
        given every repository."""
        found = tools_minimum_standard.minimum_standard(self.session, product="payments")
        self.assertEqual(
            found["product"],
            {
                "answer": "a declared product",
                "exact": "payments",
                "repositories": ["alpha-lib", "beta-app"],
            },
        )
        self.assertEqual(found["items"], [ALPHA, BETA])

    def test_an_unprotected_branch_is_cited_under_hygiene_and_keeps_it_from_present(self):
        """Catches: the v0.19.1 item `branch_protection` not read under `hygiene`,
        or beta-app's unprotected `main` served as anything but `absent`, so the
        point reads as met; and the CodeQL workflow beta-app holds not cited as
        its `code_scanning_workflow`."""
        found = tools_minimum_standard.minimum_standard(self.session, repos=["beta-app"])
        (record,) = found["items"]
        hygiene = record["points"]["hygiene"]
        self.assertEqual(hygiene["state"], "absent")
        self.assertEqual(
            hygiene_item(record, "branch_protection"),
            setting(
                "branch_protection",
                "branch_protection",
                "absent",
                "the default branch is not protected",
            ),
        )
        scanning = hygiene_item(record, "code_scanning_workflow")
        self.assertEqual(scanning["state"], "present")
        self.assertEqual(scanning["paths"], [".github/workflows/codeql.yml"])

    def test_a_secret_finding_is_where_and_which_rule_never_the_value(self):
        """Catches: the key-file finding, which has no line, dropped as malformed or
        given a line, so delta-deploy's point is no longer `absent`; the key-name
        finding lost; or a library release that starts recording the setting's
        value, `fixture-not-a-secret`, so it reaches the answer. (v0.19.1 writes
        no value for the server to filter; that filter is pinned against the
        synthetic fixture in `test_tools_minimum_standard`.)"""
        found = tools_minimum_standard.minimum_standard(self.session, repos=["delta-deploy"])
        (record,) = found["items"]
        self.assertEqual(record["points"]["no_secrets"], DELTA["points"]["no_secrets"])
        self.assertNotIn("fixture-not-a-secret", repr(found))

    def test_an_unreadable_setting_is_not_determinable_and_names_the_setting(self):
        """Catches: a setting the token could not read (every one, for
        delta-deploy) served as `absent` - a finding against the team - or its
        citation losing the setting it names; or `unmaintained` judged without the
        archived flag."""
        found = tools_minimum_standard.minimum_standard(self.session, repos=["delta-deploy"])
        (record,) = found["items"]
        for item, field in (
            ("branch_protection", "branch_protection"),
            ("secret_scanning_setting", "secret_scanning"),
            ("vulnerability_alerts", "dependabot_alerts"),
        ):
            with self.subTest(item=item):
                self.assertEqual(
                    hygiene_item(record, item), setting(item, field, "not_determinable", NO_ACCESS)
                )
        self.assertEqual(record["points"]["unmaintained"], DELTA["points"]["unmaintained"])

    def test_a_live_repository_is_not_judged_unmaintained_and_cites_its_last_commit(self):
        """Catches: v0.19.1's `unmaintained` - `not_determinable` for a repository
        that is not archived - served as present or absent, or its last-commit
        citation (commit and date) dropped."""
        found = tools_minimum_standard.minimum_standard(self.session, repos=["alpha-lib"])
        self.assertEqual(found["items"], [ALPHA])
        unmaintained = found["items"][0]["points"]["unmaintained"]
        self.assertEqual(unmaintained["state"], "not_determinable")
        self.assertEqual(
            unmaintained["evidence"][0], last_commit("alpha-lib", "2026-10-04T09:00:00Z")
        )

    def test_repos_scopes_to_the_names_given_as_a_list_or_as_text(self):
        """Catches: `repos` ignored, so the whole layer comes back; a list read as
        one comma-joined name; or the comma-separated text v0.2.1 callers send no
        longer accepted."""
        found = tools_minimum_standard.minimum_standard(self.session, repos=["alpha-lib"])
        self.assertEqual(found["items"], [ALPHA])
        self.assertEqual(found["scope"], "repos")
        self.assertEqual(found["repositories"], [known("alpha-lib")])

        legacy = tools_minimum_standard.minimum_standard(self.session, repos="alpha-lib,beta-app")
        self.assertEqual(legacy["items"], [ALPHA, BETA])
        self.assertEqual(legacy["repositories"], [known("alpha-lib"), known("beta-app")])

    def test_a_pattern_is_a_glob_over_the_names_the_layer_holds(self):
        """Catches: `pattern` ignored, so the whole layer comes back, or read as a
        regular expression rather than a glob, so `*-lib` misses alpha-lib."""
        found = tools_minimum_standard.minimum_standard(self.session, pattern="*-lib")
        self.assertEqual(found["items"], [ALPHA])
        self.assertEqual(found["scope"], "pattern")
        self.assertEqual(found["total"], 1)

    def test_the_layer_pages_one_repository_a_page_by_its_cursor(self):
        """Catches: `cursor` not followed over the layer's records, so a page
        repeats alpha-lib or drops delta-deploy or eta-portal, or a later page
        loses the summary's notes."""
        found = harness.pages(
            lambda **paging: tools_minimum_standard.minimum_standard(self.session, **paging)
        )
        self.assertEqual([page["items"] for page in found], [[ALPHA], [BETA], [DELTA], [ETA]])
        self.assertEqual([page["notes"] for page in found], [NOTES] * 4)

    def test_two_scopes_at_once_are_refused(self):
        """Catches: `repos` and `product` both given and one silently dropped, so
        the answer is scoped by a name the caller did not mean."""
        with self.assertRaises(ArgumentRefused):
            tools_minimum_standard.minimum_standard(
                self.session, repos=["alpha-lib"], product="payments"
            )


MinimumStandardWithIndex, MinimumStandardWithoutIndex = harness.both_modes(MinimumStandardContract)


if __name__ == "__main__":
    unittest.main()
