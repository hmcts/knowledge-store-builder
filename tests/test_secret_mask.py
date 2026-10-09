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
            f"{PEM_HEAD}\n{M}\n{M}\n{PEM_TAIL}\n",
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
        # The connection string's own `Password=` is the secret, not the whole string.
        (
            f'<add name="Main" connectionString="Server=db;Password={PW}" providerName="Sql" />',
            '<add name="Main" connectionString="Server=db;Password=[masked]" providerName="Sql" />',
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


# Credential shapes, assembled from parts: a hex secret, two UUIDs, a base64 key,
# a bare webhook token and a short base64 PIN key.
HEX = "0f1e2d3c" + "4b5a6978" * 15
UUID = "3f2a9c1e" + "-4b7d-8a6f-2e5d-7c9b1a0f3e4d"
UUID2 = "9d8c7b6a" + "-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
B64 = "Qk1yZ9xW" + "4vU8tS2rP6oN1mL5kJ3hG7fD0cB+" * 3 + "Ab1Cd2Ef3Gh4Ij=="
HOOK = "T0FAKE0" + "/B0FAKE0/" + "Fake0Hook9Path0x"
PIN = "Rk9vYmFy" + "MTIzNDU2"


class CredentialLookingKeys(SettingsIsolated):
    """`key` and `webhook`, and a secret word inside a key, hold a value that
    looks like a credential.

    Break: a signing key, an instrumentation key or a webhook token reaching the
    worker because its key ends in `key` or `webhook` - words the rule did not
    count - or names its secret word before another word, as `secret_key_base`.
    """

    CASES = [
        (f"secret_key_base: {HEX}\n", "secret_key_base: [masked]\n"),
        (f"app:\n  secret_key_base: {HEX}\n", "app:\n  secret_key_base: [masked]\n"),
        (f"APP_INSIGHTS_KEY: {UUID}\n", "APP_INSIGHTS_KEY: [masked]\n"),
        (f"monitoring:\n  APPINSIGHTS_KEY: {UUID}\n", "monitoring:\n  APPINSIGHTS_KEY: [masked]\n"),
        (f"JWT_KEY: {B64}\n", "JWT_KEY: [masked]\n"),
        (f"jwtKey={B64}\n", "jwtKey=[masked]\n"),
        (
            f"AUTH_PROVIDER_SERVICE_SERVER_JWT_KEY: {B64}\n",
            "AUTH_PROVIDER_SERVICE_SERVER_JWT_KEY: [masked]\n",
        ),
        (f"slack-webhook: {HOOK}\n", "slack-webhook: [masked]\n"),
        (f"NOTIFY_API_KEY_LIVE: livekey-{UUID}-{UUID2}\n", "NOTIFY_API_KEY_LIVE: [masked]\n"),
        (f"sso.pin.key={PIN}\n", "sso.pin.key=[masked]\n"),
        # A secret word fused onto the end of a segment counts as the word.
        (f"MASTERKEY={HEX}\n", "MASTERKEY=[masked]\n"),
    ]

    UNCHANGED = [
        # A key's value that is a name, a file, a path or too short to be a key.
        "  secretKeyRef:\n    name: app\n    key: password\n",
        "key: app.yaml\n",
        "keyFile: /etc/x.pem\n",
        "primaryKey: id\n",
        "KEY_VAULT_NAME: my-vault\n",
        "sortKey: createdAtTimestamp\n",
        "cacheKey: user-profile-cache-v2\n",
        "signingKey: https://keys.example/jwks.json\n",
        "webhook: release-notes\n",
        "webhook_url: https://ci.example/hooks/build\n",
        # A key whose last word says it names, refers to or configures a secret.
        f"secretName: {HEX}\n",
        f"secret_ref: {HEX}\n",
        f"passwordPolicy: {HEX}\n",
        "token_enabled: true\n",
        # Words that end in `key` and are not about keys.
        f"monkey: {HEX}\n",
        f"turkey: {HEX}\n",
        f"hockey: {HEX}\n",
        f"donkey: {HEX}\n",
        f"whiskey: {HEX}\n",
    ]

    def test_a_credential_under_a_key_or_webhook_is_masked(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_name_path_or_short_value_under_a_key_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class FusedSecretWords(SettingsIsolated):
    """A key segment ending in a secret word is named for a secret.

    Break: `DEFAULTPASSWORD=...` reaching the worker because the word boundary
    the rule split keys at is not there - the secret word is fused to another.
    """

    CASES = [
        ("PIN_DEFAULTPASSWORD=fake-pw\n", "PIN_DEFAULTPASSWORD=[masked]\n"),
        ("  - PIN_DEFAULTPASSWORD=fake-pw\n", "  - PIN_DEFAULTPASSWORD=[masked]\n"),
        ("DEFAULTPASSWORD=fake-pw\n", "DEFAULTPASSWORD=[masked]\n"),
        ("adminpassword: fake-pw\n", "adminpassword: [masked]\n"),
        ("APITOKEN=fake-tk\n", "APITOKEN=[masked]\n"),
    ]

    UNCHANGED = [
        "passwordless: magic-link\n",
        "tokenizer: wordpiece\n",
        "keyboard: uk-extended\n",
        "monkey: banana\n",
        # `pass` and `auth` fused are other words: a bypass, a protocol.
        "bypass: fake-pw\n",
        "oauth: provider-one\n",
    ]

    def test_a_fused_secret_word_masks_the_value(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_word_that_merely_contains_a_secret_word_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class PointersAreNotSecrets(SettingsIsolated):
    """A plain URL or a Terraform reference under a secret key points elsewhere.

    Break: an endpoint under an `auth` key, or `var.admin_password`, masked as a
    credential - the worker loses which service and which variable are wired
    in, and the operator reads a masked count inflated by things that are not
    secrets.
    """

    UNCHANGED = [
        "S2S_AUTH: http://service-auth-provider:4502\n",
        "token_url: https://login.example/oauth2/token\n",
        "jdbc_connection_string: jdbc:postgresql://db:5432/app\n",
        "vm_admin_password = var.admin_password\n",
        "password = local.db_password\n",
        "admin_password = module.db.admin_password\n",
        "client_secret = data.vault_secret.app.value\n",
        "password = each.value\n",
        "password = random_password.db.result\n",
        # The same pointers in the syntax that names the key on another line.
        "env:\n  - name: S2S_AUTH\n    value: http://service-auth-provider:4502\n",
        "env:\n  - name: DB_PASSWORD\n    value: var.admin_password\n",
    ]

    CASES = [
        # A URL's own credential and a secret in its query are still masked.
        (
            "S2S_AUTH: http://app:fake-pw@auth.example:4502\n",
            "S2S_AUTH: http://app:[masked]@auth.example:4502\n",
            "url-credential",
        ),
        (
            "S2S_AUTH: https://auth.example/cb?db_token=fake-tk\n",
            "S2S_AUTH: https://auth.example/cb?db_token=[masked]\n",
            "key-name",
        ),
        # Dotted, but not a Terraform reference.
        ("password = varx.admin\n", "password = [masked]\n", "key-name"),
        ("password: var-admin-pw\n", "password: [masked]\n", "key-name"),
    ]

    def test_a_url_or_terraform_reference_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)

    def test_what_is_not_a_pointer_is_still_masked(self):
        for text, want, rule in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({rule: 1}))


class ConnectionStrings(SettingsIsolated):
    """A connection string is masked where it holds a credential, not whole.

    Break: the server, database and options of every connection string
    withheld from the worker - or, the other way, its password, account key,
    URL password or SAS signature left in it once the whole-value rule is gone.
    """

    CASES = [
        (
            "ConnectionString: Server=db;User Id=app;Password=fake-pw;Pooling=true\n",
            "ConnectionString: Server=db;User Id=app;Password=[masked];Pooling=true\n",
            "connection-string-credential",
        ),
        (
            "storage_connection_string: DefaultEndpointsProtocol=https;AccountName=a;"
            "AccountKey=fakeKey==;EndpointSuffix=core.example\n",
            "storage_connection_string: DefaultEndpointsProtocol=https;AccountName=a;"
            "AccountKey=[masked];EndpointSuffix=core.example\n",
            "connection-string-key",
        ),
        (
            "DB_CONNECTION_STRING=postgres://app:fake-pw@db.example:5432/app\n",
            "DB_CONNECTION_STRING=postgres://app:[masked]@db.example:5432/app\n",
            "url-credential",
        ),
        (
            "blobConnectionString: BlobEndpoint=https://a.blob.example/;"
            "SharedAccessSignature=sv=2022-11-02&sig=fakeSig%3D;FileEndpoint=f\n",
            "blobConnectionString: BlobEndpoint=https://a.blob.example/;"
            "SharedAccessSignature=sv=2022-11-02&sig=[masked];FileEndpoint=f\n",
            "sas-signature",
        ),
    ]

    UNCHANGED = [
        "connectionString: Server=db;Database=app\n",
        "connectionString: fake-cs\n",
    ]

    def test_only_the_credential_inside_a_connection_string_is_masked(self):
        for text, want, rule in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({rule: 1}))

    def test_a_connection_string_holding_no_credential_is_unchanged(self):
        for text in self.UNCHANGED:
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class LiteralsInsideCode(SettingsIsolated):
    """Under a secret key, a literal passed to a call is masked; the call stays.

    Break: `DB_PASSWORD = base64encode("...")` reaching the worker because the
    value as a whole reads as code - or the call, its references or its
    punctuation changed when only the literal should.
    """

    LITERAL = "FakeLiteral" + "0123456789"

    def test_a_literal_in_a_call_is_masked_and_the_call_kept(self):
        for text, want in (
            (
                f'DB_PASSWORD = base64encode("{self.LITERAL}")\n',
                'DB_PASSWORD = base64encode("[masked]")\n',
            ),
            (
                f'vm_admin_password = coalesce(var.p, "{self.LITERAL}")\n',
                'vm_admin_password = coalesce(var.p, "[masked]")\n',
            ),
            (
                f"  token = sha256('{self.LITERAL}'),\n",
                "  token = sha256('[masked]'),\n",
            ),
        ):
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))

    def test_a_lookup_name_or_interpolation_in_a_call_is_unchanged(self):
        for text in (
            'password = os.environ["DB_PASSWORD"]\n',
            'password = config.get("db.password")\n',
            'password = get_secret("app-db")\n',
            'vm_admin_password = coalesce(var.p, "${var.fallback}")\n',
            f'description = base64encode("{self.LITERAL}")\n',
        ):
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)


