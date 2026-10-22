"""iPhone automation hub -- the index to find any flow by one grep.

If you (or Claude) are looking for "where is the iphone SIGNUP / POSTING / WARMUP /
PHONE-LOCK / CONTAINER-OPEN / SPOOFER", search this file first. Everything that
lives on the physical iPhone farm is listed below with its real location.

Rule: this file **does not** implement anything. It only re-exports the real
functions so grep finds them from one place. All logic stays in its own module.

================================================================================
 CLI entry points (what you type in the shell)
================================================================================

  SIGNUP (end-to-end: Get Started -> SMS/email -> username -> password):
    .venv/bin/python -m adb_bot.automation.iphone_signup run <airtable_row_id>
    .venv/bin/python -m adb_bot.automation.iphone_signup ready
    Env: IPHONE_SIGNUP_STOP_AFTER=password  (hand off after the password step)

  POSTING (today's spoofed clips, batches of 3, 1-3 h rest, 0-24 h window):
    .venv/bin/python -m adb_bot.automation.iphone_xx_post_batch <YYYY-MM-DD> \
        A1,A2,A3,A4,A5,A6 B1,B2,... C1,... D1,... E1,... \
        [--window 0-24] [--pause 1-3] [--batch 3] [--scroll 60] [--model Nikki]

  WARMUP (10 min scroll + a few follows, per account, XXTouch/WDA):
    .venv/bin/python -m adb_bot.automation.iphone_ig warmup <stream> <container> \
        --minutes 10 --follows 5
    .venv/bin/python -m adb_bot.automation.iphone_ig whoami <stream>

  CONTAINER OPEN (Crane + Shadowrocket + exit-IP check + Instagram):
    from adb_bot.automation.iphone_farm_fastopen import open_container
    open_container(device_rec_id, 'A7')

  CONTAINER (RE)BUILD (push Shadowrocket config to new C/D/E containers):
    .venv/bin/python -m adb_bot.automation.iphone_farm_containers plan|apply iphone3

  SPOOFER (per-container deterministic IDFV/IDFA/UDID/Serial/IMEI):
    from adb_bot.automation.iphone_farm_spoof import apply, identity_for
    apply(container)                               # idempotent, sticky-active

================================================================================
 Phones and letter -> stream mapping
================================================================================

  A = iphone1    B = iphone2    C = iphone3    D = iphone4    E = iphone5
  G = iphone7    H = iphone8    I = iphone6
  See /etc/adbbot/iphone_farm.json for ip / ssh / wda_port per phone.

================================================================================
 Re-exports (grep finds these so you reach the real module in one hop)
================================================================================
"""

from __future__ import annotations

# ---- SIGNUP ----
# .../iphone_signup.py -- end-to-end account creation on a real iPhone
from adb_bot.automation.iphone_signup import (                                  # noqa: F401
    run_row as iphone_signup_run_row,
    ready_rows as iphone_signup_ready_rows,
    describe as iphone_signup_describe,
    check_budget as iphone_signup_check_budget,
    note_attempt as iphone_signup_note_attempt,
)

# ---- POSTING ----
# .../iphone_xx_post_batch.py -- all-day posting loop, batches of 3 per account
from adb_bot.automation.iphone_xx_post_batch import (                           # noqa: F401
    run_phone as iphone_posting_run_phone,
    variant_for as iphone_posting_variant_for,
    main as iphone_posting_main,
)

# ---- WARMUP ----
# .../iphone_ig.py -- warmup() lives here. The module is named iphone_ig because
# it also holds whoami/follow/story flows; warmup is the "Day 1" scroll+follows.
from adb_bot.automation.iphone_ig import (                                      # noqa: F401
    warmup as iphone_warmup,
)

# ---- LOW-LEVEL DRIVERS (used by all flows) ----
# .../iphone_xx_flows.py -- XX (XXTouch), post_reel, scroll_feed, open_container
from adb_bot.automation.iphone_xx_flows import (                                # noqa: F401
    XX as iphone_xx_client,
    IG as iphone_ig_bundle,
    post_reel as iphone_post_reel,
    scroll_feed as iphone_scroll_feed,
    FlowError as iphone_flow_error,
)
# .../iphone_farm_wda.py -- Phone (WDA client), connect, resolve
from adb_bot.automation.iphone_farm_wda import (                                # noqa: F401
    Phone as iphone_wda_phone,
    connect as iphone_wda_connect,
    resolve as iphone_resolve,
    WDAError as iphone_wda_error,
)

# ---- PHONE LOCK (signup pauses posting on the same phone) ----
# .../iphone_phone_lock.py -- signup_wanted(L), try_hold(L), release(L), wait_free(L)
from adb_bot.automation.iphone_phone_lock import (                              # noqa: F401
    signup_wanted as iphone_phone_signup_wanted,
    try_hold as iphone_phone_try_hold,
    hold as iphone_phone_hold,
    wait_free as iphone_phone_wait_free,
    want as iphone_phone_want,
    unwant as iphone_phone_unwant,
)

# ---- CONTAINER OPEN & BUILD ----
# .../iphone_farm_fastopen.py -- open_container(rec_id, 'A7'): the whole chain
from adb_bot.automation.iphone_farm_fastopen import (                           # noqa: F401
    open_container as iphone_open_container,
    open_container_fast as iphone_open_container_fast,
)
# .../iphone_farm_containers.py -- rebuild Crane + Shadowrocket for new slots
from adb_bot.automation.iphone_farm_containers import (                         # noqa: F401
    plan as iphone_containers_plan,
    apply as iphone_containers_apply,
)

# ---- SPOOFER ----
# .../iphone_farm_spoof.py -- rnxspoof dylib + per-container IDFV/IDFA/UDID
from adb_bot.automation.iphone_farm_spoof import (                              # noqa: F401
    apply as iphone_spoof_apply,
    identity_for as iphone_spoof_identity_for,
)

# ---- CONFIG ----
# .../iphone_farm_config.py -- /etc/adbbot/iphone_farm.json loader
from adb_bot.automation.iphone_farm_config import (                             # noqa: F401
    load as iphone_config_load,
    relay as iphone_config_relay,
)
