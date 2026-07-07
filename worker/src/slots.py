import os
import time
import shutil
import subprocess
import asyncio

import psutil  # already in the venv (browser-use dependency)

TOKEN_DIR = "/tmp/vnc-tokens"
TOKEN_FILE = f"{TOKEN_DIR}/tokens.cfg"
LOG_DIR = "/tmp/slot-logs"

# Prefixes of the temp Chromium profiles browser-use mkdtemp()s per launch.
# When a launch is cancelled at the bubus timeout, the dir is never cleaned.
# /tmp here is a 3G tmpfs, so leaked profiles literally eat container RAM
# until every subsequent launch starts timing out.
PROFILE_DIR_PREFIXES = ("browser-use-user-data-dir-", "browseruse-tmp-")


def sweep_orphan_profile_dirs(min_age_secs: int = 60) -> int:
    """Delete leaked Chromium temp profiles under /tmp.

    Only removes dirs that (a) are not referenced in any live process's
    cmdline and (b) are older than min_age_secs — so profiles belonging to
    jobs still running on other slots are never touched.
    """
    in_use: set[str] = set()
    for p in psutil.process_iter(["cmdline"]):
        try:
            for arg in p.info["cmdline"] or []:
                if any(pfx in arg for pfx in PROFILE_DIR_PREFIXES):
                    # e.g. --user-data-dir=/tmp/browser-use-user-data-dir-xyz
                    in_use.add(arg.split("=", 1)[-1])
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue

    removed = 0
    now = time.time()
    for entry in os.listdir("/tmp"):
        if not any(entry.startswith(pfx) for pfx in PROFILE_DIR_PREFIXES):
            continue
        path = f"/tmp/{entry}"
        if any(path in used for used in in_use):
            continue
        try:
            if now - os.path.getmtime(path) < min_age_secs:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed:
        print(f"[pool] Swept {removed} orphaned Chromium profile dir(s) from /tmp")
    return removed


class Slot:
    def __init__(self, index: int):
        self.index = index
        self.display = 100 + index
        self.vnc_port = 5900 + index
        self.xvfb_proc: subprocess.Popen | None = None
        self.vnc_proc: subprocess.Popen | None = None
        self.job_id: str | None = None
        self.agent_proc: asyncio.subprocess.Process | None = None

    def __repr__(self):
        status = f"job={self.job_id}" if self.job_id else "free"
        return f"Slot({self.index}, :{self.display}, vnc={self.vnc_port}, {status})"