class OnlyTheValueChanges(SettingsIsolated):
    """A quoted value followed by a closer loses the value and nothing else.

    Break: `rawPassword = '...');` masked to `rawPassword = [masked];`, eating
    the closing quote and bracket, so the worker reads code that no longer
    parses and is not the code in the file.
    """

    CASES = [
        ("rawPassword = 'FakePass0123456789abcd');\n", "rawPassword = '[masked]');\n"),
        ("rawPassword = 'FakePass0123456789abcd'),\n", "rawPassword = '[masked]'),\n"),
        ('rawPassword = "FakePass0123456789abcd"),\n', 'rawPassword = "[masked]"),\n'),
        ('rawPassword = "FakePass0123456789abcd"]\n', 'rawPassword = "[masked]"]\n'),
        ("rawPassword = 'FakePass0123456789abcd'}\n", "rawPassword = '[masked]'}\n"),
    ]

    def test_the_closers_after_a_masked_literal_survive(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"key-name": 1}))


class EscapedSasSignatures(SettingsIsolated):
    """A SAS URL written with `&amp;` still loses its signature.

    Break: a signed URL pasted into markdown or HTML, where `&` is `&amp;`,
    reaching the worker whole because `sig=` follows a `;` and not a `&`.
    """

    def test_the_signature_after_an_html_entity_is_masked_and_the_rest_kept(self):
        got, counts = masked(
            "see https://acct.blob.example/c/b?sv=2020-10-02&amp;sig=AbCdFake%3D&amp;se=2030 now"
        )
        self.assertEqual(
            got, "see https://acct.blob.example/c/b?sv=2020-10-02&amp;sig=[masked]&amp;se=2030 now"
        )
        self.assertEqual(counts, Counter({"sas-signature": 1}))

    def test_a_word_ending_in_sig_is_not_a_signature(self):
        unchanged_and_uncounted(self, "the layout&amp;design=round\n")


