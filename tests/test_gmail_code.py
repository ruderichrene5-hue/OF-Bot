"""Reading a confirmation code out of the Gmail app, and refusing to guess.

The dangerous failure here is not "no code found" -- it is finding the *wrong*
six digits and typing them in with total confidence. These phones ship with a
resident Google account, and the one on the `gmail test` profile held an
Instagram code of its own from 5 August, so "the inbox that happens to be open"
is not the inbox we mean.
"""

import pytest
from adb_bot.automation.flows import gmail_code

INBOX = (
    "signed in as mia berg mia.berg1999@gmail.com search in mail primary "
    "instagram 418902 is your instagram code 20:14 "
    "google security alert for your linked account 19:02 "
    "lieferando your order 774213 is on its way 18:30")

SOMEBODY_ELSES_INBOX = (
    "signed in as i1aikjg s11a i1aikjgs11a@gmail.com search in mail primary "
    "instagram 902551 is your instagram code 5 aug")

SYNC_OFF = (
    "signed in as mia berg mia.berg1999@gmail.com primary "
    "gmail sync is off turn on sync to see your mail settings")


def test_the_code_is_read_from_the_instagram_message():
    assert gmail_code.find_code(INBOX, "mia.berg1999@gmail.com") == "418902"


def test_a_six_digit_order_number_is_not_a_code():
    """The other numbers in an inbox are the whole hazard."""
    text = ("signed in as mia berg mia.berg1999@gmail.com primary "
            "lieferando your order 774213 is on its way 18:30")
    assert gmail_code.find_code(text, "mia.berg1999@gmail.com") == ""


def test_no_instagram_message_means_no_code():
    assert gmail_code.find_code(
        "signed in as mia berg primary nothing from anyone 123456", "x") == ""


def test_the_code_next_to_instagrams_name_wins():
    """A flattened dump lists several messages and the newest is not first."""
    text = ("signed in as mia berg mia.berg1999@gmail.com "
            "amazon 111222 dispatched. instagram 418902 is your instagram code. "
            "spotify 333444 receipt")
    assert gmail_code.find_code(text, "mia.berg1999@gmail.com") == "418902"


def test_an_empty_screen_yields_nothing():
    assert gmail_code.find_code("", "a@b.com") == ""
    assert gmail_code.find_code(None, "a@b.com") == ""


def test_the_owner_of_the_inbox_is_checked_in_full():
    assert gmail_code.inbox_shows_address(INBOX, "mia.berg1999@gmail.com")
    assert not gmail_code.inbox_shows_address(
        SOMEBODY_ELSES_INBOX, "mia.berg1999@gmail.com")


def test_a_matching_local_part_on_another_domain_is_not_our_mailbox():
    text = "signed in as mia berg mia.berg1999@googlemail.com primary"
    assert not gmail_code.inbox_shows_address(text, "mia.berg1999@gmail.com")


def test_sync_off_is_not_the_same_as_an_empty_inbox():
    """One is fixed by waiting, the other by turning a switch on."""
    assert gmail_code.sync_is_off(SYNC_OFF)
    assert not gmail_code.sync_is_off(INBOX)


class FakeDriver:
    def __init__(self, screens):
        self._screens = list(screens)
        self.taps = []

    def read_screen(self):
        return self._screens.pop(0) if self._screens else ""

    def tap_label(self, labels):
        self.taps.append(labels)
        return True


class FakeAdb:
    """A phone with Gmail installed that comes to the front when started."""

    def __init__(self, installed=True, comes_to_front=True):
        self.commands = []
        self.installed = installed
        self.comes_to_front = comes_to_front
        self.backs = 0

    def shell_back(self, target):
        self.backs += 1
        return ""

    def run_command(self, command):
        self.commands.append(command)
        if "pm list packages" in command:
            return f"package:{gmail_code.GMAIL_PACKAGE}" if self.installed else ""
        if "mCurrentFocus" in command:
            front = (gmail_code.GMAIL_PACKAGE if self.comes_to_front
                     else gmail_code.INSTAGRAM_PACKAGE)
            return f"  mCurrentFocus=Window{{a1 u0 {front}/x}}"
        return ""


