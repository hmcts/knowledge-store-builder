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
# The shipped .mjs modules are Node code; app.js alone never needed these.
NODE_TYPES_VERSION=24.3.0

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"

# --prefix keeps the install inside the checkout (gitignored). Without it npm
# searches upwards for a package.json and installs outside the repository.
prefix=${JS_TOOLCHAIN_PREFIX:-$repo_root/.js-toolchain}
bin=$prefix/node_modules/.bin

# Every piece is checked, not just the first: adding @types/node to this list
# installed nothing on any checkout that already had eslint, and tsc then failed
# with "cannot find type definition file" on a toolchain that looked present.
if [[ ! -x "$bin/eslint" ]] || [[ ! -x "$bin/tsc" ]] \
  || [[ ! -d "$prefix/node_modules/@types/node" ]]; then
  echo "installing pinned JS toolchain into ${prefix#"$repo_root"/}"
  mkdir -p "$prefix"
  [[ -f "$prefix/package.json" ]] || printf '{"name":"js-toolchain","private":true}\n' > "$prefix/package.json"
  npm install --prefix "$prefix" --ignore-scripts --no-audit --no-fund --silent \
    "eslint@$ESLINT_VERSION" "typescript@$TYPESCRIPT_VERSION" \
      "@types/node@$NODE_TYPES_VERSION"
fi

echo "eslint $ESLINT_VERSION"
"$bin/eslint" src/knowledgestore/assets/app.js src/knowledgestore/assets/*.mjs tests/explorer/*.mjs

echo "tsc $TYPESCRIPT_VERSION --checkJs (browser: app.js)"
"$bin/tsc" --checkJs --noEmit --target es2020 --lib es2020,dom \
  src/knowledgestore/assets/app.js

# Two invocations, not one, because the two halves are different runtimes. app.js
# is browser code and needs lib.dom; the shipped .mjs modules are Node code and
# need @types/node. Checking them together fails inside @types/node itself, where
# its URLPattern declaration contradicts lib.dom's - an error in neither of our
# files, and one that would have to be silenced with a blanket skipLibCheck.
echo "tsc $TYPESCRIPT_VERSION --checkJs (node: the shipped harness and answer gate)"
"$bin/tsc" --checkJs --noEmit --target es2022 --lib es2022 \
  --module nodenext --moduleResolution nodenext \
  --typeRoots "$prefix/node_modules/@types" --types node \
  src/knowledgestore/assets/explorer_harness.mjs \
  src/knowledgestore/assets/answer_regression.mjs

echo "explorer JavaScript gates pass"
