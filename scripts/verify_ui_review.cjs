#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');
const {scan} = require('./audit_ui.cjs');

function main(argv) {
  if (argv.length !== 4 || argv[0] !== '--app' || argv[2] !== '--output')
    throw new Error('Usage: node scripts/verify_ui_review.cjs --app Cursor.app --output report.json');
  const root = path.resolve(argv[1], 'Contents/Resources/app');
  const output = path.resolve(argv[3]);
  if (output === root || output.startsWith(root + path.sep))
    throw new Error('Report must be outside the application');
  const project = path.resolve(__dirname, '..');
  const profile = JSON.parse(fs.readFileSync(path.join(project, 'patches/profile.json'), 'utf8'));
  const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
  for (const [relative, expected] of Object.entries(profile.artifacts)) {
    if (sha(fs.readFileSync(path.join(project, relative))) !== expected)
      throw new Error('Artifact fingerprint mismatch: ' + relative);
  }
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const product = JSON.parse(fs.readFileSync(path.join(root, 'product.json'), 'utf8'));
  if (pkg.version !== profile.cursor_version || product.commit !== profile.cursor_commit)
    throw new Error('Unsupported Cursor version');
  const acorn = createRequire(path.join(root, 'package.json'))('acorn');
  const patches = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(project, profile.ui_review_patch)))).files;
  const dictionary = JSON.parse(fs.readFileSync(path.join(project, profile.ui_review_dictionary), 'utf8')).translations;
  const report = {schema: 1, version: pkg.version, commit: product.commit,
    no_application_writes: true, files: []};
  for (const [relative, patch] of Object.entries(patches)) {
    const source = fs.readFileSync(path.join(root, relative));
    if (source.length !== patch.before_size || sha(source) !== patch.before_sha256)
      throw new Error('Input is not the audited baseline: ' + relative);
    const text = source.toString('utf8');
    const before = scan(text, acorn, relative.endsWith('.json'));
    const allowed = new Map();
    let characterPosition = 0, bytePosition = 0;
    for (const item of [...before.candidates].sort((a, b) => a.start - b.start)) {
      bytePosition += Buffer.byteLength(text.slice(characterPosition, item.start));
      const end = bytePosition + Buffer.byteLength(text.slice(item.start, item.end));
      allowed.set(bytePosition, {value: item.value, end});
      characterPosition = item.end;
      bytePosition = end;
    }
    const pieces = [];
    let cursor = 0;
    for (const edit of patch.edits) {
      const node = allowed.get(edit.start);
      if (!node || !dictionary[node.value] || edit.start < cursor ||
          edit.end !== node.end)
        throw new Error('Edit is not a reviewed UI literal');
      const replacement = Buffer.from(edit.data, 'base64');
      if (JSON.parse(replacement.toString('utf8')) !== dictionary[node.value])
        throw new Error('Edit differs from reviewed translation');
      pieces.push(source.subarray(cursor, edit.start), replacement);
      cursor = edit.end;
    }
    pieces.push(source.subarray(cursor));
    const target = Buffer.concat(pieces);
    if (sha(target) !== patch.after_sha256 || target.length !== patch.after_size)
      throw new Error('Generated target fingerprint mismatch');
    const after = scan(target.toString('utf8'), acorn, relative.endsWith('.json'));
    if (before.structure_sha256 !== after.structure_sha256 || before.strings !== after.strings)
      throw new Error('Non-string tokens or number of string nodes changed');
    const remaining = after.candidates.filter(item => Object.hasOwn(dictionary, item.value));
    if (remaining.length) throw new Error('Reviewed translation remains unapplied');
    report.files.push({path: relative, corrected_ui_literals: patch.edits.length,
      non_string_tokens_unchanged: true, string_node_count_unchanged: true,
      no_reviewed_literal_remaining: true, before_sha256: patch.before_sha256,
      after_sha256: patch.after_sha256});
  }
  report.pass = true;
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', {flag: 'wx'});
  console.log(JSON.stringify(report, null, 2));
}

if (require.main === module) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error('Verification stopped: ' + error.message); process.exitCode = 1; }
}