def _mailbox(screens, address="mia.berg1999@gmail.com", adb=None):
    adb = adb or FakeAdb()
    box = gmail_code.PhoneMailbox("host:1", adb, address,
                                  driver=FakeDriver(screens))
    return box, adb


# Instagram's own confirmation page, which names the address it mailed. This is
# what the flow actually read for 210 seconds on 2026-08-17 while believing it
# was reading the inbox.
INSTAGRAM_CODE_SCREEN = (
    "enter the confirmation code to confirm your profile, enter the 6-digit "
    "code we sent to mia.berg1999@gmail.com. next i didn't receive the code")


RESOLVE_OUTPUT = ("priority=0 preferredOrder=0 match=0x108000 isDefault=true\n"
                  "com.google.android.gm/.ConversationListActivityGmail\n")


def test_the_launcher_activity_is_picked_out_of_the_resolver_output():
    assert gmail_code._components(RESOLVE_OUTPUT) == [
        "com.google.android.gm/.ConversationListActivityGmail"]


def test_the_likely_launcher_is_tried_before_the_rest():
    """`dumpsys package` lists dozens; starting each in turn would spend more
    of the phone's life than the whole signup."""
    dump = ("com.google.android.gm/.provider.SomeService "
            "com.google.android.gm/.ConversationListActivityGmail "
            "com.google.android.gm/.WidgetService")
    assert gmail_code._components(dump)[0].endswith("ConversationListActivityGmail")


def test_the_component_list_is_bounded():
    dump = " ".join(f"com.google.android.gm/.Activity{n}" for n in range(20))
    assert len(gmail_code._components(dump)) == 3


NOTIFICATION_DUMP = """\
NotificationRecord(0x1: pkg=com.android.systemui id=123 tag=null
      android.title=Battery
      android.text=42% remaining until 18:30
NotificationRecord(0x2: pkg=com.google.android.gm id=456 tag=null
      android.title=418902 is your Instagram code
      android.text=Confirm your account with this code
"""


def test_the_code_is_read_from_the_notification_shade():
    """No app has to be in front, so there is no tour, no compose window and
    no "is this our inbox?" to get wrong."""
    assert gmail_code.code_from_notifications(NOTIFICATION_DUMP) == "418902"


def test_numbers_elsewhere_in_the_dump_are_not_the_code():
    """A dumpsys dump is thousands of lines of numbers. Only a code on the
    same line as Instagram's name counts -- there is no "any six digits"
    fallback."""
    no_instagram = ("android.title=Battery\n"
                    "android.text=123456 steps today\n")
    assert gmail_code.code_from_notifications(no_instagram) == ""


def test_an_empty_notification_dump_yields_nothing():
    assert gmail_code.code_from_notifications("") == ""
    assert gmail_code.code_from_notifications(None) == ""


WELCOME_TOUR = ("new in gmail all the features you love with a fresh new look "
                "got it")

# Verbatim from `Blank caio 2`, 2026-08-17. Read for 210 seconds as an inbox.
COMPOSE = ("navigate up attach files send more options from from "
           "mia.berg1999@gmail.com to add cc/bcc subject compose email")


def test_a_compose_window_is_not_an_inbox():
    """It carries the address in its `From` field, so everything that merely
    looks for the address passes on it."""
    assert gmail_code.looks_like_compose(COMPOSE)
    assert gmail_code.inbox_shows_address(COMPOSE, "mia.berg1999@gmail.com")


def test_a_real_inbox_is_not_read_as_compose():
    assert not gmail_code.looks_like_compose(INBOX)


