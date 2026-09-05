---
description: Regenerate graphify-out.md, the single-file map of this codebase's structure
---

Run `python scripts/graphify.py` from the repo root and report what it wrote.

This overwrites [graphify-out.md](../../graphify-out.md) with the current
directory tree, plus every Python file's top-level classes/functions (via
`ast`, no dependency needed). Run it after adding/removing/renaming files,
functions, or classes so the map stays truthful.
