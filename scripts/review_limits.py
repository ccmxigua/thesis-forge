"""Host-neutral review packing defaults, not provider token-limit claims."""

# Each primary chunk also produces a complete source-bound independent review.
# Keep this target conservative; physical source occurrences and fixed
# declarations remain atomic even if they exceed it. No clause is truncated.
DEFAULT_HOST_REVIEW_CHUNK_SIZE = 8
