"""Go and look at the phones parked as `Human Verification Required`.

A profile flagged this way stops posting completely and stays stopped until a
person opens MultiLogin, launches the phone and reads the screen. That is the
right design when the flag is right. The problem is that nothing ever re-asked
the question, so a flag raised by a misfire was indistinguishable from a real
checkpoint and cost the same: an account off the air until somebody happened to
work through the list by hand. On 2026-08-11, 17 of 29 flagged profiles carried
this reason, some of them for six days.

Misfires were not hypothetical. `account_flag_u2` classified the *raw* XML
hierarchy, so any marker phrase appearing in a resource-id or class name
anywhere in the tree flagged the account, and two of the markers were short
enough ("we detected", "we suspect") to be caught by ordinary Instagram copy.
Both are fixed in `ban_detection`, which stops new false flags -- this pass is
what clears the ones already on the board.

**Positive evidence, or nothing.** The verdict is not "no checkpoint markers on
screen". A screen can be blank because Instagram never opened, because the phone
was still booting, or because the dump failed, and every one of those looks like
"no markers" to a substring search. So `looks_clear` requires the account's post
count to have been read, which can only happen on a rendered profile page --
proof the app opened, the account is signed in, and nothing is blocking the UI.
Anything less is `unknown`, and `unknown` leaves the flag alone.

That asymmetry is deliberate and matches the rest of the bot: leaving a working
account parked costs posts, but un-parking a checkpointed one sends the flow
back into a screen it cannot pass, which burns queue rows and tells Instagram
something is automating the account. The cheap mistake is the one to prefer.

`decide_verification` is pure and takes the already-read facts, so the whole
ruling is unit tested without MultiLogin, a phone, or Instagram.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adb_bot.automation import ban_detection
from adb_bot.clients import airtable as at

VERDICT_STILL_BLOCKED = "still_blocked"  # a checkpoint is on the phone; flag was right
VERDICT_LOOKS_CLEAR = "looks_clear"      # proven usable; the flag can come off
VERDICT_WRONG_REASON = "wrong_reason"    # blocked, but not by a checkpoint
VERDICT_UNKNOWN = "unknown"              # could not tell; leave it alone


@dataclass
class ProbeResult:
    """What one look at a phone found.

    `screen_kind` is a `ban_detection` kind or None. `post_count` is the number
    read off the profile header, or None when it could not be read -- which is
    the only positive proof this pass accepts that the account is usable.
    """
    reachable: bool = False
    screen_kind: str | None = None
    post_count: int | None = None
    detail: str = ""


@dataclass
class AuditedProfile:
    record_id: str
    name: str
    verdict: str
    detail: str
    launch_id: str = ""
    flagged_at: str = ""
    status: str = ""
    cleared: bool = False


@dataclass
class AuditReport:
    audited: list = field(default_factory=list)   # AuditedProfile
    cleared: int = 0
    errors: list = field(default_factory=list)    # (name, message)
    dry_run: bool = True

    def counts(self) -> dict:
        out = {v: 0 for v in (VERDICT_STILL_BLOCKED, VERDICT_LOOKS_CLEAR,
                              VERDICT_WRONG_REASON, VERDICT_UNKNOWN)}
        for entry in self.audited:
            out[entry.verdict] = out.get(entry.verdict, 0) + 1
        return out

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        c = self.counts()
        return (f"[{mode}] checked={len(self.audited)} "
                f"still_blocked={c[VERDICT_STILL_BLOCKED]} "
                f"looks_clear={c[VERDICT_LOOKS_CLEAR]} "
                f"wrong_reason={c[VERDICT_WRONG_REASON]} "
                f"unknown={c[VERDICT_UNKNOWN]} cleared={self.cleared} "
                f"errors={len(self.errors)}")


def decide_verification(probe: ProbeResult) -> tuple:
    """Rule on one flagged profile. Returns (verdict, detail).

    Order matters. The block screen is read first because a phone showing a
    checkpoint can still have a readable post count behind it on some builds,
    and "there is a checkpoint" outranks "the header rendered".
    """
    if not probe.reachable:
        return (VERDICT_UNKNOWN,
                probe.detail or "could not open Instagram on this phone; flag left as it is")

    if probe.screen_kind == ban_detection.KIND_HUMAN_VERIFICATION:
        return (VERDICT_STILL_BLOCKED,
                "checkpoint still on screen -- this one really does need a person")

    if probe.screen_kind == ban_detection.KIND_BANNED:
        # Worse than a checkpoint and not fixable by tapping through it. Says so
        # rather than clearing: the flag is wrong, but in the other direction.
        return (VERDICT_WRONG_REASON,
                "the account reads as suspended/disabled, not as a checkpoint -- "
                f"this belongs under {at.PROFILE_ISSUE_BANNED}")

    if probe.screen_kind == ban_detection.KIND_ACTION_BLOCK:
        # A throttle clears itself; a person tapping through it changes nothing.
        return (VERDICT_WRONG_REASON,
                "a temporary action block is on screen, not a checkpoint -- "
                "this clears on its own and needs nobody")

    if probe.post_count is None:
        # No checkpoint found, but nothing proved the app is usable either.
        return (VERDICT_UNKNOWN,
                probe.detail or ("no checkpoint on screen, but the post count could not be "
                                 "read either -- nothing here proves the account works"))

    return (VERDICT_LOOKS_CLEAR,
            f"no checkpoint on screen and the profile rendered ({probe.post_count} posts) "
            "-- nothing is blocking this account")


def audit_verification_flags(airtable, probe, logger=None, dry_run: bool = True,
                             limit: int | None = None) -> AuditReport:
    """Look at every profile parked as `Human Verification Required`.

    `probe(profile) -> ProbeResult` is injected: it is the only half that needs
    MultiLogin and a phone, which keeps the ruling above testable and lets the
    caller decide how a phone is opened and how many may be open at once.

    `dry_run` writes nothing at all -- not even a note -- and reports exactly
    what an --apply run would do. `limit` caps how many phones one pass opens.
    """
    def log(level, message, *args):
        if logger is not None:
            getattr(logger, level, logger.info)(message, *args)

    report = AuditReport(dry_run=dry_run)

    try:
        profiles = airtable.profiles_needing_verification()
    except Exception as exc:
        log("error", "verification audit: could not read Airtable: %s", exc)
        report.errors.append(("<airtable>", str(exc)))
        return report

    if not profiles:
        log("info", "verification audit: nothing is parked as %s",
            at.PROFILE_ISSUE_VERIFICATION)
        return report

    if limit is not None and limit >= 0:
        if len(profiles) > limit:
            # Said out loud rather than silently truncated: a pass that checked
            # 8 of 17 and reported "8 checked" reads as a complete sweep.
            log("info", "verification audit: %s profile(s) parked, checking %s this pass "
                        "(the rest are picked up on the next run)", len(profiles), limit)
        profiles = profiles[:limit]

    for profile in profiles:
        record_id = profile.get("record_id")
        name = profile.get("name") or record_id
        launch_id = str(profile.get("launch_id") or "")
        if not record_id:
            continue
        if not launch_id:
            # No MLX id means no phone to look at. Not an error in this pass's
            # sense -- it is a gap in the profile row that mlx_sync fills.
            report.audited.append(AuditedProfile(
                record_id=record_id, name=name, verdict=VERDICT_UNKNOWN,
                detail="no MLX API ID on the profile row; nothing to launch",
                status=str(profile.get("status") or ""),
                flagged_at=str(profile.get("flagged_at") or "")))
            log("warning", "verification audit: %s has no MLX API ID; skipping", name)
            continue

        try:
            result = probe(profile)
        except Exception as exc:
            log("warning", "verification audit: probe failed for %s: %s", name, exc)
            result = ProbeResult(reachable=False, detail=f"probe raised: {exc}")

        verdict, detail = decide_verification(result or ProbeResult())
        entry = AuditedProfile(record_id=record_id, name=name, verdict=verdict,
                               detail=detail, launch_id=launch_id,
                               status=str(profile.get("status") or ""),
                               flagged_at=str(profile.get("flagged_at") or ""))
        report.audited.append(entry)
        log("info", "verification audit: %s -> %s -- %s", name, verdict, detail)

        if dry_run:
            continue

        note = f"Verification audit: {detail}"
        try:
            if verdict == VERDICT_LOOKS_CLEAR:
                # Untick the checkbox and leave Flagged At alone -- that pair is
                # what hands the profile to the recovery pass, which is the only
                # thing that can revive its dead queue rows.
                if airtable.clear_profile_verification_flag(record_id, note):
                    entry.cleared = True
                    report.cleared += 1
                else:
                    report.errors.append((name, "could not clear the verification flag"))
            else:
                airtable.append_profile_note(record_id, note)
        except Exception as exc:
            log("warning", "verification audit: could not write back for %s: %s", name, exc)
            report.errors.append((name, str(exc)))

    log("info", "verification audit: %s", report.summary())
    return report