class SlotPool:
    def __init__(self, max_slots: int = 10):
        self.max_slots = max_slots
        self.slots: list[Slot] = []
        self.free: asyncio.Queue[int] = asyncio.Queue()

        os.makedirs(TOKEN_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)

        for i in range(max_slots):
            self.slots.append(Slot(i))
            self.free.put_nowait(i)

        self._write_tokens()

    def try_acquire(self, job_id: str) -> Slot | None:
        try:
            idx = self.free.get_nowait()
        except asyncio.QueueEmpty:
            return None

        slot = self.slots[idx]
        slot.job_id = job_id
        ok = self._start_display(slot)
        if not ok:
            print(f"[pool] Slot {slot.index}: display stack failed, releasing")
            slot.job_id = None
            self.free.put_nowait(slot.index)
            return None
        self._write_tokens()
        return slot

    def release(self, slot: Slot):
        self._stop_display(slot)
        job_id = slot.job_id
        slot.job_id = None
        slot.agent_proc = None
        self.free.put_nowait(slot.index)
        self._write_tokens()
        sweep_orphan_profile_dirs()
        print(f"[pool] Slot {slot.index} released (was job {job_id})")

    def get_by_job_id(self, job_id: str) -> Slot | None:
        for s in self.slots:
            if s.job_id == job_id:
                return s
        return None

    def active_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.job_id is not None]

    def available_count(self) -> int:
        return self.free.qsize()

    def cleanup_all(self):
        for slot in self.slots:
            if slot.agent_proc and slot.agent_proc.returncode is None:
                try:
                    slot.agent_proc.kill()
                except ProcessLookupError:
                    pass
            self._stop_display(slot)
            slot.job_id = None
            slot.agent_proc = None

    # internal

    def _start_display(self, slot: Slot) -> bool:
        display = f":{slot.display}"
        xvfb_log = open(f"{LOG_DIR}/xvfb-{slot.index}.log", "w")
        vnc_log = open(f"{LOG_DIR}/vnc-{slot.index}.log", "w")

        # Start Xvfb
        slot.xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=xvfb_log,
            stderr=xvfb_log,
        )
        # Child holds its own dup of the fd — close ours or we leak 2 fds
        # per job and eventually hit the ulimit ("display stack failed").
        xvfb_log.close()

        # Wait for Xvfb to create its socket
        socket_path = f"/tmp/.X11-unix/X{slot.display}"
        for i in range(20):  # up to 2 seconds
            if os.path.exists(socket_path):
                break
            if slot.xvfb_proc.poll() is not None:
                print(f"[pool] Slot {slot.index}: Xvfb died (exit={
                      slot.xvfb_proc.returncode})")
                return False
            time.sleep(0.1)
        else:
            print(f"[pool] Slot {
                  slot.index}: Xvfb socket never appeared at {socket_path}")
            self._kill_proc(slot.xvfb_proc)
            slot.xvfb_proc = None
            return False

        print(f"[pool] Slot {slot.index}: Xvfb ready on {display}")

        # Start x11vnc
        slot.vnc_proc = subprocess.Popen(
            [
                "x11vnc",
                "-display", display,
                "-nopw", "-forever", "-shared",
                "-rfbport", str(slot.vnc_port),
            ],
            stdout=vnc_log,
            stderr=vnc_log,
        )
        vnc_log.close()

        # Wait for x11vnc to bind the port
        for i in range(30):  # up to 3 seconds
            if slot.vnc_proc.poll() is not None:
                print(f"[pool] Slot {slot.index}: x11vnc died (exit={
                      slot.vnc_proc.returncode})")
                try:
                    with open(f"{LOG_DIR}/vnc-{slot.index}.log") as f:
                        print(f"[pool]   log: {f.read()[-500:]}")
                except Exception:
                    pass
                self._kill_proc(slot.xvfb_proc)
                slot.xvfb_proc = None
                slot.vnc_proc = None
                return False

            # Check if port is listening
            check = subprocess.run(
                ["ss", "-tln"], capture_output=True, text=True
            )
            if f":{slot.vnc_port}" in check.stdout:
                break
            time.sleep(0.1)
        else:
            print(f"[pool] Slot {slot.index}: x11vnc never bound port {
                  slot.vnc_port}")
            self._stop_display(slot)
            return False

        print(f"[pool] Slot {slot.index}: x11vnc ready on port {
              slot.vnc_port}")
        return True

    def _stop_display(self, slot: Slot):
        for proc in [slot.vnc_proc, slot.xvfb_proc]:
            self._kill_proc(proc)
        slot.xvfb_proc = None
        slot.vnc_proc = None

        # Clean up X socket
        socket_path = f"/tmp/.X11-unix/X{slot.display}"
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        # Clean up the X lock file too — left behind when Xvfb gets
        # SIGKILLed, and a stale lock can block the next Xvfb on this
        # display number.
        try:
            os.unlink(f"/tmp/.X{slot.display}-lock")
        except FileNotFoundError:
            pass

    def _kill_proc(self, proc: subprocess.Popen | None):
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        except ProcessLookupError:
            pass

    def _write_tokens(self):
        lines = []
        for s in self.slots:
            if s.job_id:
                lines.append(f"{s.job_id}: localhost:{s.vnc_port}")
        try:
            with open(TOKEN_FILE, "w") as f:
                f.write("\n".join(lines) + "\n" if lines else "")
        except OSError as e:
            print(f"[pool] Warning: could not write token file: {e}")
