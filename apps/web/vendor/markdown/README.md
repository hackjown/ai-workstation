# Vendored Markdown renderer

Used by `apps/web/mdview.js` (file previews on the web and in the iOS app). Served from this repo, never from a CDN.

| File | Package | Version | Licence | Source |
|---|---|---|---|---|
| markdown-it.min.js | markdown-it | 15.0.3 | MIT (LICENSE-markdown-it) | npm tarball `dist/browser/markdown-it.umd.min.js` |
| purify.min.js | dompurify | 3.4.17 | Apache-2.0 OR MPL-2.0 (LICENSE-dompurify) | npm tarball `dist/purify.min.js` |

Both tarballs were checked against the npm registry's sha512 integrity before copying. The only change is the
removed trailing `//# sourceMappingURL=` line (the maps are not vendored).

sha256 of the vendored files:

```
b06c85174d2b1bb6339dfa1f0bc938cea57f62663f132a77abbf651562542443  markdown-it.min.js
1bb973289c6a99e511f9209b4159384a6020ead799eca059f065cf213b28e1f3  purify.min.js
```

To update: download the new tarballs from registry.npmjs.org, verify `dist.integrity`, copy the same two files,
strip the sourceMappingURL line, update this table.
