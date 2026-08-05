#!/usr/bin/env bash
#
# Regenerate the shareable report snapshot.
#
# Writes the *fragment* form (no <html>/<head>/<body>), which is what the
# artifact host wraps. The live view is the report server on 127.0.0.1:8080;
# this is only for the copy that gets published off-box.
#
# Prints one line: the generation time and the day's headline numbers, so the
# caller can report what changed without re-reading the file.
set -euo pipefail

OUT="${1:-/root/.claude/jobs/3ef1610b/tmp/adbbot-report.html}"
cd /root/adb_bot
set -a; . /etc/adbbot/env; set +a

PYTHONPATH=/root/adb_bot /root/adb_bot/.venv/bin/python - "$OUT" <<'PY'
import sys
from adb_bot.automation import report, report_html
from adb_bot.automation.run_loop import _airtable

try:
    airtable = _airtable()
except Exception:
    airtable = None

data = report.collect(airtable=airtable, use_cache=False)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(report_html.render(data, live=False, standalone=False, title="ADB bot"))

totals, queue = data["totals"], data["queue"]
bad = ", ".join(sorted(r["loop"] for r in data["health"]["bad"])) or "none"
print(f"{data['generated_at']} | posts={totals['posts']} runs={totals['runs']} "
      f"| {queue['by_status']} | mlx500={totals['mlx_rate']:.1f}% | unhealthy={bad}")
PY