class UrlQuerySecrets(SettingsIsolated):
    """A query or fragment parameter named for a secret loses its value, wherever
    the URL sits.

    Break: `CALLBACK: https://.../cb?token=...` reaching the worker because the
    key-name rule read the whole line as one assignment under a key naming no
    secret, and never reached the parameter inside it.
    """

    TOKEN = "abcDEF" + "0123456789"

    def test_a_secret_parameter_is_masked_and_the_rest_of_the_url_kept(self):
        for text, want in (
            (
                f"CALLBACK: https://x.example/cb?token={self.TOKEN}&next=1\n",
                "CALLBACK: https://x.example/cb?token=[masked]&next=1\n",
            ),
            (
                f"see https://x.example/app#access_token={self.TOKEN}&token_type=bearer\n",
                "see https://x.example/app#access_token=[masked]&token_type=bearer\n",
            ),
            (
                f'<a href="https://x.example/r?page=2&amp;api_key={self.TOKEN}&amp;n=1">\n',
                '<a href="https://x.example/r?page=2&amp;api_key=[masked]&amp;n=1">\n',
            ),
            ("/login?Token=fake-tk&page=2", "/login?Token=[masked]&page=2"),
            (f"/x?CLIENT_SECRET={self.TOKEN}\r\n", "/x?CLIENT_SECRET=[masked]\r\n"),
            (f"go(https://x.example/?code={self.TOKEN})", "go(https://x.example/?code=[masked])"),
            (f"https://x.example/?key={self.TOKEN}'", "https://x.example/?key=[masked]'"),
        ):
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(counts, Counter({"url-query-secret": 1}))

    def test_an_ordinary_parameter_or_a_name_like_a_secret_is_unchanged(self):
        for text in (
            "https://x.example/cb?token_type=bearer\n",
            "https://x.example/list?page=2&sort=asc\n",
            "https://x.example/nlp?tokenizer=wordpiece\n",
            "https://x.example/status?code=200\n",
            "https://x.example/lookup?key=name\n",
            "https://x.example/cb?token=${CALLBACK_TOKEN}\n",
        ):
            with self.subTest(text=text):
                unchanged_and_uncounted(self, text)

    def test_a_signature_is_counted_once_by_its_own_rule(self):
        got, counts = masked("https://a.example/c?sv=2022&sig=fakeSig%3D&token=fake-tk\n")
        self.assertEqual(got, "https://a.example/c?sv=2022&sig=[masked]&token=[masked]\n")
        self.assertEqual(counts, Counter({"sas-signature": 1, "url-query-secret": 1}))


