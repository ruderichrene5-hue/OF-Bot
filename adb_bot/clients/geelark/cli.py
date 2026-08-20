"""Command line for the Geelark host -- drive it by hand, nothing scheduled.

    python -m adb_bot.clients.geelark.cli phones      # the account's inventory
    python -m adb_bot.clients.geelark.cli tags        # tags and groups
    python -m adb_bot.clients.geelark.cli proxies     # the proxy book, by exit IP
    python -m adb_bot.clients.geelark.cli apps  ID    # what can be installed here
    python -m adb_bot.clients.geelark.cli connect ID  # start, open ADB, adb connect
    python -m adb_bot.clients.geelark.cli shell ID -- CMD   # one adb shell command
    python -m adb_bot.clients.geelark.cli start ID --apply
    python -m adb_bot.clients.geelark.cli stop  ID --apply
    python -m adb_bot.clients.geelark.cli create --amount 2 --android 5 --apply
    python -m adb_bot.clients.geelark.cli clone ID --amount 2 --apply

Nothing here is wired into a loop or a timer. This is the manual seam: it exists
so a person can bring a Geelark phone up, prove the bot can reach it, and put it
back down again, without any of that becoming scheduled behaviour.

**Anything that changes state or costs money is a dry run unless `--apply` is
passed.** `start`, `stop`, `create`, `clone` and `install` all refuse to act
without it, and print exactly what they would have done instead. `create` and
`clone` make phones, which cost real money; `start` begins billing minutes,
which is why `connect` prints the reminder to stop the phone afterwards.

`connect` is the one that answers "can the bot drive Geelark at all". It starts
the phone if it is stopped, enables ADB (off per phone by default, and only
enableable once the phone is running), waits for the bridge, then runs the same
`adb connect` + `glogin` pair `ADBClient` uses in production -- so a pass here
means the existing flows would work, not merely that the API answered.
"""

from __future__ import annotations

import argparse
import sys

from adb_bot.clients.adb import ADBClient
from adb_bot.clients.geelark.adb_enable import GeelarkAdbEnableClient
from adb_bot.clients.geelark.api import GeelarkApiClient
from adb_bot.clients.geelark.apps import GeelarkAppClient
from adb_bot.clients.geelark.launcher import GeelarkLauncherClient
from adb_bot.clients.geelark.phones import GeelarkPhoneClient, status_label
from adb_bot.clients.geelark.proxies import GeelarkProxyClient
from adb_bot.clients.geelark.readiness import prepare_geelark_profile_for_adb
from adb_bot.clients.geelark.shutdown import GeelarkShutdownClient
from adb_bot.clients.geelark.tags import GeelarkGroupClient, GeelarkTagClient
from adb_bot.clients.geelark.transport import GeelarkError, GeelarkTransport

STARTED = "started"


def _transport() -> GeelarkTransport:
    transport = GeelarkTransport()
    if not transport.is_configured:
        print("No Geelark credentials. Set GEELARK_APP_ID and GEELARK_API_KEY "
              "(/etc/adbbot/env on the server).")
        raise SystemExit(2)
    return transport


def _resolve(transport: GeelarkTransport, wanted: str) -> dict:
    """Find one phone by id or by (case-insensitive) name.

    Names are far easier to type than 18-digit ids, but they are not unique in
    Geelark, so an ambiguous name is refused rather than guessed at.
    """
    rows = GeelarkPhoneClient(transport).list_phones()
    for row in rows:
        if str(row.get("id")) == wanted:
            return row

    matches = [row for row in rows
               if str(row.get("serialName", "")).lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"'{wanted}' matches {len(matches)} phones; use the id:")
        for row in matches:
            print(f"  {row['id']}  {row['serialName']}")
        raise SystemExit(2)

    print(f"No Geelark phone with id or name '{wanted}'.")
    raise SystemExit(2)


def _print_outcome(label: str, outcome) -> bool:
    if outcome.ok:
        print(f"{label}: ok ({outcome.succeeded}/{outcome.total})")
        return True
    print(f"{label}: FAILED ({outcome.succeeded}/{outcome.total} succeeded)")
    for phone_id, reason in outcome.failures().items():
        print(f"  {phone_id}: {reason}")
    return False


