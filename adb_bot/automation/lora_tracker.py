"""Keep the Models table's LoRA columns in line with what is really happening on RunPod.

Every 5 min (adbbot-lora-tracker.timer) one SSH call to the training pod reads, per model
in the Models table (name "Nikki" -> /workspace/ai-toolkit/nikki_train.log and
output/nikki_v*/):

  * the last progress line of the ai-toolkit log ("nikki_v1: 1%| 16/2000 [00:35<1:02:29 ...")
  * whether an ai-toolkit training process is still running
  * whether the final <job>.safetensors exists

and writes "Training Progress" (step x/y, %, time left) and "LoRA Status":
Trainiert... while it runs, Fertig once the final file is there (path into Notizen),
Fehler if the log stopped without a final file. Models without a log are left alone.

Rene 2026-10-05: "auch bitte tracken immer". The RunPod REST key in reel_pipeline's
.env.local was rejected (401) that day, so the pod is reached over SSH; host/port live in
/etc/adbbot/lora_tracker.json and must be updated when the training pod changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

import requests

log = logging.getLogger("lora_tracker")

CONFIG = "/etc/adbbot/lora_tracker.json"
BASE = "appwk9LrgAhJv8iEH"
MODELS = "tblMkfEq3cEnXuZs5"
F_NAME, F_STATUS, F_PROGRESS, F_NOTES = "fld9FFX1ohatkPIev", "fld6cOK4hCphznKaS", "fldivYU6xeNDMBQ55", "fldEkBP7KQcAmDY0R"
TOOLKIT = "/workspace/ai-toolkit"

REMOTE = r"""
for name in %s; do
  logf=%s/${name}_train.log
  [ -f "$logf" ] || { echo "$name|nolog"; continue; }
  line=$(tail -c 4000 "$logf" | tr '\r' '\n' | grep -E '[0-9]+/[0-9]+ \[' | tail -1)
  final=$(ls %s/output/${name}_v*/${name}_v*.safetensors 2>/dev/null | grep -v '_[0-9]\{9\}\.safetensors' | tail -1)
  alive=$(ps -eo args | grep -E 'run\.py|ai-toolkit' | grep -i "$name" | grep -v grep | wc -l)
  age=$(( $(date +%%s) - $(stat -c %%Y "$logf") ))
  echo "$name|log|$alive|$age|$final|$line"
done
"""


def _airtable(method: str, path: str, **kw):
    tok = os.environ["AIRTABLE_TOKEN"] if "AIRTABLE_TOKEN" in os.environ else os.environ["AIRTABLE_API_KEY"]
    r = requests.request(method, f"https://api.airtable.com/v0/{BASE}/{path}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def _progress(line: str) -> str:
    m = re.search(r"(\d+)/(\d+) \[([0-9:]+)<([0-9:]+)", line or "")
    if not m:
        return ""
    done, total, _, left = m.groups()
    return f"{done}/{total} steps ({int(done) * 100 // max(1, int(total))}%) - about {left} left"


def run() -> dict:
    cfg = json.load(open(CONFIG))
    rows = _airtable("GET", MODELS, params={"returnFieldsByFieldId": "true"})["records"]
    names = {r["fields"].get(F_NAME, "").strip().lower(): r for r in rows if r["fields"].get(F_NAME)}
    if not names:
        return {}
    script = REMOTE % (" ".join(names), TOOLKIT, TOOLKIT)
    res = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                          "-p", str(cfg["ssh_port"]), "-i", cfg.get("key", "/root/.ssh/runpod_key"),
                          f"root@{cfg['ssh_host']}", script], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        log.warning("pod not reachable: %s", (res.stderr or "").strip()[-200:])
        return {}
    changes = {}
    for out in res.stdout.splitlines():
        parts = out.split("|", 5)
        if len(parts) < 2 or parts[1] != "log" or parts[0] not in names:
            continue
        name, _, alive, age, final, line = parts + [""] * (6 - len(parts))
        rec = names[name]
        status = (rec["fields"].get(F_STATUS) or {}).get("name", "") if isinstance(rec["fields"].get(F_STATUS), dict) else rec["fields"].get(F_STATUS, "")
        upd = {}
        if final:
            upd = {F_STATUS: "Fertig", F_PROGRESS: "done"}
            notes = rec["fields"].get(F_NOTES, "")
            if final not in notes:
                upd[F_NOTES] = (notes + "\n" if notes else "") + f"LoRA file on the pod: {final}"
        elif int(alive or 0) > 0 or int(age or 99999) < 600:
            upd = {F_STATUS: "Trainiert...", F_PROGRESS: _progress(line) or "starting"}
        elif status.startswith("Trainiert"):
            upd = {F_STATUS: "Fehler", F_PROGRESS: f"stopped without a final file ({_progress(line) or 'no progress'})"}
        cur = {F_STATUS: status, F_PROGRESS: rec["fields"].get(F_PROGRESS, ""), F_NOTES: rec["fields"].get(F_NOTES, "")}
        upd = {k: v for k, v in upd.items() if cur.get(k) != v}
        if upd:
            _airtable("PATCH", f"{MODELS}/{rec['id']}", json={"fields": upd, "typecast": True})
            changes[name] = upd
            log.info("%s: %s", name, upd)
    return changes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
