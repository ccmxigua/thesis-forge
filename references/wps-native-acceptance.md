# Scoped WPS native acceptance

This is a host-operated checklist, not an upload automation. Use the browser
and document tools actually exposed by the current conversation. Check the
user's upload/sharing authorization for the exact candidate and destination.
Never infer access from a prior engineering thread or bypass a rejected route.

1. Verify the exact input candidate bytes and current session binding. Preserve
   the source. Reserve `wps_acceptance` in the existing campaign using the run
   ID as candidate ID and a stable operation ID. Proceed only when the returned
   `can_execute` is true. A resumed operation is not a fresh permission to upload.
2. Use the supported WPS UI. If a file-picker submission closes with no clear
   success/error, record `upload_status=unknown` and look for the existing object
   in the chosen folder, recent documents and exact-name search. Do not repeat
   submission until the earlier outcome is known. Only evidence of non-execution
   can release the reservation.
3. Identify the actual opened native object's exact URL. Record a visible
   version if available; otherwise use null. Record UTC observation time and
   actual tool/screenshot references. Do not invent a version or equate the
   current online bytes with the uploaded local hash after online edits.
4. Refresh complex TOC/page fields natively when needed. For each actual TOC
   entry, click the link and verify the body target, not just cursor motion or
   the outline navigator. Record tested entry/target pairs in the check scope.
   Observe displayed page numbers across frontmatter/body/appendix boundaries;
   physical page indexes are not automatically printed page labels.
5. Inspect source-bound heading keep-with-next behavior, key embedded objects,
   and a meaningful font sample at readable zoom. Record specific observed
   paragraphs/objects/pages. Explicitly mark absent features not_applicable.
   A font sample is not a full-font audit, visible OLE is not OLE editability,
   and visually clean equations do not certify mathematical correctness.
6. Save, close and reopen the same object through supported UI. Recheck object
   URL/version and the material checks. A changed URL or mismatched reopened
   version blocks reuse. A new online version requires fresh observations.
7. Import the exact observation using `native_acceptance.py record`. Include
   all five check categories: toc_links, page_number_sequence,
   heading_keep_next, embedded_objects, font_sample. Each must state its scope
   and evidence references. No check or save-close-reopen can be guessed.
8. If an edited native copy was downloaded through a supported action, bind its
   exact bytes, observed object URL/version and actual download evidence in
   `exported_bytes_binding`. Store it separately inside the run. If there was
   no downloaded copy, this field is null. Never copy the input hash into it.

`pass_scoped` is only the recorded WPS scope, not Word acceptance. Formal
independent/provider/submission flags remain false. Unit-test fixture evidence
is not production acceptance. Retain unresolved author decisions and testing
limits in the final result, even when the five scoped checks pass.
