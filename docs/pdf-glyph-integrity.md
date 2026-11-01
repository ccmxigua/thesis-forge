# PDF glyph observations do not establish native font acceptance

An actual local LibreOffice export preserved the date's ToUnicode text while
painting four boxes. The embedded TrueType subset assigned those letters a
nonzero glyph ID whose complete `glyf` record was identical to glyph zero,
`.notdef`. Checking only U+FFFD or the presence of a font resource missed it.

Both `audit_equation_export.py` and `rendered_format_audit.py` now use the same
read-only glyph-record inspection. Visible printing characters using the exact
`.notdef` record cause a manual-review finding, even after glyph IDs
have been renumbered. Invalid TrueType tables and ambiguous resource mappings
produce explicit findings. Whitespace, format/variation controls, invisible
text and transparent spans are excluded from this visible-glyph claim.
Known empty `.notdef` records in supported TrueType programs are included:
positive character widths and normal Unicode do not make an empty letter a
verified painted glyph. This finding requests review instead of rewriting it.
The documented MuPDF ligature representation (a positive compound glyph
followed at the same origin by zero-advance glyph `-1` components) is inspected
once through its painted glyph. Zero advance is checked in the span's writing
direction: a 90-degree continuation has zero height, and an oblique continuation
has a two-dimensional bounding envelope. The envelope's dimensions and center
must both match a perpendicular line through the origin, allowing only floating
point rounding at glyph scale. Missing or invalid direction remains unknown.
An unbound or advancing negative glyph
remains unresolved, including for unembedded, unsupported and unmatched font
resources. The negative-glyph check does not depend on the TrueType parser.
See the primary [PyMuPDF text-trace documentation](https://pymupdf.readthedocs.io/en/latest/functions.html#Page.get_texttrace).

Resource-to-trace identity uses the exact PDF BaseFont name (apart from its
subset prefix) and actual embedded sfnt name-ID 6 PostScript names. Family and
full names, removed spaces and case folding do not grant an alias. A printing
glyph with no provable resource mapping requires manual review, rather than
being counted as uninspected success. Known malformed name tables also block.

The unit of inspection is a painted positive-ID glyph plus its same-origin,
zero-advance `-1` continuations. A leading whitespace or format Unicode can
still carry a real glyph for a later printing Unicode in the same compound.
The whole cluster decides whether inspection is needed: a legitimate glyph
passes this narrow record check, a `.notdef` still blocks, and pure nonprinting
clusters retain their exemption. No source Unicode or PDF contents are edited.

This is a narrow integrity gate. It cannot identify every tofu outline, infer
which font a native editor used, validate role-level font placement, or prove
WPS/Word compatibility. CFF and other unsupported font programs and unembedded
standard fonts are disclosed in coverage instead of being described as
inspected TrueType records. A legitimately intended glyph indistinguishable
from `.notdef` needs review; the tool does not rewrite it or guess intent.

No fonts, symbols, alignments, source DOCX, PDF or mathematical subtrees are
changed. The WPS equation-array first-character defect remains open. A local
PDF gate must never be used as a successful native editor receipt.

## Active PDF resources for repeated subset names

When several page font objects share a PostScript name, their glyph indices
are local to different embedded subsets. The checker can now resolve the
active `Tf` resource within the current page before inspecting its glyph.
It reads an in-memory snapshot of the currently open PDF, checks the page's
resource name and object number, and verifies the embedded font bytes against
the inspected program. Text paint groups must correspond one-to-one to the
engine's trace sequence; each supported span must have one unique full
ToUnicode and glyph-ID sequence match. A competing unused subset cannot make
the selected letter missing, nor lend a real glyph to a selected `.notdef`.

This resolver is deliberately conservative: it supports simple symbolic
TrueType fonts with one MacRoman format0 cmap and one-byte bfchar ToUnicode.
Forms/XObjects, unresolved resource scope, explicit alternative encodings,
unsupported CMaps/clusters, nested graphics state and ambiguous matches do
not authorize selection. Missing optional `pypdf` support also leaves repeated
names unresolved. The previous single-resource inspection, negative/compound
glyph protections and exact PostScript alias rules remain in effect.
When one text paint group uses several active resources, an unsupported or
unproven resource remains a competing uncertainty. The resolver does not drop
it and call another resource uniquely matched. All active resources in that
group must be inspectable before a unique match can authorize selection;
unsupported Type1 programs therefore cannot borrow a supported TrueType
resource's coverage. This intentionally leaves such mixed groups unresolved.
Every known empty printing glyph now requires review, including an empty
record distinct from a nonempty `.notdef`; no font or document is rewritten.

`resource_binding` records page, resource name, font object and trace span.
`record_coverage` explicitly separates inspected Unicode coverage from the
uninspected remainder, including Type1/CFF. Complete inspection is a coverage
statement, not a claim of correct glyph shapes, semantic content, exact
specified typeface or native-editor acceptance. The latter claims remain
false. The current four-page controls resolve the old592 repeated-subset
ambiguities, while321 Type1 CJK characters remain outside TrueType inspection.
