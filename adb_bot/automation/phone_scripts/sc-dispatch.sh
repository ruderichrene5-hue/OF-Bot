#!/var/jb/bin/sh
# forced command for the Shortcuts app's SSH keys: "close" -> ig-close.sh ; "open <NAME> <HOST>" -> container-open.sh
set -- $SSH_ORIGINAL_COMMAND
case "$1" in
  open)  exec /var/jb/var/mobile/bin/container-open.sh "$2" "$3" ;;
  close|"") exec /var/jb/var/mobile/bin/ig-close.sh ;;
  *) printf 'FAIL unknown command' ;;
esac
