#!/var/jb/bin/sh
# iPhone 7 / iOS 15 (G): one script does what the container Shortcut does on iOS 16 -- called from the
# Shortcut's "Run Script over SSH" step through sc-dispatch.sh as `open <NAME> <HOST>`. No App Intents
# (they are no-ops on iOS 15) and no shadowrocket:// URL schemes (each would pop an "Open in Shadowrocket?"
# sheet that nobody can answer here): VPN off/on goes through XXTouch's device API, with a tap on
# Shadowrocket's own switch as fallback. Prints one line: OK ip=.. dns=..  or  FAIL <why>.
export PATH=/var/jb/usr/local/bin:/var/jb/usr/bin:/var/jb/bin:/usr/bin:/bin
NAME=$1; HOST=$2; LOG=/var/jb/var/mobile/container-open.log
log() { echo "$(date +%FT%T) $NAME $*" >> "$LOG"; }
xx() { curl -s -m 25 -X POST --data-binary "$1" http://127.0.0.1:46952/spawn >/dev/null 2>&1; }
xxq() { rm -f /var/mobile/xxt_co.txt; xx "local f=io.open('/var/mobile/xxt_co.txt','w') f:write(tostring($1)) f:close()"; sleep 1; cat /var/mobile/xxt_co.txt 2>/dev/null; }
case "$NAME" in [A-Z]*[0-9]) ;; *) printf 'FAIL bad name'; exit 0;; esac
[ -n "$HOST" ] || { printf 'FAIL no host'; exit 0; }
r=$(/var/jb/var/mobile/bin/ig-close.sh); [ "$r" = OK ] || { log "close: $r"; printf 'FAIL close %s' "$r"; exit 0; }
xx "device.turn_off_vpn()"; sleep 2
cranectl --switch com.liguangming.Shadowrocket "name:$NAME Proxy" >/dev/null 2>&1 || { log "switch failed"; printf 'FAIL switch'; exit 0; }
sleep 1
xx "device.turn_on_vpn()"; sleep 3
if [ "$(xxq 'device.is_vpn_on()')" != "true" ]; then
  log "turn_on_vpn did not connect -> tap the switch"
  uiopen --bundleid com.liguangming.Shadowrocket; sleep 2.5
  xx "touch.tap(634,212)"; sleep 3
fi
ip=""; n=0
while [ $n -lt 45 ]; do
  ip=$(curl -s -m 5 -4 'http://ip-api.com/line/?fields=query' 2>/dev/null)
  [ "$ip" = "$HOST" ] && break
  n=$((n+1)); [ $n -eq 20 ] && { uiopen --bundleid com.liguangming.Shadowrocket; sleep 2; xx "if not device.is_vpn_on() then touch.tap(634,212) end"; }
  sleep 1
done
if [ "$ip" != "$HOST" ]; then log "ip gate failed: $ip"; printf 'FAIL proxy %s not active (ip=%s)' "$HOST" "$ip"; exit 0; fi
cc=$(curl -s -m 8 -4 'http://ip-api.com/line/?fields=countryCode' 2>/dev/null | tr -d '\r\n')
dns=$(curl -s -L -m 12 -4 'http://edns.ip-api.com/json' 2>/dev/null | tr -d '\n ' | grep -o '"geo":"[^"]*' | cut -d'"' -f4)
case "$dns" in UnitedStates*|"United States"*) ;; *) log "dns gate failed: $dns"; printf 'FAIL DNS not US (%s)' "$dns"; exit 0;; esac
[ "$cc" = "US" ] || { log "country gate failed: $cc"; printf 'FAIL country %s' "$cc"; exit 0; }
cranectl --switch com.burbn.instagram "name:$NAME" >/dev/null 2>&1; sleep 2
uiopen --bundleid com.burbn.instagram
log "OK ip=$ip dns=$dns"
printf 'OK ip=%s dns=%s' "$ip" "$dns"
