"""Discover which Instagram accounts each two-account phone actually holds.

MultiLogin's tag says a phone has two accounts; it does not say which. The
`remark` usually names one, but it is hand-typed and we found it stale on the
first phone checked (Jasmin 5's remark says @jasjasmin00; the phone is signed
into `jasmindiecoolee` and `naughty_jasminn`). Posting to a handle we guessed
wrong is worse than not posting, so the handles come from the phone: this pass
launches each tagged profile, opens Instagram's account switcher, reads what is
listed, and writes it to Airtable.

    MLX tags  ->  [this]  ->  Profiles (Cloning).Primary/Second IG Handle
                              ->  queue_runner makes two slots per phone
                              ->  the reel flow switches before posting

It is slow (a phone launch each, a couple of minutes) and it is meant to be:
run it once after tagging phones in MultiLogin, and again when the tags change.
Nothing here posts, uploads, or taps anything but the account switcher.

`Has Second Account` is written from the *observation*, not the tag: a profile
MLX tags but that only has one account logged in is recorded as single-account,
so it stops producing a second queue slot that could only fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from adb_bot.automation import second_accounts
from adb_bot.automation.flows import instagram_accounts
from adb_bot.clients.adb import ADBClient

# Instagram takes a while to render its first frame on a cold cloud phone.
IG_LAUNCH_SETTLE_SECONDS = 12
IG_FOREGROUND_ATTEMPTS = 5

# uiautomator2's first connect pushes its server jar and is flaky on these cloud
# phones; see the retry loop in read_phone_accounts for why this is not one shot.
U2_CONNECT_ATTEMPTS = 4
U2_CONNECT_RETRY_SECONDS = 5


@dataclass
class ProfileAccounts:
    """What one phone turned out to be holding."""

    serial_no: str
    launch_id: str
    name: str
    record_id: str | None = None
    primary: str = ""
    second: str = ""
    all_handles: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.primary)

    @property
    def has_second(self) -> bool:
        return bool(self.second)


@dataclass
class SyncReport:
    checked: list = field(default_factory=list)     # [ProfileAccounts] -- phones read
    to_check: list = field(default_factory=list)    # names a dry run would read
    updated: list = field(default_factory=list)     # names written to Airtable
    unchanged: list = field(default_factory=list)
    failed: list = field(default_factory=list)      # [(name, error)]
    skipped: list = field(default_factory=list)     # [(name, reason)]
    untagged_hints: list = field(default_factory=list)   # tagging gaps in MLX
    dry_run: bool = True

    def summary(self) -> str:
        if self.dry_run:
            # A dry run reads no phone, so it has nothing to say about handles.
            # Reporting these as "updated" would claim work that never happened.
            return (f"[DRY-RUN] would check={len(self.to_check)} "
                    f"skipped={len(self.skipped)} "
                    f"untagged-but-hinted={len(self.untagged_hints)}")
        two = sum(1 for p in self.checked if p.has_second)
        return (f"[APPLIED] checked={len(self.checked)} with-second-account={two} "
                f"updated={len(self.updated)} unchanged={len(self.unchanged)} "
                f"failed={len(self.failed)} skipped={len(self.skipped)}")


def plan_write(observed: ProfileAccounts, existing: dict | None) -> dict | None:
    """The Airtable patch for one phone, or None when nothing changed.

    Pure, so the "don't rewrite a row that already says this" logic is testable
    without a phone or a base. `existing` is one entry from
    ``AirtableClient.second_account_profiles()``.
    """
    if not observed.ok:
        return None
    existing = existing or {}
    same = (
        instagram_accounts.normalize_handle(existing.get("primary")) == observed.primary
        and instagram_accounts.normalize_handle(existing.get("second")) == observed.second
        and bool(existing.get("has_second")) == observed.has_second
    )
    if same:
        return None
    return {
        "primary": observed.primary,
        "second": observed.second,
        "has_second": observed.has_second,
    }


def read_phone_accounts(launch_id: str, name: str, serial_no: str, *,
                        api_client, adb_enable_client, launcher_client, shutdown_client,
                        logger, readiness_max_attempts: int = 8,
                        readiness_wait_seconds: int = 15,
                        connect_max_attempts: int = 5) -> ProfileAccounts:
    """Launch one phone, read its account switcher, shut it down again.

    The phone is closed on every exit path -- an abandoned cloud phone costs
    money and holds a concurrency slot -- which is why the whole body is in a
    try/finally rather than a happy-path return.
    """
    # Imported here so this module stays importable (and unit-testable) on a box
    # without uiautomator2 or a running MLX agent.
    from adb_bot.automation.workflow import connect_with_retries, prepare_profile_for_adb

    out = ProfileAccounts(serial_no=serial_no, launch_id=launch_id, name=name)
    started = False
    try:
        logger.info("second-accounts: launching %s (%s)", name, launch_id)
        launcher_client.start_profiles([launch_id])
        started = True

        profile = prepare_profile_for_adb(
            launch_id, api_client, adb_enable_client, logger,
            max_attempts=readiness_max_attempts, wait_seconds=readiness_wait_seconds,
            launcher_client=launcher_client,
        )
        if profile is None:
            out.error = "phone never became ADB-ready"
            return out

        adb_client = ADBClient()
        target = connect_with_retries(adb_client, profile, logger, launch_id,
                                      max_attempts=connect_max_attempts)
        if not target:
            out.error = "ADB never connected"
            return out

        try:
            import uiautomator2 as u2
        except ImportError:
            out.error = "uiautomator2 is not installed in this interpreter"
            return out

        for command in (
            f"adb -s {target} shell monkey -p {instagram_accounts.IG_PACKAGE} "
            f"-c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n {instagram_accounts.IG_PACKAGE}/.activity.MainTabActivity",
        ):
            adb_client.run_command(command)
            time.sleep(IG_LAUNCH_SETTLE_SECONDS / 2)

        # uiautomator2's first connect pushes its server jar over the freshly
        # opened adb tunnel, and on these cloud phones that push often fails
        # (a truncated STAT reply) even though `adb get-state` already says
        # "device". Retrying gets it on the second or third go; without this a
        # perfectly healthy phone is written off after a two-minute launch.
        device = None
        last_error = ""
        for attempt in range(1, U2_CONNECT_ATTEMPTS + 1):
            try:
                device = u2.connect(target)
                device.implicitly_wait(12)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.info("second-accounts: uiautomator2 connect attempt %d/%d for %s "
                            "failed (%s)", attempt, U2_CONNECT_ATTEMPTS, name, last_error)
                device = None
                time.sleep(U2_CONNECT_RETRY_SECONDS * attempt)
        if device is None:
            out.error = f"uiautomator2 could not connect: {last_error}"
            return out

        # `monkey` sometimes reports success while the launcher is still up.
        for _ in range(IG_FOREGROUND_ATTEMPTS):
            try:
                current = (device.app_current() or {}).get("package")
            except Exception:
                current = None
            if current == instagram_accounts.IG_PACKAGE:
                break
            adb_client.run_command(
                f"adb -s {target} shell am start -n "
                f"{instagram_accounts.IG_PACKAGE}/.activity.MainTabActivity")
            time.sleep(IG_LAUNCH_SETTLE_SECONDS / 2)
        else:
            out.error = "Instagram never reached the foreground"
            return out

        found = instagram_accounts.discover_accounts(device, logger=logger)
        active = instagram_accounts.normalize_handle(found.get("active"))
        handles = [instagram_accounts.normalize_handle(h) for h in found.get("accounts") or []]
        handles = [h for h in handles if h]
        out.all_handles = handles

        if not active and not handles:
            out.error = "could not read any account off the phone"
            return out

        # The account the phone sits on is the primary; the other one is the
        # second. Falling back to the switcher's own order keeps a phone whose
        # header was unreadable usable.
        out.primary = active or (handles[0] if handles else "")
        others = [h for h in handles if h != out.primary]
        out.second = others[0] if others else ""
        if len(others) > 1:
            logger.warning("second-accounts: %s has %d accounts (%s); only the first "
                           "extra one is used", name, len(handles), ", ".join(handles))
        return out
    except Exception as exc:  # pragma: no cover - defensive around device work
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if started:
            try:
                shutdown_client.shutdown_profiles([launch_id])
            except Exception as exc:
                logger.warning("second-accounts: could not shut down %s: %s", name, exc)


def run_second_account_sync(airtable, mlx_items, *, api_client, adb_enable_client,
                            launcher_client, shutdown_client, logger,
                            dry_run: bool = True, only_serials=None,
                            limit: int | None = None,
                            recheck_known: bool = False) -> SyncReport:
    """Read every tagged phone's accounts and record them in Airtable.

    `mlx_items` is the raw MLX profile list. `only_serials` restricts the run to
    named MLX serials (for a one-phone test); `limit` caps how many phones are
    launched, because each one costs a couple of minutes. `recheck_known` re-reads
    phones whose handles Airtable already holds -- off by default so a repeat run
    only picks up the phones still missing.

    A dry run launches nothing: like every other loop here it prints the work it
    would do and leaves the phones alone. That matters more than usual for this
    one, because "read twenty phones" is twenty launches and the better part of
    an hour -- not something anyone should trigger by leaving off `--apply`.
    """
    report = SyncReport(dry_run=dry_run)

    scan = second_accounts.scan_profiles(mlx_items)
    report.untagged_hints = list(scan.untagged_hints)
    logger.info("second-accounts: MLX tag scan -- %s", scan.summary())
    for name, remark in scan.untagged_hints:
        logger.warning("second-accounts: %s has no second-account tag but its remark "
                       "mentions one (%s) -- tag it in MultiLogin to double its posts",
                       name, remark.replace("\n", " / ")[:120])

    known = airtable.second_account_profiles()

    launched = 0
    for tagged in scan.tagged:
        if only_serials and tagged.serial_no not in set(only_serials):
            continue
        existing = known.get(tagged.serial_no)
        if existing is None:
            report.skipped.append((tagged.name, "no Profiles (Cloning) row for this MLX serial"))
            continue
        if not recheck_known and existing.get("primary") and existing.get("second"):
            report.skipped.append((tagged.name, "handles already recorded (use --recheck to re-read)"))
            continue
        if limit is not None and launched >= limit:
            report.skipped.append((tagged.name, f"run cap of {limit} phone(s) reached"))
            continue

        launched += 1
        if dry_run:
            # No launch: report the phone this run would read and move on.
            logger.info("[DRY-RUN] would launch %s (%s) and read its account switcher"
                        " -- MLX remark hints at %s",
                        tagged.name, tagged.launch_id,
                        ", ".join(tagged.remark_handles) or "nothing")
            report.to_check.append(tagged.name)
            continue

        observed = read_phone_accounts(
            tagged.launch_id, tagged.name, tagged.serial_no,
            api_client=api_client, adb_enable_client=adb_enable_client,
            launcher_client=launcher_client, shutdown_client=shutdown_client,
            logger=logger,
        )
        observed.record_id = existing.get("record_id")
        report.checked.append(observed)

        if not observed.ok:
            logger.warning("second-accounts: %s -- %s", tagged.name, observed.error)
            report.failed.append((tagged.name, observed.error))
            continue

        logger.info("second-accounts: %s is signed in as %r, second account %r "
                    "(MLX remark claimed %s)",
                    tagged.name, observed.primary, observed.second or "<none>",
                    ", ".join(tagged.remark_handles) or "nothing")

        patch = plan_write(observed, existing)
        if patch is None:
            report.unchanged.append(tagged.name)
            continue

        note = (f"switcher showed {', '.join(observed.all_handles) or 'nothing'}"
                f" (tag {tagged.tag!r})")
        if airtable.record_profile_accounts(observed.record_id, patch["primary"],
                                            patch["second"], note=note):
            report.updated.append(tagged.name)
        else:
            report.failed.append((tagged.name, "Airtable write failed"))

    logger.info("second-accounts: %s", report.summary())
    return report
