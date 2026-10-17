#!/bin/bash
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
git checkout -- tools/dsp.py tools/gh_client.py tools/steer.py tools/evo_merge.py tools/autonomy.py tools/agent_core.py
echo "ROLLBACK_OK tools/已还原"
git checkout -- tools/autonomy_gate.py tools/autonomy_selftest.py tools/gates.sh tools/watch.py
rm -f tools/boss.py tools/boss_gate.py
echo "ROLLBACK_OK wave3已还原"
git checkout -- tools/boss.py tools/boss_gate.py tools/leader_gate.py README.md
echo "ROLLBACK_OK wave4已还原"
