"""Masking over whole files, one per format an estate commits.

Break: a format whose secrets reach a worker - a planted value surviving in the
masked file - or masking that changes more than the secret, so the worker reads
a file that differs from the original beyond the values it must not see.

The expected output is the template rendered with `[masked]` at each planted
marker, which a person placed by hand; it is never derived from `mask`.
"""

from __future__ import annotations

import unittest

import secret_corpus
from settings_isolation import SettingsIsolated

from knowledgestore import secret_mask

# What each file must keep verbatim: references, and values that are not secrets.
SURVIVES = {
    "app.dotenv": ("PAYMENT_TOKEN=${PAYMENT_TOKEN}", "FEATURE_X=true", "DB_HOST=db.example"),
    "application.properties": ("app.api.key=${API_KEY}", "spring.datasource.username=orders"),
    "application.yml": ("client-id: orders-client", "scope: openid,profile", "ttl: 600"),
    "appsettings.json": (
        '"Issuer": "https://orders.example"',
        '"Uri": "https://orders-kv.vault.example/"',
        "Server=db.example;Database=orders;User Id=orders;",
    ),
    "certs.yaml": ("MIIBfakeCertificateBody", secret_corpus.KEPT["pem_head"], "issuer: orders-ca"),
    "chart-values.yaml": (
        "S2S_AUTH: http://service-auth-provider:4502",
        "KEY_VAULT_NAME: reporting-vault",
        "cacheKey: user-profile-cache-v2",
        "secretName: web-secrets",
        "webhook_url: https://ci.example/hooks/build",
        "jdbc_connection_string: jdbc:postgresql://db.example:5432/reporting",
        "key: password",
    ),
    "client.py": (
        "self.password = password",
        "self.token = token",
        'password = os.environ["DB_PASSWORD"]',
        "password=password, token=self.token)",
        'f"Bearer {self.token}"',
    ),
    "deploy.sh": ('--account-key "$STORAGE_KEY"', "--set database.host=db.example \\"),
    "deployment.yaml": ("value: prod", "secretKeyRef:", "value: ${CACHE_AUTH}"),
    "docker-compose.yml": ("API_TOKEN=${API_TOKEN}", "POSTGRES_USER: orders"),
    "main.tf": ('default = "uksouth"', "value = var.api_token", 'value = "2"'),
    "secret.yaml": ("name: orders-db", "type: Opaque"),
    "seed_users.py": ('password="[masked]")', 'password=os.environ["VIEWER_PASSWORD"]'),
    "settings.ini": ("host = db.example", "data = /var/lib/orders"),
    "sso.properties": (
        "sso.keyFile=/etc/sso/signing.pem",
        "spring.datasource.url=jdbc:postgresql://db.example:5432/orders",
        "sig=[masked];FileEndpoint=https://acct.file.example/",
    ),
    "storage.md": ("&amp;sig=[masked]&amp;se=2030-01-01",),
    "terraform.tfvars": ('admin_username = "ordersadmin"', 'owner       = "orders-team"'),
    "UserService.java": (
        "this.password = password;",
        "String token = tokenStore.issue(user);",
        'System.getenv("APP_SECRET")',
    ),
    "V3__roles.sql": ("VALUES ('smtp_host', 'smtp.example')",),
    "vm.tf": (
        "vm_admin_password = var.admin_password",
        "db_password    = random_password.db.result",
        "client_secret  = data.vault_secret.app.value",
        'base64encode("[masked]")',
        'coalesce(var.admin_password, "[masked]")',
        'lookup(var.tokens, "API_TOKEN")',
    ),
    "values.yaml": (
        'password: "{{ .Values.global.redisPassword }}"',
        "passwordSecretRef: orders-db",
        "AccountName=orders;",
        "tokenTtl: 300",
        "value: info",
    ),
    "web.config": (
        'value="#{StorageAccessKey}#"',
        '<add key="Environment" value="Production" />',
        'userName="orders"',
    ),
}

LINE_ENDINGS = {"LF": "\n", "CRLF": "\r\n"}


class CorpusProperties(SettingsIsolated):
    def test_every_format_is_in_the_corpus_and_has_its_survivors_named(self):
        # Break: a fixture added without the checks below knowing what it keeps.
        self.assertEqual(sorted(name for name, _ in secret_corpus.corpus()), sorted(SURVIVES))

    def test_no_planted_value_survives_and_nothing_else_changes(self):
        for name, template in secret_corpus.corpus():
            for ending, newline in LINE_ENDINGS.items():
                with self.subTest(file=name, ending=ending):
                    source = secret_corpus.render(template, masked=False).replace("\n", newline)
                    want = secret_corpus.render(template, masked=True).replace("\n", newline)
                    got = secret_mask.mask(source)
                    for planted in secret_corpus.planted_names(template):
                        for trace in secret_corpus.planted_traces(planted):
                            self.assertIn(trace, source)
                            self.assertNotIn(trace, got, f"{planted} survived in {name}")
                    for kept in SURVIVES[name]:
                        self.assertIn(kept.replace("\n", newline), got)
                    # Every byte outside the planted spans, line endings included.
                    self.assertEqual(got, want)

    def test_masking_keeps_every_line_of_every_file(self):
        # Break: a value masked across lines - a private-key block collapsed onto
        # one - so a worker citing a line below it in the masked copy cites the
        # wrong line of the real file.
        for name, template in secret_corpus.corpus():
            for ending, newline in LINE_ENDINGS.items():
                with self.subTest(file=name, ending=ending):
                    source = secret_corpus.render(template, masked=False).replace("\n", newline)
                    got = secret_mask.mask(source)
                    self.assertEqual(got.count(newline), source.count(newline))
                    self.assertEqual(got.count("\n"), source.count("\n"))

    def test_source_code_is_not_masked_at_all(self):
        # Break: masking that reads code as config, so a worker extracting a
        # class sees `[masked]` where a parameter or a member was.
        for name, template in secret_corpus.corpus():
            if not secret_corpus.planted_names(template):
                with self.subTest(file=name):
                    self.assertEqual(secret_mask.mask(template), template)


if __name__ == "__main__":
    unittest.main()
