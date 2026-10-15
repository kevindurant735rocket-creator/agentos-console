"""D107 single-instance behaviour sub-probe.

Why this is product behaviour and not instrument hygiene: double-clicking the
same .app twice on macOS, the user's intuition is "it is already running, bring
that window forward". What actually happens without a guard is two independent
processes each opening a window, the Dock icon bouncing between them, and two
WKWebViews polling the same 8790 service with no idea the other exists. The AX
intermittent collapse measured in D105 (same build, sometimes 18s of one window
per second, sometimes 25s of zero windows) is the classic symptom of those
coexisting instances fighting over the window server.

Shape: collect() only produces a facts dict, judge() is a pure function of that
dict. That split exists so native_gate_selftest can mutate facts and require the
judgements to flip red -- a check buried inside the live probe cannot be given
teeth by the selftest.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
BIN = os.path.join(ROOT, "AgentOS.app", "Contents", "MacOS", "AgentOS")
WORK = os.path.join(ROOT, "work", "native")

# A second instance, if single-instance enforcement is absent, runs the full
# probe sequence before exiting. Give it 30s so it can exit; then a slow exit
# is caught by the elapsed check instead of hanging the gate.
SECOND_AUTOCLOSE = "30"
# The probe sequence itself takes about 36s, so a takeover exit must be far
# below that.
SECOND_FAST_SECONDS = 10.0
# A's evidence fields are flushed only when its sequence wraps up, so we have
# to wait for A to exit on its own.
FIRST_EXIT_TIMEOUT = 90.0

TAKEOVER_MARKS = ("已有实例", "已激活并退出")


def _dump_facts(raw, workdir):
    """Persist the facts dict so the selftest can mutate a *real* base.

    Reading judge() input from disk is what makes the teeth check honest: a
    hand-written all-green base could encode a shape the live probe never
    emits, and every mutation built on it would be fiction.
    """
    p = os.path.join(workdir, "single_instance_facts.json")
    try:
        with open(p + ".tmp", "w") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(p + ".tmp", p)
    except Exception as e:
        sys.stderr.write("facts-dump-failed:%s\n" % e)
    return p


def _pids():
    r = subprocess.run(["pgrep", "-x", "AgentOS"], capture_output=True, text=True)
    return set(int(x) for x in r.stdout.split() if x.strip().isdigit())


def _load(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _run(bin_path, env, timeout):
    try:
        r = subprocess.run([bin_path], env=env, capture_output=True, text=True,
                           timeout=timeout, cwd=ROOT)
        return r.returncode, (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout>%ds" % timeout


def collect(app_bin=None, work=None, url="http://127.0.0.1:8790/", timeout=180,
            second_env_extra=None):
    """Start A (escape hatch open) -> wait for it in the process table -> start
    B without the hatch -> measure B.

    A's evidence fields only land on disk when A's sequence finishes, so the
    first version of this probe read them while A was still alive, necessarily
    got None, and then misread "field not there yet" as "field not written".
    """
    bin_path = app_bin or BIN
    workdir = work or WORK
    os.makedirs(workdir, exist_ok=True)
    p1 = os.path.join(workdir, "probe_single_a.json")
    p2 = os.path.join(workdir, "probe_single_b.json")
    for p in (p1, p2):
        if os.path.isfile(p):
            os.unlink(p)
    env = dict(os.environ)
    env["AGENTOS_PROBE_OUT"] = p1
    env["AGENTOS_URL"] = url
    env["AGENTOS_AUTOCLOSE"] = "60"
    env["AGENTOS_MULTI_INSTANCE"] = "1"
    of = open(os.path.join(workdir, "single-a.out"), "w")
    ef = open(os.path.join(workdir, "single-a.err"), "w")
    a = subprocess.Popen([bin_path], env=env, stdout=of, stderr=ef, text=True,
                         cwd=ROOT)
    raw = {"a_pid": a.pid, "a_in_process_table": False,
           "before_pids": [], "after_pids": [],
           "second_rc": None, "second_elapsed": None,
           "second_stderr_tail": "", "second_takeover_msg": False,
           "second_probe_exists": False, "second_probe_path": p2,
           "first_probe_fields": 0, "first_multi_instance": None,
           "first_sibling_instances": None}
    try:
        before = set()
        for _ in range(40):
            before = _pids()
            if a.pid in before or a.poll() is not None:
                break
            time.sleep(0.25)
        raw["a_in_process_table"] = a.pid in before
        raw["before_pids"] = sorted(before)

        env2 = dict(os.environ)
        env2["AGENTOS_PROBE_OUT"] = p2
        env2["AGENTOS_URL"] = url
        env2["AGENTOS_AUTOCLOSE"] = SECOND_AUTOCLOSE
        # Negative control: pass {"AGENTOS_MULTI_INSTANCE": "1"} to turn B
        # back into the old "single-instance logic does not exist" behaviour,
        # proving these judgements really do go red.
        env2.update(second_env_extra or {})
        t = time.time()
        rc2, se2 = _run(bin_path, env2, timeout)
        raw["second_elapsed"] = round(time.time() - t, 1)
        lines = se2.strip().splitlines()
        tail = (lines[-1] if lines else "")[:80]
        raw["second_rc"] = rc2
        raw["second_stderr_tail"] = tail
        raw["second_takeover_msg"] = all(m in se2 for m in TAKEOVER_MARKS)
        raw["second_probe_exists"] = os.path.isfile(p2)
        raw["after_pids"] = sorted(_pids())

        try:
            a.wait(timeout=FIRST_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        d1 = _load(p1)
        raw["first_probe_fields"] = len(d1)
        raw["first_multi_instance"] = d1.get("probe_multi_instance")
        raw["first_sibling_instances"] = d1.get("probe_sibling_instances")
    finally:
        of.close()
        ef.close()
        if a.poll() is None:
            a.terminate()
            try:
                a.wait(timeout=10)
            except subprocess.TimeoutExpired:
                a.kill()
    _dump_facts(raw, workdir)
    return raw


def judge(raw):
    """Pure function of the facts dict. No process, no filesystem, no clock."""
    checks = []

    def C(name, ok, detail):
        checks.append((name, bool(ok), str(detail)))

    C("single-instance-a-in-process-table", raw.get("a_in_process_table"),
      "A=%s in %s" % (raw.get("a_pid"), raw.get("before_pids")))
    C("single-instance-second-rc", raw.get("second_rc") == 0,
      "rc=%s" % raw.get("second_rc"))
    C("single-instance-second-activated-existing", raw.get("second_takeover_msg"),
      raw.get("second_stderr_tail") or "(no stderr tail)")
    elapsed = raw.get("second_elapsed")
    C("single-instance-second-exited-fast",
      isinstance(elapsed, (int, float)) and elapsed < SECOND_FAST_SECONDS,
      "%s (sequence needs ~36s)" % elapsed)
    C("single-instance-second-wrote-no-probe",
      raw.get("second_probe_exists") is False,
      "takeover branch writes no probe.json")
    before = raw.get("before_pids") or []
    after = raw.get("after_pids") or []
    C("single-instance-no-extra-process", len(after) <= len(before),
      "before=%d after=%d" % (len(before), len(after)))
    sib = raw.get("first_sibling_instances")
    C("single-instance-evidence-fields",
      raw.get("first_multi_instance") == 1 and isinstance(sib, int),
      "multi=%s siblings=%s fields=%s"
      % (raw.get("first_multi_instance"), sib, raw.get("first_probe_fields")))
    return checks


def probe(app_bin=None, work=None, url="http://127.0.0.1:8790/", timeout=180,
          second_env_extra=None):
    """Compatibility wrapper: live facts -> pure judge."""
    raw = collect(app_bin=app_bin, work=work, url=url, timeout=timeout,
                  second_env_extra=second_env_extra)
    return judge(raw), raw
