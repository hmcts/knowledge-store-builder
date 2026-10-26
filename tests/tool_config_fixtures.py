"""Tool-configuration files that declare a `$schema`, in the shape the tools write them.

A `$schema` on a tool's own schema says the file is an *instance* of that schema,
not a schema. Every document below is a public format written out in its real
layout with invented values, because the `json-schema` stage was first tested only
against real schemas and so never met these (CLAUDE.md 2.4). Several share keyword
names with JSON Schema (`extends` above all), which is the trap.

Keyed by the path each lives at in a clone, as the tools expect to find it.
"""

from __future__ import annotations

RENOVATE = """{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", "local>example-org/renovate-config:base"],
  "packageRules": [
    {"matchUpdateTypes": ["minor", "patch"], "automerge": true}
  ]
}
"""

TSCONFIG = """{
  "$schema": "https://json.schemastore.org/tsconfig",
  "extends": "./tsconfig.base.json",
  "compilerOptions": {
    "target": "ES2022",
    "module": "esnext",
    "strict": true,
    "outDir": "dist"
  },
  "include": ["src"]
}
"""

ESLINTRC = """{
  "$schema": "https://json.schemastore.org/eslintrc",
  "root": true,
  "extends": ["eslint:recommended", "plugin:@example/recommended"],
  "rules": {"no-console": "warn"}
}
"""

ESLINTRC_STRING_EXTENDS = """{
  "$schema": "https://json.schemastore.org/eslintrc",
  "extends": "@example/eslint-config",
  "env": {"node": true}
}
"""

ARM_TEMPLATE = """{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "storageName": {"type": "string", "defaultValue": "examplestore"}
  },
  "resources": [
    {
      "type": "Microsoft.Storage/storageAccounts",
      "apiVersion": "2023-01-01",
      "name": "[parameters('storageName')]",
      "location": "uksouth"
    }
  ]
}
"""

VSCODE_SETTINGS = """{
  "$schema": "vscode://schemas/settings/user",
  "editor.tabSize": 2,
  "files.exclude": {"**/.git": true},
  "extends": ["example.base-settings"]
}
"""

PACKAGE_JSON_MODULE = """{
  "name": "example-package",
  "version": "1.0.0",
  "type": "module",
  "items": ["a", "b"],
  "dependencies": {"left-pad": "^1.3.0"}
}
"""

COMPOSER = """{
  "$schema": "https://getcomposer.org/schema.json",
  "name": "example-org/example-package",
  "type": "library",
  "require": {"php": ">=8.2"},
  "extra": {"items": ["one"]}
}
"""

# The path each lives at in a clone. `package.json` carries no `$schema`: the one
# real-world file here that is judged by the no-`$schema` branch instead.
FILES = {
    "renovate.json": RENOVATE,
    "tsconfig.json": TSCONFIG,
    ".eslintrc.json": ESLINTRC,
    "web/.eslintrc.json": ESLINTRC_STRING_EXTENDS,
    "infra/azuredeploy.json": ARM_TEMPLATE,
    ".vscode/settings.json": VSCODE_SETTINGS,
    "package.json": PACKAGE_JSON_MODULE,
    "composer.json": COMPOSER,
}
