"""Secret values masked out of text a worker reads, with their keys kept.

Every expected output is a literal written by hand. Every value is invented, and
the shapes a secret scanner looks for are assembled from parts at runtime so
this file never holds one whole.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter

from settings_isolation import SettingsIsolated

from knowledgestore import config, secret_mask

M = "[masked]"

# Assembled, never written whole: a private-key header, and token shapes.
PEM_KIND = "RSA PRIVATE" + " KEY"
PEM_HEAD = "-----BEGIN " + PEM_KIND + "-----"
PEM_TAIL = "-----END " + PEM_KIND + "-----"
AWS_ID = "AK" + "IA" + "FAKE" * 4
GITHUB = "gh" + "p_" + "x" * 36
SLACK_PATH = "T000" + "/B000/" + "x" * 8
SK_KEY = "sk" + "_live_" + "fake0123456789"
SDK_KEY = "sdk" + "-00000000-0000-0000-0000-000000000000"
SONAR = "sq" + "p_" + "0" * 40
JWT = "ey" + "Jxxxx.ey" + "Jyyyy.zzzz"


def masked(text: str) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    return secret_mask.mask(text, counts), counts


class KeyNameRules(SettingsIsolated):
    """An assignment under a secret-named key: the value goes, the key stays.

    Break: the key-name rule dropped, or a format it should read not matched.
    """

    CASES = [
        ("password: fake-pw\n", "password: [masked]\n"),
        ("DB_PASSWORD=fake-pw\n", "DB_PASSWORD=[masked]\n"),
        ('"clientSecret": "fake-cs",\n', '"clientSecret": "[masked]",\n'),
        ('api_key = "fake-ak"\n', 'api_key = "[masked]"\n'),
        ("db.password=fake-pw\n", "db.password=[masked]\n"),
        ("export AUTH_TOKEN='fake-tk'\n", "export AUTH_TOKEN='[masked]'\n"),
        ("storage_account_key: fake-sk\n", "storage_account_key: [masked]\n"),
        ("connectionString: fake-cs\n", "connectionString: [masked]\n"),
        ("credentials = fake-cr\n", "credentials = [masked]\n"),
        ("SAS=fake-sas\n", "SAS=[masked]\n"),
        ("basic_auth: fake-au\n", "basic_auth: [masked]\n"),
        ("privateKey: fake-pk\n", "privateKey: [masked]\n"),
        ("AccessKey: fake-ak\n", "AccessKey: [masked]\n"),
        ("pwd=fake-pw\n", "pwd=[masked]\n"),
        ("passwd: fake-pw\n", "passwd: [masked]\n"),
        ("password: fake-pw # rotated\n", "password: [masked] # rotated\n"),
        # A key starting its line takes the value to the end of the line, so a
        # separator inside an `.env` value does not leave its second half.
        ("PASSWORD=ab;cd,ef\n", "PASSWORD=[masked]\n"),
        ("    this.password = fake-pw;\n", "    this.password = [masked];\n"),
        # An annotated default: the type is kept and the literal goes.
        ('    password: str = "fake-pw"\n', '    password: str = "[masked]"\n'),
        ("/login?token=fake-tk&page=2", "/login?token=[masked]&page=2"),
        (
            '{"user": "app", "password": "fake-pw"}',
            '{"user": "app", "password": "[masked]"}',
        ),
    ]

    def test_each_format_masks_the_value_and_keeps_the_key(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))


class ReferencesAreNotSecrets(SettingsIsolated):
    """A value that points at a secret, or is not one, is left as it is.

    Break: a reference masked as a secret - the worker loses which variable or
    store a service reads, which is architecture, not a credential.
    """

    UNCHANGED = [
        "password: ${DB_PASSWORD}\n",
        'password: "{{ .Values.db.password }}"\n',
        "PASSWORD=$(cat /run/db)\n",
        "PASSWORD=$DB_PASSWORD\n",
        "passwordSecretRef: app-db\n",
        "  secretKeyRef:\n    name: app-secrets\n    key: password\n",
        'password: "@Microsoft.KeyVault(SecretUri=https://kv.example/secrets/db)"\n',
        "password: vault:secret/data/app#db\n",
        'password: ""\n',
        "password:\n",
        "token_ttl: 300\n",
        "auth: true\n",
        "password: null\n",
        "author: Jane\n",
        "the password policy requires twelve characters\n",
        'password = os.environ["DB_PASSWORD"]\n',
        "http://localhost:8080/path\n",
        "Basic usage is described below\n",
        "pip install sk-learn-tutorial-examples\n",
        # Code: a type annotation, a parameter, an argument, a dict entry
        # mid-line - none is a value, and masking them mangles the source.
        "    password: str\n",
        "    token: Optional[str] = None\n",
        "def fused_ids(token: str, plan_ids: set[str]) -> list[str]:\n",
        "with self.subTest(token=token):\n",
        '{"user": u, "password": pw}\n',
        "# a hard-coded secret: one, two\n",
        "password: ${A}-${B}\n",
        '    "private-key": (\n',
        '    "api_token": settings.API_TOKEN,\n',
        "mkdir -p build && docker run -p 8080:80 app\n",
        "tool login --password-file /run/pw\n",
        'tool login --password "$PW"\n',
    ]

    def test_references_and_non_secrets_are_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, text)
                self.assertEqual(counts, Counter())


class ValueShapeRules(SettingsIsolated):
    """A value whose own shape says it is a secret, wherever it appears.

    Break: any one shape rule dropped from the defaults, or its group mis-set so
    the context it should keep goes with the value.
    """

    CASES = [
        (
            f"{PEM_HEAD}\nMIIBfake\nAAAA\n{PEM_TAIL}\n",
            f"{PEM_HEAD}{M}{PEM_TAIL}\n",
            "pem-private-key",
        ),
        (
            "https://acct.blob.example/c/b?sv=2022-11-02&sig=fakeSig%3D&se=2030",
            "https://acct.blob.example/c/b?sv=2022-11-02&sig=[masked]&se=2030",
            "sas-signature",
        ),
        (
            "Protocol=https;AccountName=acct;AccountKey=fakeKey==;Suffix=core.example",
            "Protocol=https;AccountName=acct;AccountKey=[masked];Suffix=core.example",
            "connection-string-key",
        ),
        (
            "Endpoint=sb://bus.example/;SharedAccessKey=fakeKey=",
            "Endpoint=sb://bus.example/;SharedAccessKey=[masked]",
            "connection-string-key",
        ),
        (
            'header("Authorization: Bearer fake.tok.en")',
            'header("Authorization: Bearer [masked]")',
            "authorization-header",
        ),
        (
            "Authorization: Basic ZmFrZQ==\n",
            "Authorization: Basic [masked]\n",
            "authorization-header",
        ),
        (
            "postgres://app:fake-pw@db.example:5432/app",
            "postgres://app:[masked]@db.example:5432/app",
            "url-credential",
        ),
        (f"header {JWT} end", "header [masked] end", "jwt"),
        (f"id {AWS_ID} end", "id [masked] end", "aws-access-key-id"),
        (f"use {GITHUB} here", "use [masked] here", "github-token"),
        (
            f"https://hooks.slack.com/services/{SLACK_PATH}",
            "https://hooks.slack.com/services/[masked]",
            "slack-webhook",
        ),
        (f"key {SK_KEY} end", "key [masked] end", "sk-key"),
        (f"flags {SDK_KEY} end", "flags [masked] end", "feature-flag-sdk-key"),
        (f"scan {SONAR} end", "scan [masked] end", "sonarqube-token"),
        (
            '"db": "ENC[AES256_GCM,data:ZmFrZQ==,type:str]"',
            '"db": "[masked]"',
            "sops-encrypted",
        ),
        (
            "CREATE USER app IDENTIFIED BY 'fake-pw';",
            "CREATE USER app IDENTIFIED BY '[masked]';",
            "sql-credential",
        ),
        (
            "ALTER ROLE app WITH PASSWORD 'fake-pw';",
            "ALTER ROLE app WITH PASSWORD '[masked]';",
            "sql-credential",
        ),
        ("mysql -u app -pfakepw db", "mysql -u app -p[masked] db", "cli-secret-flag"),
        ("tool login --password fake-pw", "tool login --password [masked]", "cli-secret-flag"),
        ("tool login --token=fake-tk", "tool login --token=[masked]", "cli-secret-flag"),
    ]

    def test_each_shape_is_masked_under_its_own_rule_name(self):
        for text, want, rule in self.CASES:
            with self.subTest(rule=rule, text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({rule: 1}))

    def test_a_shape_under_a_secret_key_is_counted_once_by_the_shape(self):
        # Break: the key-name rule re-masking a value a shape already masked,
        # which double-counts and, for `ENC[...]`, leaves stray brackets behind.
        got, counts = masked("password: ENC[AES256_GCM,data:ZmFrZQ==,type:str]\n")
        self.assertEqual(got, "password: [masked]\n")
        self.assertEqual(counts, Counter({"sops-encrypted": 1}))


class EstateRules(SettingsIsolated):
    """`KSB_SECRET_PATTERNS` adds shapes the library cannot know.

    Break: the setting not read at call time, or the group rule inverted.
    """

    def test_a_custom_rule_masks_its_first_group_or_its_whole_match(self):
        config.SECRET_PATTERNS = {
            **config.DEFAULT_SECRET_PATTERNS,
            "estate-pin": r"PIN-(\d{4})",
            "estate-code": r"XQ-\w+",
        }
        got, counts = masked("door PIN-1234 and XQ-abc9 here")
        self.assertEqual(got, "door PIN-[masked] and [masked] here")
        self.assertEqual(counts, Counter({"estate-pin": 1, "estate-code": 1}))

    def test_a_rule_that_cannot_compile_fails_rather_than_masking_nothing(self):
        config.SECRET_PATTERNS = {"unclosed-group": "([A-Z"}
        with self.assertRaises(re.error):
            secret_mask.mask("anything")


class Determinism(SettingsIsolated):
    TEXT = f"password: fake-pw\npostgres://app:fake-db@db.example/app\n{AWS_ID}\ntoken_ttl: 300\n"

    def test_the_same_input_twice_is_byte_identical(self):
        # Break: rule order taken from an unordered collection.
        first = secret_mask.mask(self.TEXT)
        second = secret_mask.mask(self.TEXT)
        self.assertEqual(first, second)

    def test_masking_masked_text_finds_nothing_more(self):
        # Break: `[masked]` itself read as a value - a re-run would count again.
        once, first = masked(self.TEXT)
        twice, second = masked(once)
        self.assertEqual(
            once,
            "password: [masked]\npostgres://app:[masked]@db.example/app\n[masked]\n"
            "token_ttl: 300\n",
        )
        self.assertEqual(sum(first.values()), 3)
        self.assertEqual((twice, second), (once, Counter()))

    def test_no_counts_argument_is_accepted(self):
        self.assertEqual(secret_mask.mask("pwd=fake-pw"), "pwd=[masked]")


if __name__ == "__main__":
    unittest.main()
