# New farm iPhone: checklist (state 2026-10-14)

Order matters. Steps 1-4 are by hand on the phone, the rest runs from the server.
Test phone for anything new: F (iphone9, no accounts).

## 1. Jailbreak + base tweaks (by hand, Sileo)
- palera1n rootless (iOS 15/16). Home of `mobile` is `/var/jb/var/mobile` on rootless.
- OpenSSH (palera1n bootstrap), set the `mobile` password in Sileo.
- Crane (Havoc, paid) -- containers; `cranectl` ships with it.
- XXTouch Elite (Havoc, licence) -- posting/warm-up inside Instagram. Its licence host
  must be routed DIRECT in Shadowrocket (the container tool writes that rule).
- CatVNC -- input only; keyboard over VNC is unusable, use on-screen taps.
- ChargeLimiter -- then `/var/root/aldente.conf` 80/40 edge_trigger, 35 C stop.
- Known risk: F lost its jailbreak once after Crane + ChargeLimiter (SpringBoard
  SIGABRT). Install one tweak, respring, check, next.

## 2. Apps (App Store, by hand)
- Instagram
- Shadowrocket (paid). Global Routing = Proxy in its own UI; file writes leak DNS.
- NewTerm only if Rene wants a terminal on the phone (paste does not work there).
- Shortcuts: no longer needed since the cranectl path (14.10).

## 3. Region, time zone, name (Settings, by hand)
- Time zone + region of the proxy country (US). `check` flags Asia/Bangkok.
- Language en-US.

## 4. SSH key
- `~/.ssh/authorized_keys` under the rootless home. Without typing on the phone:
  XXTouch `/spawn` over USB iproxy, or `sh farm/f.sh` on the Mac mini.

## 5. Server side
```bash
.venv/bin/python -m adb_bot.automation.iphone_farm_add --name "<iPhone UID>" --ip <lan ip> --vnc-password <pw>
```
- `/etc/adbbot/iphone_farm.json`: model / screen_px / scale, `ssh` alias, `wda_port`.
- `~/.ssh/config` Host alias with ProxyCommand via the Mac mini, `IdentitiesOnly yes`.
- `adbbot-vnc-tunnel.service`: `-L` lines for VNC 1590N and WDA 810N.
- Airtable: Device row; Containers rows with proxies (Rene enters proxy logins).
```bash
.venv/bin/python -m adb_bot.automation.iphone_farm_wda prepare-phone iphoneN   # AppSync
.venv/bin/python -m adb_bot.automation.iphone_farm_wda setup                    # WDA on the Mac
.venv/bin/python -m adb_bot.automation.iphone_farm_containers apply iphoneN
```

## 6. Tweak stack (server, repeatable)
```bash
.venv/bin/python -m adb_bot.automation.iphone_farm_tweaks check iphoneN
.venv/bin/python -m adb_bot.automation.iphone_farm_tweaks install iphoneN   # sudo password prompt
.venv/bin/python -m adb_bot.automation.iphone_farm_tweaks verify iphoneN
```
`install` adds the jjolano + udevs apt sources, `curl`, LocSim, RootBridge 1.0.2,
HookKit 2.5.1, Shadow 3.7.6 (debs cached in `/var/lib/adbbot/iphone_debs`),
regenerates Shadow's ruleset (`shdw -g`) and switches Shadow on for Instagram only
(essential + recommended hooks, as proven on F). `verify` launches Instagram over WDA
and checks it is still alive 25 s later without a crash log.

## Not covered yet (from the 13.10 research)
- Device identity per container (IDFV, Meta device key in the keychain) is shared by
  all Crane containers on a phone. Shadow hides the jailbreak, it does not spoof this.
  Options: own tweak (hook identifierForVendor + MobileGestalt keyed by Crane
  container id) or DORITOS (200 $, declined for now).
- A jailbreak-detection test app as the strong check for Shadow (AppSync is on the
  phones, so an IPA can be installed).
- LocSim: the per-launch "GPS to exit-IP city" step was removed from the container
  opener on 14.10 (commit 065fb00); LocSim stays installed, nothing calls it.
