#!/usr/bin/env bash
#
# plex-corrupt-scan.sh — find media files that will fail Plex playback with
# `s1001 (Network)` because the FILE is corrupt (full size, but a leading
# zero-block / unreadable header), not because of the mount/network/codec.
#
# Background: a corrupt file makes Plex's Media Decision Engine log
#   MDE: video has neither a video stream nor an audio stream
#   Streaming Resource: Cannot make a decision ... file is unplayable ...
# and the client shows error s1001. Healthy files Direct Play fine, so the
# split looks like "movies broken, TV fine" until you probe the actual file.
#
# Run INSIDE the Plex container (e.g. `pct enter 200`) as root.
#
#   ./plex-corrupt-scan.sh            # zeroed-header scan of the default roots
#   ./plex-corrupt-scan.sh /mnt/movies /mnt/tv /mnt/unas/Media/Music
#   ./plex-corrupt-scan.sh --log      # also list files Plex couldn't decide on
#   ./plex-corrupt-scan.sh --probe    # deep-probe each flagged file with Plex ffmpeg
#
# Two detectors, cheap → thorough:
#   1. zeroed-header scan  — reads the first 64 KiB of every media file and
#      flags any that are entirely NUL (the classic leading zero-block). Fast:
#      a few bytes per file, safe to run against the whole library.
#   2. --log               — greps the PMS log for "Failed to get a decision"
#      (files Plex already choked on; superset — includes transient analyser
#      misses, so cross-check against detector 1 / --probe before deleting).
#   3. --probe             — runs `Plex Transcoder -i` on each flagged file to
#      confirm ffmpeg can't find a stream. Definitive but slower.
#
# After confirming a file is corrupt: re-grab it in Radarr/Sonarr (delete file
# → search) or restore a Synology snapshot, then Analyze the item in Plex.
# If MANY files across different volumes are flagged, suspect the NAS itself —
# check Storage Manager health + S.M.A.R.T. and run a Btrfs data scrub.

set -euo pipefail

# ---- config -----------------------------------------------------------------
DEFAULT_ROOTS=(/mnt/movies /mnt/tv)                       # scanned when no paths given
EXTS=(mkv mp4 avi m4v mov mpg mpeg m2ts ts wmv flv webm)  # media containers to check
HEADER_BYTES=65536                                        # leading bytes tested for all-NUL
PLEX_APP="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server"
PLEX_LOG="$PLEX_APP/Logs/Plex Media Server.log"
TRANSCODER="/usr/lib/plexmediaserver/Plex Transcoder"
CODECS_DIR="$PLEX_APP/Codecs"

# ---- helpers ----------------------------------------------------------------
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[plex-scan] $*"; }

# build the find -iname ( a -o b -o c ) predicate from EXTS
name_pred=(); for e in "${EXTS[@]}"; do name_pred+=(-o -iname "*.$e"); done
name_pred=("${name_pred[@]:1}")   # drop leading -o

# first-16-bytes-all-NUL is the fast pre-filter; a NUL leading block always
# starts with 16 zeros, so this catches every candidate before the fuller read.
is_zeroed() {  # $1 = file; true if the first HEADER_BYTES are entirely NUL
  local f="$1"
  [[ "$(head -c 16 "$f" | tr -d '\0' | wc -c)" -eq 0 ]] || return 1   # cheap reject
  [[ "$(head -c "$HEADER_BYTES" "$f" | tr -d '\0' | wc -c)" -eq 0 ]]
}

# locate the ffmpeg external-libs dir (build hash changes across Plex versions)
find_ffmpeg_libs() {
  local d
  d="$(find "$CODECS_DIR" -maxdepth 1 -type d -name '*-linux-x86_64' \
        ! -name 'EasyAudioEncoder*' 2>/dev/null | head -1)"
  [[ -n "$d" ]] && echo "$d"
}

probe() {  # $1 = file; print a one-line verdict using Plex's own ffmpeg
  local f="$1" libs out
  libs="$(find_ffmpeg_libs)" || true
  [[ -x "$TRANSCODER" ]] || { echo "  (probe skipped: no Plex Transcoder)"; return; }
  out="$(FFMPEG_EXTERNAL_LIBS="${libs:-}" "$TRANSCODER" -i "$f" 2>&1 || true)"
  if grep -qiE 'Invalid data|could not find codec|EBML|Error opening input' <<<"$out"; then
    echo "  -> CORRUPT (ffmpeg: $(grep -iE 'Invalid data|EBML|Error opening' <<<"$out" | head -1 | sed 's/^ *//'))"
  elif grep -qE 'Stream #' <<<"$out"; then
    echo "  -> readable (has streams) — corruption may be elsewhere in the file"
  else
    echo "  -> inconclusive (no stream lines and no error)"
  fi
}

# ---- scan -------------------------------------------------------------------
roots=(); do_log=0; do_probe=0
for a in "$@"; do
  case "$a" in
    --log)   do_log=1 ;;
    --probe) do_probe=1 ;;
    -*)      die "unknown flag: $a (use --log, --probe)" ;;
    *)       roots+=("$a") ;;
  esac
done
[[ ${#roots[@]} -gt 0 ]] || roots=("${DEFAULT_ROOTS[@]}")

for r in "${roots[@]}"; do
  [[ -d "$r" ]] || die "not a directory: $r (is the mount/bind present?)"
  [[ -n "$(ls -A "$r" 2>/dev/null)" ]] || die "$r is empty — refusing to scan an unmounted bind"
done

info "zeroed-header scan (first $((HEADER_BYTES/1024)) KiB all-NUL) under: ${roots[*]}"
found=0
while IFS= read -r -d '' f; do
  if is_zeroed "$f"; then
    found=$((found+1))
    printf 'ZEROED: %s (%s bytes)\n' "$f" "$(stat -c %s "$f")"
    [[ $do_probe -eq 1 ]] && probe "$f"
  fi
done < <(find "${roots[@]}" -type f \( "${name_pred[@]}" \) -print0)
info "zeroed-header hits: $found"

if [[ $do_log -eq 1 ]]; then
  echo
  if [[ -f "$PLEX_LOG" ]]; then
    info "files Plex logged 'Failed to get a decision' for (superset — verify before deleting):"
    grep -a "Failed to get a decision" "$PLEX_LOG" | sed 's/.*decision for: //' | sort -u || true
  else
    info "PMS log not found at: $PLEX_LOG"
  fi
fi

[[ $found -eq 0 ]] && info "no leading-zero-block corruption found in ${roots[*]}"
exit 0
