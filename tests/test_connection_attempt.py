"""Pending connection cancellation is scoped to an owned child process."""
import subprocess
import sys
from avd_feed_connect.rdp.attempt import ConnectionAttempt


def test_cancel_before_spawn_reaps_late_process():
    attempt = ConnectionAttempt()
    attempt.cancel()
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    try:
        attempt.attach(proc)
        proc.wait(timeout=5)
        assert attempt.cancelled.is_set()
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait()


def test_cancel_stops_owned_group_and_is_idempotent():
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    attempt = ConnectionAttempt()
    try:
        attempt.attach(proc)
        attempt.cancel();attempt.cancel()
        proc.wait(timeout=5)
        assert not attempt.active()
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait()


def test_cancel_kills_descendant_even_when_group_leader_exits(tmp_path):
    import os
    import time
    marker=tmp_path/'child-pid'
    script=('import subprocess,sys,time\n'
            'p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])\n'
            'time.sleep(.2)\n'
            'open(sys.argv[1],"w").write(str(p.pid))\n'
            'time.sleep(60)\n')
    leader=subprocess.Popen([sys.executable,'-c',script,str(marker)],start_new_session=True)
    try:
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        descendant=int(marker.read_text())
        attempt=ConnectionAttempt();attempt.attach(leader);attempt.cancel()
        leader.wait(timeout=5)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status=f'/proc/{descendant}/stat'
            if not os.path.exists(status) or open(status).read().split()[2]=='Z':break
            time.sleep(.02)
        else:raise AssertionError('descendant survived cancellation')
    finally:
        try:os.killpg(leader.pid,9)
        except ProcessLookupError:pass
        leader.wait()
