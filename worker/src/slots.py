import os
import time
import subprocess
import asyncio

import redis as redis_lib

TOKEN_DIR = "/tmp/vnc-tokens"
TOKEN_FILE = f"{TOKEN_DIR}/tokens.cfg"
LOG_DIR = "/tmp/slot-logs"
JOB_TTL = 60 * 60 * 24


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
        return (
            f"Slot({self.index}, :{self.display}, "
            f"vnc={self.vnc_port}, {status})"
        )


class SlotPool:
    def __init__(
        self,
        max_slots: int,
        redis_client: redis_lib.Redis,
        worker_id: str,
    ):
        self.max_slots = max_slots
        self.r = redis_client
        self.worker_id = worker_id
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
            print(
                f"[pool] Slot {slot.index}: display stack failed, releasing"
            )
            slot.job_id = None
            self.free.put_nowait(slot.index)
            return None
        self._write_tokens()

        # Register routing entry: edge reads this to find the worker
        # for a live view request.
        try:
            self.r.set(
                f"worker:job:{job_id}",
                f"{self.worker_id}:{slot.vnc_port}",
                ex=JOB_TTL,
            )
        except Exception as e:
            print(
                f"[pool] WARN: could not register routing for "
                f"{job_id}: {e}"
            )

        return slot

    def release(self, slot: Slot):
        self._stop_display(slot)
        job_id = slot.job_id
        slot.job_id = None
        slot.agent_proc = None
        self.free.put_nowait(slot.index)
        self._write_tokens()

        if job_id:
            try:
                self.r.delete(f"worker:job:{job_id}")
            except Exception as e:
                print(
                    f"[pool] WARN: could not delete routing for "
                    f"{job_id}: {e}"
                )

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

        slot.xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=xvfb_log,
            stderr=xvfb_log,
        )

        socket_path = f"/tmp/.X11-unix/X{slot.display}"
        for _ in range(20):
            if os.path.exists(socket_path):
                break
            if slot.xvfb_proc.poll() is not None:
                print(
                    f"[pool] Slot {slot.index}: Xvfb died "
                    f"(exit={slot.xvfb_proc.returncode})"
                )
                return False
            time.sleep(0.1)
        else:
            print(
                f"[pool] Slot {slot.index}: Xvfb socket never "
                f"appeared at {socket_path}"
            )
            self._kill_proc(slot.xvfb_proc)
            slot.xvfb_proc = None
            return False

        print(f"[pool] Slot {slot.index}: Xvfb ready on {display}")

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

        for _ in range(30):
            if slot.vnc_proc.poll() is not None:
                print(
                    f"[pool] Slot {slot.index}: x11vnc died "
                    f"(exit={slot.vnc_proc.returncode})"
                )
                vnc_log.flush()
                try:
                    with open(f"{LOG_DIR}/vnc-{slot.index}.log") as f:
                        print(f"[pool]   log: {f.read()[-500:]}")
                except Exception:
                    pass
                self._kill_proc(slot.xvfb_proc)
                slot.xvfb_proc = None
                slot.vnc_proc = None
                return False

            check = subprocess.run(
                ["ss", "-tln"], capture_output=True, text=True
            )
            if f":{slot.vnc_port}" in check.stdout:
                break
            time.sleep(0.1)
        else:
            print(
                f"[pool] Slot {slot.index}: x11vnc never bound "
                f"port {slot.vnc_port}"
            )
            self._stop_display(slot)
            return False

        print(
            f"[pool] Slot {slot.index}: x11vnc ready on port "
            f"{slot.vnc_port}"
        )
        return True

    def _stop_display(self, slot: Slot):
        for proc in [slot.vnc_proc, slot.xvfb_proc]:
            self._kill_proc(proc)
        slot.xvfb_proc = None
        slot.vnc_proc = None

        socket_path = f"/tmp/.X11-unix/X{slot.display}"
        try:
            os.unlink(socket_path)
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
