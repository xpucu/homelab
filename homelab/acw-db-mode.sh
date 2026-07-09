#!/usr/bin/env bash
#
# acw-db-mode.sh — flip Autocaliweb's metadata.db between two modes:
#
#   local  (normal): real DB on the container's local disk, symlinked from the
#                     NFS library root. NFS-safe. ACW running.
#   share  (edit):   real DB placed on the NFS library root (no symlink) so
#                     Calibre desktop can open the library over SMB and bulk-edit.
#                     ACW STOPPED for the whole session.
#
# Run INSIDE the ACW container (e.g. `pct enter 112`) as root:
#   ./acw-db-mode.sh status   # show current mode + service state
#   ./acw-db-mode.sh edit     # -> share mode (stop ACW, DB onto the share)
#   ./acw-db-mode.sh done     # -> local mode (DB back local, symlink, start ACW)
#
# ⚠️  ONE writer at a time. In share mode ACW stays STOPPED. Do NOT start ACW
#     while Calibre desktop has the library open — two writers on one metadata.db
#     corrupts it. Close Calibre before running `done`.

set -euo pipefail

# ---- config -----------------------------------------------------------------
LIB="/mnt/unas/Media/Books/Calibre"          # Calibre library root on the NFS share
LOCAL_DIR="/var/lib/autocaliweb-db"          # local (container-disk) home for the DB
SERVICE="autocaliweb"                        # systemd unit name
OWNER="acw:acw"                              # ACW service user:group
BACKUP_DIR="/mnt/unas/NAS_Backup/acw-db-backups"
KEEP=5                                       # timestamped backups to retain

DB_SHARE="$LIB/metadata.db"
DB_LOCAL="$LOCAL_DIR/metadata.db"
SIDECARS=(metadata.db-wal metadata.db-shm metadata.db-journal)

# ---- helpers ----------------------------------------------------------------
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[acw-db] $*"; }

check_env() {
  [[ $EUID -eq 0 ]] || die "run as root (inside the ACW container)"
  [[ -d "$LIB" ]]   || die "library path not found: $LIB (is the UNAS mounted/bound?)"
  # guard against operating on an empty/unmounted bind: expect the library to have content
  [[ -n "$(ls -A "$LIB" 2>/dev/null)" ]] || die "$LIB is empty — refusing to touch it"
}

current_mode() {
  if   [[ -L "$DB_SHARE" ]];                     then echo local        # symlink present
  elif [[ -f "$DB_SHARE" ]];                     then echo share        # real DB on the share
  elif [[ -f "$DB_LOCAL" && ! -e "$DB_SHARE" ]]; then echo local-nolink # local DB, symlink missing
  else echo unknown
  fi
}

svc_stop() {
  if systemctl is-active --quiet "$SERVICE"; then
    info "stopping $SERVICE"
    systemctl stop "$SERVICE"
  fi
  ! systemctl is-active --quiet "$SERVICE" || die "$SERVICE still running after stop"
}

svc_start() {
  info "starting $SERVICE"
  systemctl start "$SERVICE"
}

backup() {  # $1 = db file to snapshot
  local src="$1" ts
  [[ -f "$src" ]] || return 0
  mkdir -p "$BACKUP_DIR"
  ts="$(date +%Y%m%d-%H%M%S)"
  info "backup: $src -> $BACKUP_DIR/metadata.$ts.db"
  # NB: destination is the squashed UNAS NFS export — ownership can't be set there,
  # so DON'T use `cp -a`/`-p` (their chown fails → non-zero → set -e aborts mid-flip).
  # Copy bytes + mtime only; ownership is irrelevant for a backup file.
  cp --preserve=timestamps "$src" "$BACKUP_DIR/metadata.$ts.db"
  ls -1t "$BACKUP_DIR"/metadata.*.db 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -f
}

move_sidecars() {  # move WAL/SHM/journal from dir $1 -> dir $2 (if any linger)
  local from="$1" to="$2" s
  for s in "${SIDECARS[@]}"; do
    if [[ -f "$from/$s" ]]; then info "moving stray $s"; mv -f "$from/$s" "$to/"; fi
  done
}

# ---- operations -------------------------------------------------------------
to_share() {  # local -> share (unlock for Calibre desktop editing)
  local mode; mode="$(current_mode)"
  case "$mode" in
    share)
      info "already in SHARE (edit) mode"; svc_stop
      info "DB is on the share: $DB_SHARE — ready for Calibre desktop." ; return 0 ;;
    local|local-nolink) ;;
    *) die "unexpected state '$mode' — inspect $DB_SHARE / $DB_LOCAL manually" ;;
  esac

  svc_stop
  backup "$DB_LOCAL"
  [[ -L "$DB_SHARE" ]] && { info "removing symlink $DB_SHARE"; rm -f "$DB_SHARE"; }
  info "moving DB local -> share"
  mv -f "$DB_LOCAL" "$DB_SHARE"
  move_sidecars "$LOCAL_DIR" "$LIB"

  echo
  info "SHARE mode ready. ACW is STOPPED."
  info "Open the library in Calibre desktop over SMB (\\\\<unas>\\Shared_Drive\\Media\\Books\\Calibre) and edit."
  info "⚠️  Do NOT start ACW while Calibre has it open."
  info "When finished: close Calibre, then run:  $0 done"
}

to_local() {  # share -> local (restore normal, NFS-safe operation)
  local mode; mode="$(current_mode)"
  svc_stop  # never move while the service could be writing

  case "$mode" in
    share)
      backup "$DB_SHARE"
      mkdir -p "$LOCAL_DIR"
      info "moving DB share -> local"
      mv -f "$DB_SHARE" "$DB_LOCAL"
      move_sidecars "$LIB" "$LOCAL_DIR"
      ;;
    local)
      info "already local (symlink present) — refreshing ownership + starting" ;;
    local-nolink)
      info "DB is local but symlink missing — will recreate it" ;;
    *) die "unexpected state '$mode' — inspect $DB_SHARE / $DB_LOCAL manually" ;;
  esac

  [[ -f "$DB_LOCAL" ]] || die "local DB missing at $DB_LOCAL — aborting before symlink"
  if [[ ! -L "$DB_SHARE" ]]; then
    [[ -e "$DB_SHARE" ]] && die "$DB_SHARE exists and is not a symlink — refusing to clobber"
    info "creating symlink $DB_SHARE -> $DB_LOCAL"
    ln -s "$DB_LOCAL" "$DB_SHARE"
  fi
  chown -R "$OWNER" "$LOCAL_DIR"
  # note: chown -h on the NFS symlink is not permitted under squash — intentionally skipped
  svc_start
  info "LOCAL mode restored; ACW running."
}

show_status() {
  local mode; mode="$(current_mode)"
  echo "mode:     $mode"
  echo "service:  $(systemctl is-active "$SERVICE" 2>/dev/null || echo inactive)"
  echo "share db: $(ls -ld "$DB_SHARE" 2>/dev/null || echo 'missing')"
  echo "local db: $(ls -l  "$DB_LOCAL" 2>/dev/null || echo 'missing')"
}

# ---- dispatch ---------------------------------------------------------------
check_env
case "${1:-}" in
  edit)   to_share ;;
  done)   to_local ;;
  status) show_status ;;
  *) echo "usage: $0 {edit|done|status}"; exit 2 ;;
esac