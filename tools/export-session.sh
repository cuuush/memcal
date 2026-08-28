#!/usr/bin/env bash
# Export a Hermes session into memcal/transcripts/ under its own session id, so
# reading a new session never overwrites the last one.
#
#   ./tools/export-session.sh              # most recent session
#   ./tools/export-session.sh <session-id> # a specific one
#
# Debugging memcal means reading how the agent actually used it, and that means
# keeping the old transcripts around to compare against.
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/transcripts"
mkdir -p "$DEST"

SID="${1:-}"
if [[ -z "$SID" ]]; then
  # `sessions list` is newest-first; the id is the last column.
  SID="$(hermes sessions list | awk 'NR>2 && NF {print $NF; exit}')"
fi

if [[ -z "$SID" ]]; then
  echo "could not determine a session id" >&2
  exit 1
fi

OUT="$DEST/$SID.jsonl"
if [[ -e "$OUT" ]]; then
  echo "already exported: $OUT"
  exit 0
fi

hermes sessions export "$OUT" --session-id "$SID" --format jsonl
echo "$OUT"
