#!/usr/bin/env node
// Read packaged UI resources without running Cursor code.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');

const uiProperties = new Set([
  'label', 'title', 'description', 'placeholder', 'aria-label', 'ariaLabel',
  'text', 'textContent', 'innerText', 'tooltip', 'message', 'detail', 'caption',
  'emptyMessage', 'markdownDescription', 'buttonLabel', 'sublabel', 'subLabel',
  'helperText', 'prompt', 'primaryLabel', 'regretLabel', 'cancelLabel',
  'confirmLabel', 'children', 'disabledByTeamAdmin', 'notReady',
  'displayName',
]);
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const english = value => /[a-zA-Z]{2}/.test(value) && !/[\u3400-\u9fff]/.test(value);
const keyName = node => node?.name ?? node?.value;

function scan(source, acorn, json = false) {
  if (json) JSON.parse(source);
  const ast = json ? acorn.parseExpressionAt(source, 0, {ecmaVersion: 'latest'}) : acorn.parse(source, {
    ecmaVersion: 'latest', sourceType: 'module', allowReturnOutsideFunction: true,
  });
  const candidates = [];
  const dynamicCandidates = [];
  let strings = 0, templates = 0, uiStrings = 0;
  const stack = [[ast, null]];
  while (stack.length) {
    const [node, parent, grandparent] = stack.pop();
    if (node.type === 'TemplateLiteral') {
      templates++;
      if (parent?.type === 'Property' && parent.value === node &&
          uiProperties.has(keyName(parent.key))) {
        const segments = node.quasis.map(part => part.value.cooked ?? part.value.raw);
        if (segments.some(english)) dynamicCandidates.push({
          start: node.start, end: node.end, kind: 'template:' + keyName(parent.key),
          segments, expressions: node.expressions.length,
        });
      }
    }
    if (node.type === 'Literal' && typeof node.value === 'string') {
      strings++;
      let kind;
      if (parent?.type === 'Property' && parent.value === node && !parent.computed &&
          uiProperties.has(keyName(parent.key))) kind = 'property:' + keyName(parent.key);
      if (parent?.type === 'AssignmentExpression' && parent.right === node &&
          parent.left.type === 'MemberExpression' && !parent.left.computed &&
          uiProperties.has(keyName(parent.left.property))) kind = 'assignment:' + keyName(parent.left.property);
      if (parent?.type === 'ArrayExpression' && grandparent?.type === 'Property' &&
          ['enumDescriptions', 'markdownEnumDescriptions'].includes(keyName(grandparent.key)))
        kind = 'enum-description';
      if (parent?.type === 'CallExpression') {
        const callee = keyName(parent.callee.property) ?? keyName(parent.callee);
        if (callee === 'setAttribute' &&
            ['aria-label', 'title', 'placeholder', 'alt'].includes(parent.arguments[0]?.value) &&
            parent.arguments[1] === node) kind = 'attribute:' + parent.arguments[0].value;
        if (['createTextNode', 'insert', 'text'].includes(callee)) kind = 'text:' + callee;
        if (/^<[a-z][\s\S]*>/.test(node.value)) kind = 'markup';
      }
      if (kind) {
        uiStrings++;
        if (english(node.value)) candidates.push({
          start: node.start, end: node.end, kind, value: node.value,
        });
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (key === 'start' || key === 'end') continue;
      if (Array.isArray(value)) {
        for (const child of value) if (child?.type) stack.push([child, node, parent]);
      } else if (value?.type) stack.push([value, node, parent]);
    }
  }
  const hash = crypto.createHash('sha256');
  for (const token of acorn.tokenizer(source, {ecmaVersion: 'latest', sourceType: 'module'})) {
    hash.update(JSON.stringify([token.type.label,
      token.type.label === 'string' ? null : source.slice(token.start, token.end)]) + '\n');
  }
  return {strings, templates, uiStrings, structure_sha256: hash.digest('hex'),
    candidates, dynamicCandidates};
}

function main(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!['--app', '--parser', '--output'].includes(argv[i]) || !argv[i + 1])
      throw new Error('Usage: node scripts/audit_ui.cjs --app Cursor.app --output report.json [--parser acorn.js]');
    args[argv[i]] = argv[i + 1];
  }
  if (!args['--app'] || !args['--output']) throw new Error('--app and --output are required');
  const root = path.resolve(args['--app'], 'Contents/Resources/app');
  const acorn = args['--parser'] ? require(path.resolve(args['--parser'])) :
    createRequire(path.join(root, 'package.json'))('acorn');
  const profile = JSON.parse(fs.readFileSync(path.join(__dirname, '../patches/profile.json'), 'utf8'));
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const product = JSON.parse(fs.readFileSync(path.join(root, 'product.json'), 'utf8'));
  if (pkg.version !== profile.cursor_version || product.commit !== profile.cursor_commit)
    throw new Error('Unsupported Cursor build; no audit coverage claim can be made');
  const files = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      if (entry.isSymbolicLink()) throw new Error('Refusing symlink in resource tree');
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(full);
      else if (entry.isFile() && entry.name.endsWith('.js') &&
               path.relative(root, full).split(path.sep).join('/') !== profile.runtime_path)
        files.push(full);
    }
  }
  visit(path.join(root, 'out'));
  const extensionManifests = [];
  const extensionRoot = path.join(root, 'extensions');
  for (const entry of fs.readdirSync(extensionRoot, {withFileTypes: true})) {
    if (!entry.isDirectory()) continue;
    const manifest = path.join(extensionRoot, entry.name, 'package.json');
    if (!fs.existsSync(manifest)) continue;
    const pkg = JSON.parse(fs.readFileSync(manifest, 'utf8'));
    extensionManifests.push({id: `${pkg.publisher}.${pkg.name}`});
    if (['anysphere', 'inspecta'].includes(pkg.publisher)) {
      files.push(manifest);
      const defaults = path.join(extensionRoot, entry.name, 'package.nls.json');
      if (fs.existsSync(defaults)) files.push(defaults);
    }
  }
  const report = {schema: 1, version: pkg.version, commit: product.commit,
    no_changes_made: true, limitations: [
      'Static candidates require review; English does not automatically mean missing translation.',
      'Dynamic templates, remote content, user text, and runtime-only UI are not certified translated.',
    ], extension_manifests: extensionManifests, files: []};
  for (const file of files.sort()) {
    const bytes = fs.readFileSync(file);
    const parsed = scan(bytes.toString('utf8'), acorn, file.endsWith('.json'));
    report.files.push({path: path.relative(root, file).split(path.sep).join('/'),
      sha256: digest(bytes), ...parsed});
  }
  const output = path.resolve(args['--output']);
  if (output === root || output.startsWith(root + path.sep))
    throw new Error('Audit report must not be written into the application');
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', {flag: 'wx'});
  console.log(JSON.stringify({files: report.files.length,
    strings: report.files.reduce((sum, file) => sum + file.strings, 0),
    candidates: report.files.reduce((sum, file) => sum + file.candidates.length, 0),
    no_changes_made: true}));
}

module.exports = {scan};
if (require.main === module) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error('Audit stopped: ' + error.message); process.exitCode = 1; }
}
