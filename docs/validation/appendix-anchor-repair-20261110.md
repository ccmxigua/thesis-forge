# Appendix-label preservation repair

## Scope and defect

This repair extends the appendix/TOC safeguards published at
`4f6337e4a4300fc5355a030327357f2af6de15bd`. It does not change thesis equations,
content, school requirements, native-acceptance budgets, or GitHub Actions
configuration.

Appendix normalization previously assigned `Paragraph.text`. Although the
visible label was corrected, that assignment rebuilt the paragraph's runs and
removed bookmarks, comment ranges, character formatting, and embedded objects.
Losing a native bookmark can also invalidate a TOC destination. Three new
regressions reproduced these losses before the implementation change.

The normalizer now edits only the label and separators in existing text nodes.
It retains title runs and range anchors. Fields, hyperlinks, drawings and other
unsupported structures are left intact and reported as an unresolved label for
review rather than flattened. Repeated normalization is idempotent.

## Verification scope

The new regression covers bookmarks, preserved title XML, split label runs,
comment anchors, whitespace and titleless headings, idempotence, and fail-closed
handling of drawings, fields and hyperlinks. An independent review found no
blocking code issue and additionally checked randomized run boundaries.

Validation uses Python 3.12.14 and the exact dependencies in
`requirements-test.txt`. The host has Pandoc 3.1.11.1 and
LibreOfficeDev 26.8.0.0.alpha0; native fixture results are specific to those
versions. Repository fixtures are not a new business candidate or native WPS
acceptance run.

Results recorded on 2026-11-10 UTC:

- Complete repository `python -m pytest -q`: **2,552 passed, 2,029 subtests
  passed, 6 skipped, 5 warnings**, exit code 0, in 729.67 seconds.
- Focused appendix/TOC, reviewed-rule isolation, current-session, native-gate
  and root-cause regression: **240 passed, 48 subtests passed**. This earlier
  run included the first four new regression cases. The final complete run
  includes all six new cases.
- Final new test module alone: **6 passed, 4 subtests passed**.
- `compileall` and `git diff --check`: exit code 0. Existing invalid-escape
  warnings in two legacy patch scripts remain visible. The complete run also
  reports two Pillow deprecation warnings and the intentional duplicate-ZIP
  fixture warning.
- Portable runtime archive: exact-payload verification passed, with no missing,
  mismatched or unexpected files. ZIP SHA-256:
  `ab0e41591f613030cd354b84a61a277ac198adef49d6bce03cd5bda79d93e890`.

Skipped tests are not counted as passes. No claim of native WPS success follows
from these regression totals.

## Native acceptance remains separate

Automated regression and a verified runtime archive do not establish a WPS
TOC/page-number pass. The original candidate, source/spec bindings and campaign
budget must be recovered before resuming that native acceptance. Neither a new
campaign nor fresh candidate is created by this repair to evade the original
limits. Previously failed or incomplete native results remain failed or
incomplete. No user manuscript or independent ongoing document was changed.

The historical `current-session-release.md` and `release-status.json` are left
unchanged. Their counts and native observations belong to their original run,
not this repair.