def test_a_permission_dialog_in_front_of_gmail_is_cleared(monkeypatch):
    """This is what actually blocked Gmail for five launches: it starts, asks
    for a runtime permission, and its own dialog holds the focus -- so Gmail
    never "arrives" and every later candidate starts behind the same dialog."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)

    class Blocked(FakeAdb):
        def __init__(self):
            super().__init__()
            self.dialog = True

        def run_command(self, command):
            if "mCurrentFocus" in command:
                if self.dialog:
                    return ("mCurrentFocus=Window{1 u0 "
                            f"{gmail_code.PERMISSION_PACKAGE}/x}}")
                return f"mCurrentFocus=Window{{1 u0 {gmail_code.GMAIL_PACKAGE}/x}}"
            return super().run_command(command)

    adb = Blocked()

    class Allowing(FakeDriver):
        def tap_label(self, labels):
            super().tap_label(labels)
            adb.dialog = False          # "Allow" dismisses it
            return True

    box = gmail_code.PhoneMailbox("host:1", adb, "a@gmail.com",
                                  driver=Allowing([]))
    assert box._wait_in_front(seconds=6)


def test_notification_permission_is_granted_without_a_dialog(monkeypatch):
    """The shade read depends on it, and granting beats tapping."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = FakeAdb()
    box = gmail_code.PhoneMailbox("host:1", adb, "a@gmail.com",
                                  driver=FakeDriver([]))
    box.open_gmail()
    assert any("pm grant" in c and gmail_code.NOTIFICATION_PERMISSION in c
               for c in adb.commands)


def test_gmail_is_given_time_to_come_up():
    """Six seconds was not enough for a just-installed Gmail: the check said
    "not in front", the next candidate was started over the top of it, and
    whatever started fastest won instead of the mailbox."""
    class Slow(FakeAdb):
        def __init__(self):
            super().__init__()
            self.looks = 0

        def run_command(self, command):
            if "mCurrentFocus" in command:
                self.looks += 1
                if self.looks < 4:      # not up yet
                    return "mCurrentFocus=Window{a1 u0 com.android.launcher/x}"
            return super().run_command(command)

    adb = Slow()
    box = gmail_code.PhoneMailbox("host:1", adb, "a@gmail.com")
    assert box._wait_in_front(seconds=30)


def test_autosend_is_not_a_mailbox():
    dump = ("com.google.android.gm/.AutoSendActivity "
            "com.google.android.gm/.ConversationListActivityGmail")
    assert all("autosend" not in c.lower()
               for c in gmail_code._components(dump))


def test_compose_activities_are_never_started():
    """Ranking by "mail" put `.ComposeActivityGmailExternal` first -- every
    component of Gmail contains "mail"."""
    dump = ("com.google.android.gm/.ComposeActivityGmailExternal "
            "com.google.android.gm/.ConversationListActivityGmail")
    picked = gmail_code._components(dump)
    assert all("compose" not in c.lower() for c in picked)
    assert picked[0].endswith("ConversationListActivityGmail")


