---
worth: no
added: 2026-10-04
---
# Hover markdown escapes punctuation and flattens see cref

Hover text comes back as `most\-recent first\.` and `<see cref="Bus.Changed"/>` as plain `Bus\.Changed`. Not a
DotRush defect: roslyn-language-server 5.12 returns the same escaping (plus `&nbsp;` around inline code, which DotRush
has dropped since #214). It is Roslyn's own doc-comment formatter; reporting it to DotRush would be noise.
