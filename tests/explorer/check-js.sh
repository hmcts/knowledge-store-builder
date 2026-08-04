#!/usr/bin/env bash
# The explorer's JavaScript gates: lint and type-check, exactly as CI runs them.
#
# This script exists because the two ways of running these checks had drifted
# apart, and the local one was the weaker. The repository is a Python package
# with no package.json, so a bare `npm install` walks up the directory tree and
# installs into whatever parent has one - in one case a home directory, far
# outside the checkout. An ad-hoc `npx tsc` then resolves a stray node_modules
# above the repository, reports unrelated errors from it, and misses the real
# ones in this file: four implicit-any errors reached CI that way.
#
# So the toolchain is pinned here, installed into a prefix inside the checkout,
# and CI calls this same script. There is one command and one set of versions,
# and a local pass means what CI means.
set -euo pipefail

ESLINT_VERSION=10.8.0
TYPESCRIPT_VERSION=7.0.2

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"

# --prefix keeps the install inside the checkout (gitignored). Without it npm
# searches upwards for a package.json and installs outside the repository.
prefix=${JS_TOOLCHAIN_PREFIX:-$repo_root/.js-toolchain}
bin=$prefix/node_modules/.bin

if [ ! -x "$bin/eslint" ] || [ ! -x "$bin/tsc" ]; then
  echo "installing pinned JS toolchain into ${prefix#"$repo_root"/}"
  mkdir -p "$prefix"
  [ -f "$prefix/package.json" ] || printf '{"name":"js-toolchain","private":true}\n' > "$prefix/package.json"
  npm install --prefix "$prefix" --ignore-scripts --no-audit --no-fund --silent \
    "eslint@$ESLINT_VERSION" "typescript@$TYPESCRIPT_VERSION"
fi

echo "eslint $ESLINT_VERSION"
"$bin/eslint" src/knowledgestore/assets/app.js tests/explorer/*.mjs

echo "tsc $TYPESCRIPT_VERSION --checkJs"
"$bin/tsc" --checkJs --noEmit --target es2020 --lib es2020,dom \
  src/knowledgestore/assets/app.js

echo "explorer JavaScript gates pass"
