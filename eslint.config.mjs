// Core ESLint rules only - no plugins - so the lint step needs no install
// beyond eslint itself, and mirrors the complexity limits enforced in review.
const browserGlobals = {
  document: 'readonly', window: 'readonly', Option: 'readonly',
  setTimeout: 'readonly', clearTimeout: 'readonly',
};
const nodeGlobals = { console: 'readonly', process: 'readonly' };
const rules = {
  complexity: ['error', 15],
  'no-nested-ternary': 'error',
  'no-unused-vars': 'error',
  'no-undef': 'error',
};

export default [
  {
    files: ['src/knowledgestore/assets/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'script', globals: browserGlobals },
    rules,
  },
  {
    files: ['tests/explorer/*.mjs'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: nodeGlobals },
    rules,
  },
];