class PemBlocksKeepTheirLines(SettingsIsolated):
    """A private-key block is masked line by line, so every line after it keeps
    its number.

    Break: the block collapsed onto one line, so a worker citing a line of the
    masked copy below it points at the wrong line of the real file.
    """

    def test_each_line_of_the_block_is_masked_and_every_line_ending_kept(self):
        for text, want in (
            (
                f"key: |\r\n  {PEM_HEAD}\r\n  MIIBfake\r\n\r\n  AAAA\r\n  {PEM_TAIL}\r\nnext: 1\r\n",
                f"key: |\r\n  {PEM_HEAD}\r\n  {M}\r\n\r\n  {M}\r\n  {PEM_TAIL}\r\nnext: 1\r\n",
            ),
            (
                f'"{PEM_HEAD}\\nMIIBfake\\n{PEM_TAIL}\\n"',
                f'"{PEM_HEAD}{M}{PEM_TAIL}\\n"',
            ),
        ):
            with self.subTest(text=text):
                got, counts = masked(text)
                self.assertEqual(got, want)
                self.assertEqual(got.count("\n"), text.count("\n"))
                self.assertEqual(counts, Counter({"pem-private-key": 1}))


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
            "signing-cert: example-secret\nthe admin password is example-secret\n",
        ),
        (
            "a value that does not look like a credential, under a key whose secret word is "
            "`key`, `webhook`, or followed by another word",
            "signing-key: example-secret\nalert-webhook: example-secret\n"
            "secret_key_base: example-secret\n",
        ),
        ("a secret in a URL's path, under any key", "auth: https://hooks.example/example-secret\n"),
        (
            "a literal in a call that reads like a name",
            'password = get("DB_PASSWORD", "example-secret")\n',
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