def test_the_compose_window_is_backed_out_of_not_read(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, adb = _mailbox([COMPOSE, INBOX])
    assert box.wait_for_code(timeout=60) == "418902"
    assert adb.backs == 1, "read the compose window instead of leaving it"


def test_gmails_welcome_tour_is_recognised():
    """A Gmail installed a minute ago opens on this, not on an inbox."""
    assert gmail_code.is_onboarding(WELCOME_TOUR)


MEET_PROMO = ("close google meet, now in gmail video meetings with live "
              "captioning and screen sharing for up to 100 people")


def test_the_second_promo_behind_the_first_is_also_a_tour():
    """Gmail stacks these: the Meet promo sits behind the welcome tour, and
    stopped a run one screen further on."""
    assert gmail_code.is_onboarding(MEET_PROMO)


SYNC_OFF_INBOX = ("open navigation drawer search in emails signed in as cici "
                  "rahmaputrimu mia.berg1999@gmail.com account and settings. "
                  "primary account sync is off. turn it on in account "
                  "settings. dismiss")


def test_gmails_sync_banner_is_followed_to_the_switch(monkeypatch):
    """The account was on the phone and the inbox was open, and Gmail simply
    was not fetching mail. The banner saying so is itself the way to fix it."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)

    class Settings(FakeDriver):
        def __init__(self):
            # The banner, then the settings screen `_turn_sync_on` reads, then
            # the inbox it comes back to.
            super().__init__([SYNC_OFF_INBOX, "sync gmail data usage", INBOX])
            self.tapped = []

        def tap_label(self, labels):
            self.tapped.append(labels)
            return True

    driver = Settings()
    adb = FakeAdb()
    box = gmail_code.PhoneMailbox("host:1", adb, "mia.berg1999@gmail.com",
                                  driver=driver)

    assert box.wait_for_code(timeout=60) == "418902"
    assert any(gmail_code._SYNC_BANNER_LABELS == t for t in driver.tapped), \
        "never followed the sync banner"
    assert any(gmail_code._SYNC_SWITCH_LABELS == t for t in driver.tapped), \
        "never reached the sync switch"


def test_the_inbox_tip_over_the_message_list_is_dismissed():
    """"Welcome to your new inbox" sits over the only part of the screen worth
    reading."""
    tip = ("welcome to your new inbox mail categories group messages of the "
           "same type for reading all at once dismiss tip")
    assert gmail_code.is_onboarding(tip)


def test_an_inbox_is_not_mistaken_for_the_tour():
    assert not gmail_code.is_onboarding(INBOX)


def test_the_welcome_tour_is_clicked_through_then_the_inbox_is_read(monkeypatch):
    """Without this the tour reads as somebody else's mailbox and the run
    stops one screen short of the code."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([WELCOME_TOUR, WELCOME_TOUR, INBOX])
    assert box.wait_for_code(timeout=60) == "418902"


def test_a_tour_that_never_ends_is_reported_not_waited_out(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([WELCOME_TOUR] * 40)
    try:
        box.wait_for_code(timeout=600)
    except gmail_code.MailboxNotReady as exc:
        assert "welcome tour" in str(exc)
    else:
        raise AssertionError("tapped at a tour forever")


def test_a_missing_gmail_is_reported_at_once_not_waited_out(monkeypatch):
    """Gmail is not preinstalled on these phones. `am start` failed with
    "Activity class ... does not exist", nothing said so, and a launch died."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([INBOX], adb=FakeAdb(installed=False))
    try:
        box.wait_for_code(timeout=300)
    except gmail_code.MailboxNotReady as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("waited for a code from an app that is not there")


def test_a_gmail_that_will_not_come_up_still_reads_the_shade(monkeypatch):
    """Four launches ended on "Gmail would not come to the front" with the
    mail very likely already delivered. The shade needs nothing in front."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)

    class NoUi(FakeAdb):
        def __init__(self):
            super().__init__(comes_to_front=False)

        def run_command(self, command):
            if "dumpsys notification" in command:
                return NOTIFICATION_DUMP
            return super().run_command(command)

    box, _ = _mailbox([], address="mia.berg1999@gmail.com", adb=NoUi())
    assert box.wait_for_code(timeout=60) == "418902"


def test_instagrams_own_screen_is_never_read_as_the_inbox(monkeypatch):
    """It names the address, so "is this our inbox?" would pass on it.

    With Gmail not in front the screen is not read at all -- the run gives no
    code rather than a wrong one, and never touches Instagram's page.
    """
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    driver = FakeDriver([INSTAGRAM_CODE_SCREEN])
    adb = FakeAdb(comes_to_front=False)
    box = gmail_code.PhoneMailbox("host:1", adb, "mia.berg1999@gmail.com",
                                  driver=driver)

    assert box.wait_for_code(timeout=30) == ""
    assert driver._screens, "read the screen while Gmail was not in front"


def test_reading_a_code_switches_to_gmail_and_back(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, adb = _mailbox([INBOX])
    assert box.wait_for_code(timeout=30) == "418902"

    started = [c for c in adb.commands if "am start" in c]
    assert any(gmail_code.GMAIL_PACKAGE in c for c in started)
    # Instagram must be back in front, or the next screen read reports an
    # unknown screen and ends a run that was fine.
    assert gmail_code.INSTAGRAM_PACKAGE in started[-1]
    # Never force-stop: a backgrounded signup survives, a killed one does not.
    assert not any("force-stop" in c for c in adb.commands)


def test_somebody_elses_inbox_is_refused_rather_than_read(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, adb = _mailbox([SOMEBODY_ELSES_INBOX])
    try:
        box.wait_for_code(timeout=30)
    except gmail_code.WrongMailbox as exc:
        assert "mia.berg1999@gmail.com" in str(exc)
    else:
        raise AssertionError("read a code out of the wrong mailbox")
    # Even refusing, it must put Instagram back.
    assert gmail_code.INSTAGRAM_PACKAGE in adb.commands[-1]


def test_a_mailbox_that_is_not_syncing_is_reported_not_waited_on(monkeypatch):
    """Still reported -- but only after the switch has been tried, so the
    screen has to keep saying it, which is what a phone that will not sync
    does."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([SYNC_OFF] * 12)
    try:
        box.wait_for_code(timeout=30)
    except gmail_code.MailboxNotReady as exc:
        assert "Sync Gmail" in str(exc)
    else:
        raise AssertionError("waited out the timeout on a mailbox that never syncs")


def test_no_mail_inside_the_window_returns_empty(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    quiet = ("signed in as mia berg mia.berg1999@gmail.com primary "
             "no new mail here")
    box, _ = _mailbox([quiet, quiet, quiet, quiet, quiet, quiet])
    assert box.wait_for_code(timeout=1, poll_seconds=0) == ""


# The real `dumpsys content` rows off `Blank caio 2`, 2026-08-18. Columns are
# authority, syncable, enabled -- and `syncable=-1` is the state a freshly
# signed-in account sits in, which is why only `enabled` may be read.
SYNC_DUMP_OFF = (
    "com.google.android.gms.reminders         -1        false    Total  0    0\n"
    "com.google.android.location.reporting    1         true     Total  0    0\n"
    "gmail-ls                                 -1        false    Total  0    0\n"
    "subscribedfeeds                          -1        true     Total  0    0\n")
SYNC_DUMP_ON = SYNC_DUMP_OFF.replace(
    "gmail-ls                                 -1        false",
    "gmail-ls                                 -1        true ")


def test_the_sync_authority_row_is_read_off_a_real_dump():
    assert gmail_code.sync_enabled_in_dump(SYNC_DUMP_OFF) is False
    assert gmail_code.sync_enabled_in_dump(SYNC_DUMP_ON) is True


def test_a_missing_authority_is_not_the_same_as_sync_being_off():
    """None and False call for different actions: one is "I could not tell",
    the other is "no mail can arrive". Collapsing them would abandon runs on
    phones that were fine."""
    assert gmail_code.sync_enabled_in_dump("nothing about gmail here") is None
    assert gmail_code.sync_enabled_in_dump("") is None
    assert gmail_code.sync_enabled_in_dump(None) is None


class SyncAdb(FakeAdb):
    def __init__(self, dump, **kw):
        super().__init__(**kw)
        self.dump = dump

    def run_command(self, command):
        if "dumpsys content" in command:
            self.commands.append(command)
            return self.dump
        return super().run_command(command)


def test_a_mailbox_that_cannot_sync_is_refused_before_any_waiting(monkeypatch):
    """`Blank caio 2` spent its whole 210-second budget on 2026-08-18 polling an
    inbox that could never fill, then reported "no code arrived" -- which reads
    as Instagram's fault. The sync manager knew all along."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([INSTAGRAM_CODE_SCREEN] * 20,
                      adb=SyncAdb(SYNC_DUMP_OFF))
    with pytest.raises(gmail_code.MailboxNotReady) as caught:
        box.wait_for_code(timeout=180, poll_seconds=0)
    assert "Sync Gmail" in str(caught.value)


def test_a_syncing_mailbox_is_not_refused(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    good = ("signed in as mia berg mia.berg1999@gmail.com primary "
            "418902 is your instagram code")
    box, _ = _mailbox([good], adb=SyncAdb(SYNC_DUMP_ON))
    assert box.wait_for_code(timeout=30, poll_seconds=0) == "418902"


def test_an_empty_primary_tab_reads_as_empty():
    """Gmail says "Nothing in Primary", not any of the phrases the flow knew,
    so a synced-but-empty inbox was indistinguishable from a dead one."""
    assert gmail_code.looks_empty("Nothing in Primary") is True
    assert gmail_code.looks_empty("You've finished!") is True


# --- turning Gmail's sync on as a prep step -----------------------------------

class SyncFlipAdb(FakeAdb):
    """A phone whose `gmail-ls` row flips to enabled once the switch is tapped.

    The dump is what `dumpsys content` really prints; the flip is what really
    happened on `Blank caio 2` when `Sync Gmail` was ticked.
    """

    def __init__(self, enabled=False, flips=True, **kw):
        super().__init__(**kw)
        self.enabled = enabled
        self.flips = flips
        self.swipes = 0
        self.started = []

    def flip_on(self):
        if self.flips:
            self.enabled = True

    def run_command(self, command):
        if "dumpsys content" in command:
            row = "true " if self.enabled else "false"
            return ("com.google.android.gms.reminders  -1  false  Total 0\n"
                    f"gmail-ls                         -1  {row}  Total 0\n")
        if "input swipe" in command:
            self.swipes += 1
            return ""
        if "am start" in command:
            self.started.append(command)
            return ""
        return super().run_command(command)


class SyncDriver(FakeDriver):
    """Taps `Sync Gmail`, which flips the phone's authority on."""

    def __init__(self, screens, adb, switch_label="Sync Gmail"):
        super().__init__(screens)
        self.adb = adb
        self.switch_label = switch_label

    def tap_label(self, labels):
        self.taps.append(labels)
        if self.switch_label in labels:
            self.adb.flip_on()
            return True
        return True


def test_sync_already_on_costs_no_ui_at_all():
    """The common case once a phone has been prepared: one dumpsys, no taps."""
    adb = SyncFlipAdb(enabled=True)
    box, _ = _mailbox([], adb=adb)
    box.driver = SyncDriver([], adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_ALREADY_ON
    assert box.driver.taps == []
    assert adb.started == []


def test_sync_off_is_turned_on_and_confirmed_by_the_sync_manager(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False)
    box, _ = _mailbox([], adb=adb)
    # settings list, then the account page already showing the switch
    box.driver = SyncDriver(
        ["Settings General settings mia.berg1999@gmail.com Add account",
         "Data usage Sync Gmail Days of emails to sync"], adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_TURNED_ON
    assert ("Sync Gmail",) in box.driver.taps
    assert any("PublicPreferenceActivity" in c for c in adb.started)


def test_a_tap_that_does_not_flip_the_authority_is_a_failure(monkeypatch):
    """The tick is drawn before the setting is stored, so the checkbox agrees a
    moment early. Only the sync manager is believed."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False, flips=False)
    box, _ = _mailbox([], adb=adb)
    box.driver = SyncDriver(
        ["Settings mia.berg1999@gmail.com",
         "Data usage Sync Gmail"], adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_FAILED


def test_the_switch_is_scrolled_to_rather_than_assumed_on_screen(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False)
    box, _ = _mailbox([], adb=adb)
    # Reads start on the account page, the account row having just been tapped.
    box.driver = SyncDriver(
        ["Inbox type Inbox categories Notifications",
         "Smart Compose Smart Reply Nudges",
         "Data usage Sync Gmail Days of emails to sync"], adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_TURNED_ON
    assert adb.swipes == 2


def test_a_page_that_never_shows_the_switch_stops_instead_of_scrolling_forever(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False)
    box, _ = _mailbox([], adb=adb)
    box.driver = SyncDriver(["Settings mia.berg1999@gmail.com"]
                            + ["nothing useful here"] * 40, adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_FAILED
    assert adb.swipes == gmail_code.MAX_SYNC_SCROLLS


def test_an_unreadable_authority_is_not_read_as_sync_being_off(monkeypatch):
    """None and False mean different things: stopping on "could not tell" would
    abandon phones that were fine."""
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False)
    adb.run_command = lambda command: ""      # dumpsys says nothing at all
    box, _ = _mailbox([], adb=adb)
    box.driver = SyncDriver([], adb)
    assert box.ensure_sync_on() == gmail_code.RESULT_SYNC_UNKNOWN
    assert box.driver.taps == []


def test_the_phone_is_never_left_sitting_in_gmails_settings(monkeypatch):
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    adb = SyncFlipAdb(enabled=False, flips=False)
    box, _ = _mailbox([], adb=adb)
    box.driver = SyncDriver(["Settings mia.berg1999@gmail.com",
                             "Data usage Sync Gmail"], adb)
    box.ensure_sync_on()
    assert any(gmail_code.INSTAGRAM_ACTIVITY in c for c in adb.started)
