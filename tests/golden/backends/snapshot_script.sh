umask 077
__indus_snap_tmp=$(mktemp '/tmp/indus-snap-golden.sh.tmp.XXXXXXXXXX') || exit 1
{ ( unset ${!INDUS_SESSION_*} ${!INDUS_UI_SESSION_*} ${!INDUS_CRON_*} ${!INDUS_RPC_*} ${!INDUS_DELEGATE_*} INDUS_HOME AI_AGENT INDUS_AGENT OTHER_PASS PROJECT_FLAG 2>/dev/null; export -p ) || true; } > "$__indus_snap_tmp"
for __indus_fn in $(compgen -A function); do
  case "$__indus_fn" in _[!_]*) continue ;; esac
  declare -f "$__indus_fn"
done >> "$__indus_snap_tmp" 2>/dev/null
alias -p >> "$__indus_snap_tmp"
printf '%s\n' 'shopt -s expand_aliases' 'set +e' 'set +u' >> "$__indus_snap_tmp"
mv -f "$__indus_snap_tmp" '/tmp/indus-snap-golden.sh' || rm -f "$__indus_snap_tmp"
builtin cd -- /vercel/sandbox 2>/dev/null || true
printf '\n%s%s%s\n' '__INDUS_CWD_golden__' "$(pwd -P)" '__INDUS_CWD_golden__'
