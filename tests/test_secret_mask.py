"""Secret values masked out of text a worker reads, with their keys kept.

Every expected output is a literal written by hand. Every value is invented, and
the shapes a secret scanner looks for are assembled from parts at runtime so
this file never holds one whole.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

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
        # `pass` and `passphrase` as whole words, the dotenv and keystore spellings.
        ("DB_PASS=fake-pw\n", "DB_PASS=[masked]\n"),
        ("keystore.passphrase: fake-pw\n", "keystore.passphrase: [masked]\n"),
        ("password: fake-pw # rotated\n", "password: [masked] # rotated\n"),
        # A key starting its line takes the value to the end of the line, so a
        # separator inside an `.env` value does not leave its second half.
        ("PASSWORD=ab;cd,ef\n", "PASSWORD=[masked]\n"),
        ("    this.password = fake-pw;\n", "    this.password = [masked];\n"),
        # An annotated default: the type is kept and the literal goes.
        ('    password: str = "fake-pw"\n', '    password: str = "[masked]"\n'),
        ("/login?token=fake-tk&page=2", "/login?token=[masked]&page=2"),
        # A shell line continuation is not part of the value.
        ("  --set db.password=fake-pw \\\n", "  --set db.password=[masked] \\\n"),
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
        # The chart convention naming the Secret object that holds the value.
        "  existingSecret: redis-auth\n",
        "  secretKeyRef:\n    name: app-secrets\n    key: password\n",
        'password: "@Microsoft.KeyVault(SecretUri=https://kv.example/secrets/db)"\n',
        "password: vault:secret/data/app#db\n",
        'password: ""\n',
        "password:\n",
        "token_ttl: 300\n",
        "auth: true\n",
        "password: null\n",
        "author: Jane\n",
        # `pass` inside a word is not the word.
        "compass: north\n",
        "bypassCache: always\n",
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
        # Code assigning a name to a member or passing it on: names, not values.
        "        this.password = password;\n",
        "        self.token = token\n",
        "  password: password,\n",
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
            '"Main": "Server=db;User Id=app;Password=fake-pw;Pooling=true"',
            '"Main": "Server=db;User Id=app;Password=[masked];Pooling=true"',
            "connection-string-credential",
        ),
        (
            "'Host=db;Pwd=fake-pw'",
            "'Host=db;Pwd=[masked]'",
            "connection-string-credential",
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
            "redis://:fake-pw@cache.example:6379/0",
            "redis://:[masked]@cache.example:6379/0",
            "url-credential",
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


# An invented value, split so no line here reads as an assignment of it.
PW = "fake" + "Pw123"


def unchanged_and_uncounted(case: unittest.TestCase, text: str) -> None:
    got, counts = masked(text)
    case.assertEqual(got, text)
    case.assertEqual(counts, Counter())


class KubernetesEnvLists(SettingsIsolated):
    """A list item whose `name:` names a secret holds the secret in `value:`.

    Break: a Kubernetes or Helm `env:` entry - the commonest deployment shape -
    reaching the worker whole, because the secret-named key sits on the sibling
    line and the value under a key, `value`, that names nothing.
    """

    CASES = [
        (
            f"env:\n  - name: DB_PASSWORD\n    value: {PW}\n",
            "env:\n  - name: DB_PASSWORD\n    value: [masked]\n",
        ),
        (
            f'env:\n- name: "API_TOKEN"\n  value: "{PW}"\n',
            'env:\n- name: "API_TOKEN"\n  value: "[masked]"\n',
        ),
        (
            f"env:\n    -   name: CLIENT_SECRET\n        value: '{PW}' # rotated\n",
            "env:\n    -   name: CLIENT_SECRET\n        value: '[masked]' # rotated\n",
        ),
        # Another key between the two, and the value first, are the same item.
        (
            f"  - name: db.password\n    description: the db\n    value: {PW}\n",
            "  - name: db.password\n    description: the db\n    value: [masked]\n",
        ),
        (
            f"  - value: {PW}\n    name: SAS\n",
            "  - value: [masked]\n    name: SAS\n",
        ),
        (
            f"set:\n  - key: AUTH\n    value: {PW}\n  - key: LOG_LEVEL\n    value: info\n",
            "set:\n  - key: AUTH\n    value: [masked]\n  - key: LOG_LEVEL\n    value: info\n",
        ),
    ]

    UNCHANGED = [
        "env:\n  - name: LOG_LEVEL\n    value: info\n",
        "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n"
        "        name: app-secrets\n        key: password\n",
        "env:\n  - name: DB_PASSWORD\n    value: ${DB_PASSWORD}\n",
        'env:\n  - name: DB_PASSWORD\n    value: "{{ .Values.db.password }}"\n',
        # The next item's value is not this item's.
        "env:\n  - name: DB_PASSWORD\n    valueFrom: {}\n  - name: PORT\n    value: web\n",
        # A value nested under another key of the item is not the item's value.
        "  - name: API_TOKEN\n    spec:\n      value: web\n",
    ]

    def test_the_value_of_an_item_named_for_a_secret_is_masked(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_an_item_named_for_no_secret_or_holding_a_reference_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class XmlConfig(SettingsIsolated):
    """.NET and Java configuration: attributes, `key`/`value` pairs and elements.

    Break: a `web.config` appSetting, a Spring bean property or a Maven
    `<password>` element reaching the worker whole, because XML has no `:` or
    `=`-terminated value the assignment rules read.
    """

    CASES = [
        (
            f'<add key="DbPassword" value="{PW}" />',
            '<add key="DbPassword" value="[masked]" />',
        ),
        (
            f"<property name='db.password' value='{PW}'/>",
            "<property name='db.password' value='[masked]'/>",
        ),
        (f"<password>{PW}</password>", "<password>[masked]</password>"),
        (
            f"  <server>\n    <apiToken>\n      {PW}\n    </apiToken>\n",
            "  <server>\n    <apiToken>\n      [masked]\n    </apiToken>\n",
        ),
        (f'<db password="{PW}" user="app"/>', '<db password="[masked]" user="app"/>'),
        (
            f'<add name="Main" connectionString="Server=db;Key={PW}" providerName="Sql" />',
            '<add name="Main" connectionString="[masked]" providerName="Sql" />',
        ),
    ]

    UNCHANGED = [
        '<add key="LogLevel" value="info" />',
        '<add key="DbPassword" value="" />',
        '<add key="DbPassword" value="#{DbPassword}#" />',
        "<password>${DB_PASSWORD}</password>",
        '<input type="password" name="pw" />',
        "<label>Password</label>\n",
        "List<String> tokens = new ArrayList<>();\n",
    ]

    def test_an_xml_secret_is_masked_and_its_markup_kept(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(sum(counts.values()), 1)

    def test_xml_holding_no_secret_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class YamlBlockScalars(SettingsIsolated):
    """`password: |` holds its value on the lines below it.

    Break: a multi-line secret - a key, a certificate bundle, a password
    written as a literal block - reaching the worker because its value is not on
    the key's line.
    """

    CASES = [
        (
            f"password: |\n  {PW}\nuser: app\n",
            "password: |\n  [masked]\nuser: app\n",
        ),
        (
            f"db:\n  token: >-\n    {PW}\n    {PW}\n\n    more\n  user: app\n",
            "db:\n  token: >-\n    [masked]\n    [masked]\n\n    [masked]\n  user: app\n",
        ),
        (
            f"- clientSecret: |+ # from the vault export\n    {PW}\n- user: app\n",
            "- clientSecret: |+ # from the vault export\n    [masked]\n- user: app\n",
        ),
    ]

    UNCHANGED = [
        "description: |\n  the password is rotated monthly\nuser: app\n",
        "password: |\n  ${DB_PASSWORD}\n",
        "password: |\nuser: app\n",
    ]

    def test_the_lines_of_a_secret_block_scalar_are_masked_and_the_indicator_kept(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_block_under_a_key_naming_no_secret_or_holding_a_reference_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class FlowMappings(SettingsIsolated):
    """A one-line YAML flow mapping is configuration, not a code literal.

    Break: `{password: x, user: y}` reaching the worker because a value a `,`
    ends reads as an argument in code.
    """

    CASES = [
        (f"{{password: {PW}, user: app}}\n", "{password: [masked], user: app}\n"),
        (f"db: {{user: app, token: {PW}}}\n", "db: {user: app, token: [masked]}\n"),
        (
            f"  - {{name: DB_PASSWORD, value: {PW}}}\n",
            "  - {name: DB_PASSWORD, value: [masked]}\n",
        ),
        (
            f'[{{"name": "DB_PASSWORD",\n  "value": "{PW}"}}]\n',
            '[{"name": "DB_PASSWORD",\n  "value": "[masked]"}]\n',
        ),
    ]

    UNCHANGED = [
        "{password: ${DB_PASSWORD}, user: app}\n",
        "{user: app, level: info}\n",
        "  - {name: LOG_LEVEL, value: info}\n",
        # Code: a call's argument, and an assignment, are not flow mappings.
        "connect({password: pw, user: u})\n",
        "const options = {password: pw, user: u};\n",
        '{"name": "DB_PASSWORD", "value": process.env.DB_PASSWORD}\n',
    ]

    def test_an_unquoted_flow_value_under_a_secret_key_is_masked(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_flow_mapping_holding_no_secret_and_code_are_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class KubernetesSecretManifests(SettingsIsolated):
    """Every value in a Secret's `data:` and `stringData:` is secret, named or not.

    Break: a Secret manifest committed with `DB_URL` or `host` keys - names no
    rule recognises - reaching the worker whole, when the manifest's own kind
    says each of them is a secret.
    """

    def test_every_value_under_a_secret_manifests_data_is_masked(self):
        text = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n"
            "  DB_URL: cG9zdGdyZXM=\n  note: |\n    line one\n"
            f"stringData:\n  host: db.example # the primary\n  password: {PW}\n"
            "type: Opaque\n"
        )
        got, counts = masked(text)
        self.assertEqual(
            got,
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n"
            "  DB_URL: [masked]\n  note: |\n    [masked]\n"
            "stringData:\n  host: [masked] # the primary\n  password: [masked]\n"
            "type: Opaque\n",
        )
        # `password` is the key-name rule's; the three it cannot name are this one's.
        self.assertEqual(counts, Counter({"key-name": 4}))

    def test_only_the_secret_document_of_a_stream_is_masked(self):
        got, _ = masked("kind: Secret\ndata:\n  a: eA==\n---\nkind: ConfigMap\ndata:\n  b: info\n")
        self.assertEqual(
            got, "kind: Secret\ndata:\n  a: [masked]\n---\nkind: ConfigMap\ndata:\n  b: info\n"
        )

    def test_a_config_map_and_a_templated_secret_are_unchanged(self):
        for text in (
            "kind: ConfigMap\ndata:\n  host: db.example\n",
            'kind: Secret\nstringData:\n  url: "{{ .Values.url }}"\n',
            "kind: Secret\nmetadata:\n  name: app\n",
        ):
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class HclVariableDefaults(SettingsIsolated):
    """A Terraform variable named for a secret holds it in `default`.

    Break: `variable "db_password" { default = "..." }` reaching the worker,
    because the secret's name is the block's label and not a key.
    """

    def test_the_default_of_a_variable_named_for_a_secret_is_masked(self):
        got, counts = masked(
            f'variable "db_password" {{\n  type      = string\n  default   = "{PW}"\n'
            "  sensitive = true\n}\n"
        )
        self.assertEqual(
            got,
            'variable "db_password" {\n  type      = string\n  default   = "[masked]"\n'
            "  sensitive = true\n}\n",
        )
        self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_variable_named_for_no_secret_or_with_an_empty_default_is_unchanged(self):
        for text in (
            'variable "location" {\n  default = "uksouth"\n}\n',
            'variable "db_password" {\n  default = ""\n}\n',
            'variable "db_password" {\n  type = string\n}\n',
        ):
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class LineEndingsPreserved(SettingsIsolated):
    """Masking changes the secret and nothing else, whatever the line ending.

    Break: a CRLF file losing its `\r` with the value - or worse, a quoted value
    whose `\r` stopped it matching, masked with its quotes and comma - so the
    worker reads a file that differs from the original beyond the secret.
    """

    CASES = [
        (f"password: {PW}\r\nuser: app\r\n", "password: [masked]\r\nuser: app\r\n"),
        (f"DB_PASSWORD={PW}\r\n", "DB_PASSWORD=[masked]\r\n"),
        (f'  "password": "{PW}",\r\n', '  "password": "[masked]",\r\n'),
        (f"password: {PW} # old\r\n", "password: [masked] # old\r\n"),
        (f"/x?token={PW}\r\n", "/x?token=[masked]\r\n"),
        (
            f"env:\r\n  - name: DB_PASSWORD\r\n    value: {PW}\r\n",
            "env:\r\n  - name: DB_PASSWORD\r\n    value: [masked]\r\n",
        ),
        (f"token: |\r\n  {PW}\r\nuser: app\r\n", "token: |\r\n  [masked]\r\nuser: app\r\n"),
        (f"{{password: {PW}, user: app}}\r\n", "{password: [masked], user: app}\r\n"),
        (f"<password>{PW}</password>\r\n", "<password>[masked]</password>\r\n"),
    ]

    def test_crlf_survives_masking_byte_for_byte(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_crlf_file_holding_no_secret_is_unchanged(self):
        unchanged_and_uncounted(self, "user: app\r\npassword: ${DB_PASSWORD}\r\n\r\n")


SKILL = Path(__file__).resolve().parent.parent / "skills" / "knowledge-store-build" / "SKILL.md"


def limit_bullets(text: str, heading: str) -> list[str]:
    """The bullets of the list that follows `heading`, each on one line."""
    bullets: list[str] = []
    for line in text.split(heading, 1)[1].splitlines()[1:]:
        if line.startswith("- "):
            bullets.append(line[2:])
        elif bullets and line.startswith("  "):
            bullets[-1] += " " + line.strip()
        elif bullets:
            break
    return [" ".join(bullet.split()) for bullet in bullets]


class DocumentedLimits(SettingsIsolated):
    """What masking still lets through is written down, and the list is true.

    Break: a limit fixed while the docs still promise it, or a new limit that
    neither the module nor the build skill tells an operator about. Fixing one
    fails the first test; removing it here then fails the second until the
    module docstring and the skill drop it too.
    """

    LIMITS = [
        ("a literal compared or passed in code", 'if pw == "example-secret":\n'),
        (
            "a value under a key that names no secret word, or in prose",
            "signing-key: example-secret\nthe admin password is example-secret\n",
        ),
        (
            "a value whose name is in another column",
            "INSERT INTO clients (id, client_secret) VALUES (1, 'example-secret');\n",
        ),
        ("the lines a value continues onto", "password=first-half \\\n    example-secret\n"),
        ("a value in an XML CDATA section", "<password><![CDATA[example-secret]]></password>\n"),
    ]

    def test_each_documented_limit_still_reaches_the_worker(self):
        for limit, text in self.LIMITS:
            with self.subTest(limit=limit):
                self.assertIn(
                    "example-secret",
                    secret_mask.mask(text),
                    "masked now: drop this limit from secret_mask's docstring and the build skill",
                )

    def test_the_module_and_the_build_skill_list_exactly_these_limits(self):
        documented = limit_bullets(secret_mask.__doc__ or "", "**What this does not mask.**")
        in_skill = limit_bullets(SKILL.read_text(encoding="utf-8"), "**The limit.**")
        self.assertEqual(in_skill, documented)
        self.assertEqual([b.split(":", 1)[0] for b in documented], [lim for lim, _ in self.LIMITS])


if __name__ == "__main__":
    unittest.main()