def cmd_phones(args) -> int:
    transport = _transport()
    rows = GeelarkPhoneClient(transport).list_phones()
    if not rows:
        print("No cloud phones on this account.")
        return 0

    print(f"{'ID':<20} {'NAME':<32} {'STATUS':<9} {'ANDROID':<12} "
          f"{'COUNTRY':<8} PROXY")
    for row in sorted(rows, key=lambda r: str(r.get("serialName", "")).lower()):
        equipment = row.get("equipmentInfo") or {}
        proxy = row.get("proxy") or {}
        endpoint = (f"{proxy.get('server')}:{proxy.get('port')}"
                    if proxy.get("server") else "-")
        print(f"{row['id']:<20} {str(row.get('serialName'))[:32]:<32} "
              f"{status_label(row.get('status')):<9} "
              f"{str(equipment.get('osVersion') or '-'):<12} "
              f"{str(equipment.get('countryName') or '-'):<8} {endpoint}")
    print(f"\n{len(rows)} phone(s). Started phones bill by the minute.")
    return 0


def cmd_tags(args) -> int:
    transport = _transport()
    print("TAGS:")
    for name, tag_id in sorted(GeelarkTagClient(transport).tag_ids_by_name().items()):
        print(f"  {tag_id:<20} {name}")
    print("\nGROUPS:")
    for group in GeelarkGroupClient(transport).list_groups():
        print(f"  {group.get('id'):<20} {group.get('name')}")
    return 0


def cmd_proxies(args) -> int:
    transport = _transport()
    client = GeelarkProxyClient(transport)
    clusters = client.endpoint_clusters()

    details: dict[int, dict] = {}
    if args.check_ips:
        # Geelark's own detection rather than tunnelling from this host: it is
        # what the phones will actually use, and it answers even when this
        # server cannot reach the endpoint.
        print("asking Geelark where each proxy actually comes out...\n")
        details = client.exit_ips()

    for endpoint, members in sorted(clusters.items()):
        port = int(endpoint.rsplit(":", 1)[1])
        suffix = ""
        if args.check_ips:
            info = details.get(port) or {}
            if info.get("ip"):
                suffix = (f"  exit={info['ip']:<16} {info.get('country') or ''}"
                          f" / {info.get('isp') or ''}")
            else:
                suffix = "  exit=UNREACHABLE"
        print(f"  {endpoint:<28} {len(members)} record(s){suffix}")

    gateways = {endpoint.split(":")[0] for endpoint in clusters}
    print(f"\n{sum(len(v) for v in clusters.values())} proxy record(s) across "
          f"{len(clusters)} endpoint(s) on {len(gateways)} gateway host(s).")

    if args.check_ips:
        live = [d["ip"] for d in details.values() if d.get("ip")]
        distinct = len(set(live))
        print(f"{distinct} distinct exit IP(s) across {len(live)} reachable "
              f"endpoint(s).")
        phones = len(GeelarkPhoneClient(transport).list_phones())
        if distinct:
            print(f"At {phones} phone(s) today that is "
                  f"{phones / distinct:.1f} phone(s) per exit IP.")
    else:
        print("The gateway host is not the exit IP -- pass --check-ips to read "
              "the real ones.")
    return 0


