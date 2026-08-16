"""Reading a confirmation code out of the Gmail app, and refusing to guess.

The dangerous failure here is not "no code found" -- it is finding the *wrong*
six digits and typing them in with total confidence. These phones ship with a
resident Google account, and the one on the `gmail test` profile held an
Instagram code of its own from 5 August, so "the inbox that happens to be open"
is not the inbox we mean.
"""

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

    def read_screen(self):
        return self._screens.pop(0) if self._screens else ""


class FakeAdb:
    def __init__(self):
        self.commands = []

    def run_command(self, command):
        self.commands.append(command)
        return ""


def _mailbox(screens, address="mia.berg1999@gmail.com"):
    adb = FakeAdb()
    box = gmail_code.PhoneMailbox("host:1", adb, address,
                                  driver=FakeDriver(screens))
    return box, adb


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
    monkeypatch.setattr(gmail_code.time, "sleep", lambda _s: None)
    box, _ = _mailbox([SYNC_OFF])
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