def cmd_rotate(args) -> int:
    from adb_bot.clients.geelark.ip_rotation import (
        ProxyRotationError,
        ProxyRotator,
    )

    transport = _transport()
    rotator = ProxyRotator(GeelarkProxyClient(transport).list_proxies())

    if not rotator.reboot_config:
        print("No rotation URLs configured. Set GEELARK_PROXY_REBOOT_URLS to a "
              "JSON object keyed by SOCKS5 port, e.g.\n"
              '  {"54015": "https://mobile.proxy-seller.com/c/modem/ip/<token>"}')
        return 2

    if args.check_all:
        # NOT a dry run. There is no read-only status endpoint on this vendor,
        # so the only way to test a link is to use it -- this rotates every
        # configured proxy. An earlier version of this claimed otherwise and
        # rotated all four at once by surprise.
        if not args.apply:
            print("DRY RUN: --check-all ROTATES every configured proxy "
                  f"({', '.join(str(p) for p in rotator.rotatable_ports())}) -- "
                  "this vendor has no read-only status URL, so testing a link "
                  "means using it. Re-run with --apply.")
            return 0
        print("rotating every configured proxy to test its link\n")
        for port, result in rotator.probe_all().items():
            state = "OK" if result["ok"] else "REFUSED"
            print(f"  {port}  {state:<8} HTTP {result['status']}  {result['detail']}")
        print("\n200/OK means the link fired. 400 with body ERROR is a cooldown "
              "on a working link.\n400 with an HTML body is the wrong URL "
              "entirely.")
        return 0

    if not args.port:
        print("Give a port to rotate, or --check-all to exercise every link.")
        return 2

    if not args.apply:
        print(f"DRY RUN: would rotate the exit IP of port {args.port}. "
              f"Re-run with --apply.")
        print(f"  current exit IP: {rotator.exit_ip(args.port) or 'unreachable'}")
        return 0

    try:
        result = rotator.rotate_and_verify(args.port)
    except ProxyRotationError as error:
        print(f"{error}")
        return 1

    if not result.get("accepted", True):
        print(f"  the vendor REFUSED the call: {result.get('detail')}")
        print("  body 'ERROR' on this endpoint is a cooldown -- the link works, "
              "it was called again too soon. Wait and retry rather than "
              "treating the link as broken.")
        return 1

    print(f"  before : {result['before']}")
    print(f"  after  : {result['after']}")
    print(f"  changed: {result['changed']} (after {result['seconds']}s)")
    if not result["changed"]:
        print("The vendor accepted the call but the address did not move. A "
              "mobile proxy can hand back the address it just released; treat "
              "an unchanged IP as a real outcome, not a failure to retry blindly.")
    return 0 if result["changed"] else 1


def cmd_apps(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    apps = GeelarkAppClient(transport).list_apps(str(phone["id"]))
    for app in apps:
        installed = "installed" if app.get("installStatus") == 1 else "-"
        print(f"  {str(app.get('appName'))[:28]:<28} "
              f"{str(app.get('packageName'))[:32]:<32} "
              f"{str(app.get('versionName') or ''):<18} {installed}")
    print(f"\n{len(apps)} app(s) available on {phone['serialName']}.")
    return 0


def cmd_install(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    apps = GeelarkAppClient(transport)
    matches = apps.find_app(str(phone["id"]), args.package)
    if not matches:
        print(f"No app with package '{args.package}' available on "
              f"{phone['serialName']}.")
        return 1

    chosen = matches[0]
    if len(matches) > 1:
        print(f"{len(matches)} versions of {args.package}; taking "
              f"{chosen.get('versionName')}. Pass --version-id to choose.")
    version_id = args.version_id or str(chosen.get("appVersionId"))

    if not args.apply:
        print(f"DRY RUN: would install {args.package} "
              f"({chosen.get('versionName')}) on {phone['serialName']}. "
              f"Re-run with --apply.")
        return 0

    apps.install_app(str(phone["id"]), version_id)
    print(f"Install requested for {args.package} on {phone['serialName']}.")
    return 0


def cmd_start(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    if not args.apply:
        print(f"DRY RUN: would start {phone['serialName']} ({phone['id']}), "
              f"which begins billing minutes. Re-run with --apply.")
        return 0
    outcome = GeelarkLauncherClient(transport).start_profiles([str(phone["id"])])
    return 0 if _print_outcome(f"start {phone['serialName']}", outcome) else 1


def cmd_stop(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    if not args.apply:
        print(f"DRY RUN: would stop {phone['serialName']} ({phone['id']}). "
              f"Re-run with --apply.")
        return 0
    outcome = GeelarkShutdownClient(transport).shutdown_profiles([str(phone["id"])])
    return 0 if _print_outcome(f"stop {phone['serialName']}", outcome) else 1


def cmd_billing(args) -> int:
    from adb_bot.clients.geelark.billing import GeelarkBillingClient

    transport = _transport()
    runway = GeelarkBillingClient(transport).runway()
    phones = GeelarkPhoneClient(transport).list_phones()
    running = sum(1 for row in phones
                  if status_label(row.get("status")) == STARTED)

    print(f"plan               {runway['plan']}")
    print(f"profile slots      {runway['profiles_available']} free of "
          f"{runway['profiles']}")
    print(f"parallel slots     {runway['parallels']}")
    print(f"phones running     {running}")
    print(f"balance            ${runway['balance']:.2f}")
    print(f"gift credit        ${runway['gift']:.2f}")
    print(f"bought minutes     {runway['time_addon_minutes']:,}")
    print(f"per-minute runway  ~{runway['minutes_left']:,} min "
          f"(~{runway['minutes_left'] / 60:.0f}h)")

    over = running - runway["parallels"]
    if over > 0:
        print(f"\n{over} phone(s) are running beyond the parallel slots and are "
              f"billing per minute.")
    else:
        print(f"\nWithin the parallel slots -- nothing is billing per minute.")
    print("Parallel slots cover phones started through the API and driven over "
          "ADB. They do NOT cover Geelark's own RPA tasks.")
    return 0


def cmd_create(args) -> int:
    transport = _transport()
    names = args.name
    if not args.apply:
        print(f"DRY RUN: would create {len(names)} phone(s) on "
              f"{args.mobile_type} bound to proxy serial {args.proxy_serial}: "
              f"{', '.join(names)}. This COSTS MONEY. Re-run with --apply.")
        return 0

    data = GeelarkPhoneClient(transport).create_phones(
        names,
        mobile_type=args.mobile_type,
        proxy_serial_no=args.proxy_serial,
        group_name=args.group or None,
        tags=args.tag or None)

    # The envelope says success even when every row failed -- read the details.
    details = data.get("details") or []
    made = [d for d in details if not d.get("code")]
    failed = [d for d in details if d.get("code")]
    for detail in made:
        print(f"  created {detail.get('profileName')} -> {detail.get('id')}")
    for detail in failed:
        print(f"  FAILED  {detail.get('profileName') or '(unnamed)'}: "
              f"{detail.get('code')} {detail.get('msg')}")
    print(f"\n{len(made)} created, {len(failed)} failed.")
    return 0 if made and not failed else 1


def cmd_clone(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    if not args.apply:
        print(f"DRY RUN: would clone {phone['serialName']} {args.amount} time(s). "
              f"This COSTS MONEY. Re-run with --apply.")
        return 0
    data = GeelarkPhoneClient(transport).clone_phone(str(phone["id"]), args.amount)
    print(f"cloned: {data}")
    return 0


class _PrintLogger:
    """Feeds the readiness module's log calls to stdout for the CLI."""

    def _write(self, message, *args):
        print("  " + (message % args if args else message))

    info = warning = error = _write


def _bring_up(transport, phone):
    """Start the phone if needed, open ADB, and return a drivable `Profile`.

    Delegates to `prepare_geelark_profile_for_adb` so the CLI and any flow that
    later drives Geelark share one definition of "ready" -- two copies of this
    sequence would drift, and the ordering rules in it are not obvious.
    """
    name = phone.get("serialName")
    if status_label(phone.get("status")) != STARTED:
        print(f"{name} is {status_label(phone.get('status'))}; starting it "
              f"(this begins billing minutes)...")
    else:
        print(f"{name} is already started.")

    return prepare_geelark_profile_for_adb(
        str(phone["id"]), transport, logger=_PrintLogger())


def _connect_with_retries(adb: ADBClient, profile, attempts: int = 6,
                          wait_seconds: int = 10) -> bool:
    """`adb connect`, retried -- the bridge is not up the instant ADB is.

    Observed on a freshly created phone: Geelark returned a valid ip/port, the
    first connect was refused, and the same endpoint connected ~10s later.
    """
    import time

    for attempt in range(1, attempts + 1):
        if adb.connect(profile):
            return True
        if attempt < attempts:
            print(f"  connect attempt {attempt} failed; retrying in "
                  f"{wait_seconds}s...")
            time.sleep(wait_seconds)
    return False


def cmd_connect(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    profile = _bring_up(transport, phone)
    if profile is None:
        return 1

    print(f"\nADB endpoint: {profile.target}")
    adb = ADBClient()
    if args.no_adb:
        print("Skipping the local adb connect (--no-adb).")
        return 0

    # Geelark hands back the port before the bridge is actually accepting
    # connections -- a freshly created phone refused the first connect and took
    # it ~10s later. One attempt reads as "not reachable from this host", which
    # is the wrong diagnosis entirely, so retry the way the posting path does.
    if not _connect_with_retries(adb, profile):
        print("adb connect failed after retries. The endpoint may not be "
              "reachable from this host.")
        return 1
    print("adb connect ok")

    if not adb.authenticate(profile):
        print("glogin did not confirm. The phone is connected but may reject commands.")
        return 1
    print("glogin ok")

    model = adb.run_command(f"adb -s {profile.target} shell getprop ro.product.model")
    release = adb.run_command(f"adb -s {profile.target} shell getprop ro.build.version.release")
    print(f"\nOn the phone: model={model!r} android={release!r}")
    print(f"\nThe bot can drive this phone. Remember to stop it when done:\n"
          f"  python -m adb_bot.clients.geelark.cli stop {phone['id']} --apply")
    return 0


def cmd_shell(args) -> int:
    transport = _transport()
    phone = _resolve(transport, args.phone)
    profile = _bring_up(transport, phone)
    if profile is None:
        return 1

    adb = ADBClient()
    if not _connect_with_retries(adb, profile) or not adb.authenticate(profile):
        print("Could not reach the phone over ADB.")
        return 1

    command = " ".join(args.command)
    print(adb.run_command(f"adb -s {profile.target} shell {command}") or "")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adb_bot.clients.geelark.cli",
        description="Drive the Geelark cloud-phone host by hand.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("phones", help="list the account's cloud phones").set_defaults(func=cmd_phones)
    subparsers.add_parser("tags", help="list tags and groups").set_defaults(func=cmd_tags)

    proxies = subparsers.add_parser("proxies", help="list proxies and their exit IPs")
    proxies.add_argument("--check-ips", action="store_true",
                         help="make a request through each proxy to read its "
                              "real exit IP, which the proxy record does not carry")
    proxies.set_defaults(func=cmd_proxies)

    rotate = subparsers.add_parser(
        "rotate", help="force a new exit IP on one proxy (vendor-side)")
    rotate.add_argument("port", type=int, nargs="?", default=0,
                        help="the proxy's SOCKS5 port")
    rotate.add_argument("--check-all", action="store_true",
                        help="exercise every configured rotation link. NOT a "
                             "dry run -- this vendor has no read-only status "
                             "URL, so it really does rotate every proxy")
    rotate.add_argument("--apply", action="store_true")
    rotate.set_defaults(func=cmd_rotate)

    apps = subparsers.add_parser("apps", help="apps installable on one phone")
    apps.add_argument("phone")
    apps.set_defaults(func=cmd_apps)

    install = subparsers.add_parser("install", help="install an app on one phone")
    install.add_argument("phone")
    install.add_argument("package")
    install.add_argument("--version-id", default="")
    install.add_argument("--apply", action="store_true")
    install.set_defaults(func=cmd_install)

    start = subparsers.add_parser("start", help="start a phone (bills minutes)")
    start.add_argument("phone")
    start.add_argument("--apply", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", help="stop a phone")
    stop.add_argument("phone")
    stop.add_argument("--apply", action="store_true")
    stop.set_defaults(func=cmd_stop)

    subparsers.add_parser(
        "billing", help="plan, parallel slots and remaining runway"
    ).set_defaults(func=cmd_billing)

    create = subparsers.add_parser("create", help="create new phones (costs money)")
    create.add_argument("--name", action="append", required=True,
                        help="name for one new phone; repeat per phone")
    create.add_argument("--mobile-type", default="Android 13",
                        help='e.g. "Android 13" -- the name, not a number')
    create.add_argument("--proxy-serial", type=int, required=True,
                        help="serialNo of a proxy already saved on the account "
                             "(see `proxies`); creation fails without one")
    create.add_argument("--group", default="")
    create.add_argument("--tag", action="append")
    create.add_argument("--apply", action="store_true")
    create.set_defaults(func=cmd_create)

    clone = subparsers.add_parser("clone", help="clone a phone (costs money)")
    clone.add_argument("phone")
    clone.add_argument("--amount", type=int, default=1)
    clone.add_argument("--apply", action="store_true")
    clone.set_defaults(func=cmd_clone)

    connect = subparsers.add_parser(
        "connect", help="start, open ADB, and connect from this host")
    connect.add_argument("phone")
    connect.add_argument("--no-adb", action="store_true",
                         help="get the endpoint but do not run adb locally")
    connect.set_defaults(func=cmd_connect)

    shell = subparsers.add_parser("shell", help="run one adb shell command")
    shell.add_argument("phone")
    shell.add_argument("command", nargs=argparse.REMAINDER)
    shell.set_defaults(func=cmd_shell)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GeelarkError as error:
        print(f"Geelark refused the call: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
