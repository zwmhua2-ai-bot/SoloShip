#!/usr/bin/env python3
"""SoloShip P05-B: Windows 后台 Supervisor。

职责：
  * 单实例：操作系统级命名互斥体（Windows Mutex）+ 文件字节锁，PID 文件只是展示用途。
  * 统一调度：P00 交接任务与 P02 开发任务共用一个循环，首版每轮最多执行一个开发任务。
  * 失败隔离：单个任务异常只影响该任务，Supervisor 不退出。
  * 不重跑旧任务：启动时把「已受理但未收尾」的历史任务登记为待恢复，交由 P05-C 处理。
  * 只读凭据检查：绝不写入 GitHub PAT，也不回退到账号级缓存 Token。

控制目录（全部在 SoloShip 根下）：
    .control/supervisor/state.json        运行状态、PID、启动时间、最近心跳
    .control/supervisor/supervisor.lock   字节锁文件（配合命名互斥体）
    .control/supervisor/queue.json        队列快照（审计用）
    .control/supervisor/claims/*.json     单任务认领（防重复执行，留并行扩展位）
    .control/supervisor/recovery_pending.json  未收尾任务（P05-C 再处理）
    .control/supervisor/deferred/         被推迟的任务文件
    .control/supervisor/logs/supervisor-YYYYMMDD.log
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import codex_adapter as codex  # noqa: E402
import env_guard  # noqa: E402
import security  # noqa: E402
import status_index  # noqa: E402
from p00_bridge import atomic_text, dump_json, utc_now  # noqa: E402

ROOT_DEFAULT = BRIDGE_DIR.parent
SCHEMA = "soloship.supervisor/v1"
CONTROL_REL = Path(".control") / "supervisor"
MUTEX_PREFIX = "Local\\SoloShipSupervisor-"
ERROR_ALREADY_EXISTS = 183
PID_MAX_ATTEMPTS = 2

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_NOT_RUNNING = 4
EXIT_START_FAILED = 5
EXIT_STOP_TIMEOUT = 6
EXIT_PREFLIGHT_BLOCKED = 7


# --------------------------------------------------------------------- utils
def force_utf8_stdio() -> list[str]:
    """把**被重定向的** stdout/stderr 固定为 UTF-8，返回实际调整过的流名。

    为什么需要：任务计划程序拉起时，守护进程的 stdout 被重定向到
    `logs/supervisor-boot.log`，Python 默认按本地代码页（zh-CN 为 cp936）写字节，
    而日志消费方按 UTF-8 读，中文就会变成乱码（P05-B 实测过 UTF-8/GBK 混排）。

    为什么只改非终端：交互式控制台仍按本地代码页显示，避免人工排障时反而乱码。
    为什么不用 PYTHONIOENCODING：那会改变环境变量并可能被下游子进程继承，
    这里只动本进程的文本流包装器，不扩大任何子进程能看到的环境。
    """
    changed: list[str] = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if stream.isatty():
                continue
        except (AttributeError, ValueError, OSError):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
            changed.append(name)
        except (ValueError, OSError):
            continue
    return changed


def control_dir(root: Path) -> Path:
    return Path(root).resolve() / CONTROL_REL


def state_path(root: Path) -> Path:
    return control_dir(root) / "state.json"


def queue_path(root: Path) -> Path:
    return control_dir(root) / "queue.json"


def recovery_path(root: Path) -> Path:
    return control_dir(root) / "recovery_pending.json"


def stop_path(root: Path) -> Path:
    return control_dir(root) / "stop.request"


def claims_dir(root: Path) -> Path:
    return control_dir(root) / "claims"


def deferred_dir(root: Path) -> Path:
    return control_dir(root) / "deferred"


def logs_dir(root: Path) -> Path:
    return control_dir(root) / "logs"


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


WRITE_ATTEMPTS = 8


def _write_json(path: Path, payload) -> None:
    """原子写 + 冲突重试。

    Windows 上若另一个进程此刻正打开目标文件（最典型的是 `start` 的
    `_wait_ready` 每秒轮询 `state.json`），`os.replace` 会抛
    `PermissionError [WinError 5]`——Python 的 `open()` 不带 FILE_SHARE_DELETE，
    替换就被拒绝。这是正常的瞬时冲突，重试即可；不重试会让守护进程在
    **第一次心跳**就死掉（P05-B 重启时实测过，任务计划还报 0）。
    """
    delay = 0.02
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            atomic_text(Path(path), dump_json(payload))
            return
        except PermissionError:
            if attempt >= WRITE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.4)


def mutex_name(root: Path) -> str:
    import hashlib

    digest = hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return f"{MUTEX_PREFIX}{digest}"


def pid_alive(pid: int | None) -> bool:
    """只查询，绝不影响目标进程。

    不用 os.kill(pid, 0)：Windows 上它的语义是 TerminateProcess，
    用错的信号或权限边界都可能误伤，且对以受限令牌启动的进程会误判为不存在。
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                      False, pid)
        if not handle:
            err = ctypes.get_last_error()
            # 无权限说明进程存在（只是不归我们管），不能当成"已退出"
            return err == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) == 1
    except ValueError:
        return False


# ------------------------------------------------------------------ OS lock
class SingleInstanceLock:
    """操作系统级单一实例锁。

    Windows：命名互斥体（会话内唯一）。另外始终持有一个文件字节锁，
    这样即使 Python 版本/平台不支持互斥体，也不会有两个进程同时跑。
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.mutex_name = mutex_name(self.root)
        self.lock_path = control_dir(self.root) / "supervisor.lock"
        self._mutex = None
        self._fh = None
        self.owner_pid: int | None = None

    # -- Windows mutex
    def _acquire_mutex(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            return False
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._mutex = handle
        return True

    def _release_mutex(self) -> None:
        if self._mutex is None:
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(self._mutex))
        self._mutex = None

    # -- file byte lock
    def _acquire_file(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                self._fh.close()
            finally:
                self._fh = None
            return False
        payload = json.dumps({"pid": os.getpid(), "at": utc_now(),
                              "mutex": self.mutex_name}).encode("utf-8")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(payload)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return True

    def _release_file(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def acquire(self) -> bool:
        if not self._acquire_mutex():
            return False
        if not self._acquire_file():
            self._release_mutex()
            return False
        self.owner_pid = os.getpid()
        return True

    def release(self) -> None:
        self._release_file()
        self._release_mutex()
        self.owner_pid = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("另一个 Supervisor 实例持有锁")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def lock_is_held(root: Path) -> bool:
    """只探测不占用：能拿到说明当前没有实例。"""
    probe = SingleInstanceLock(root)
    if probe.acquire():
        probe.release()
        return False
    return True


# ------------------------------------------------------------------- logging
def make_logger(root: Path):
    import logging

    logs_dir(root).mkdir(parents=True, exist_ok=True)
    name = f"soloship.supervisor.{abs(hash(str(Path(root).resolve()))) & 0xFFFF}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        stamp = datetime.now().strftime("%Y%m%d")
        handler = logging.FileHandler(logs_dir(root) / f"supervisor-{stamp}.log",
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [pid=%(process)d] %(message)s"))
        logger.addHandler(handler)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(console)
    return logger


# ----------------------------------------------------------------- preflight
def preflight(root: Path, codex_cmd: list[str] | None = None) -> dict:
    """后台环境自检。只读，不写入任何凭据。"""
    root = Path(root).resolve()
    config = security.bridge_config(root)
    github_cfg = config.get("github") or {}
    token_env = github_cfg.get("token_env") or "GH_TOKEN"

    resolved = codex.resolve_codex(codex_cmd)
    version = codex.codex_version(codex_cmd) if resolved else None
    logged_in, login_detail = (False, "codex 不可用")
    if resolved:
        logged_in, login_detail = codex.login_state(codex_cmd, timeout_s=30)

    child = codex.child_env()
    home = os.environ.get("CODEX_HOME")
    auth_file = None
    for candidate in ([Path(home) / "auth.json"] if home else []) + \
                     [Path.home() / ".codex" / "auth.json"]:
        if candidate.is_file():
            auth_file = str(candidate)
            break

    source_env, removed = env_guard.scrub_secrets(dict(os.environ),
                                                  keep_paths=env_guard.PATH_ENV_NAMES)
    sandbox_status = None
    sandbox_cache = root / ".control" / "security" / "sandbox_status.json"
    if sandbox_cache.is_file():
        sandbox_status = (_read_json(sandbox_cache) or {}).get("status")

    result = {
        "codex": {"cmd": resolved, "found": resolved is not None, "version": version},
        "codex_home": {"value": home, "present": bool(home), "auth_file": auth_file},
        "login": {"logged_in": logged_in, "detail": (login_detail or "").strip()[-400:]},
        "env": {
            "kept_for_codex": [name for name in env_guard.PATH_ENV_NAMES if name in child],
            "scrubbed_count": len(removed),
            "gh_token_in_codex_child": token_env in child,
        },
        "github": {
            "enabled": bool(github_cfg.get("enabled")),
            "mode": github_cfg.get("mode"),
            "require_env_token": bool(github_cfg.get("require_env_token", False)),
            "token_env": token_env,
            "token_present": bool(os.environ.get(token_env)),
            "publish_gate": "blocked_missing_token"
            if github_cfg.get("require_env_token") and not os.environ.get(token_env)
            else "ok",
        },
        "sandbox": {
            "execution_mode": security.execution_mode(root),
            "sandbox_status": sandbox_status,
            "windows_test_mode": bool(security.windows_test_mode(root)),
            "isolation_claim": (security.windows_test_mode(root) or {}).get("isolation_claim"),
        },
        "blocking": [],
        "degraded": [],
    }
    if not result["codex"]["found"]:
        result["degraded"].append("CODEX_CLI_MISSING: 开发任务将无法执行（P00 交接不受影响）")
    if result["codex"]["found"] and not logged_in:
        result["degraded"].append("CODEX_NOT_LOGGED_IN: 开发任务会被阻断")
    if not result["codex_home"]["present"]:
        result["degraded"].append("CODEX_HOME 未设置：可能找不到 auth.json")
    if "CODEX_HOME" in os.environ and "CODEX_HOME" not in child:
        result["blocking"].append("CODEX_HOME 被环境清洗误删：Codex 子进程会丢失登录态")
    if result["env"]["gh_token_in_codex_child"]:
        result["blocking"].append("GitHub Token 出现在 Codex 子进程环境：违反凭据隔离")
    if result["github"]["publish_gate"] == "blocked_missing_token":
        result["degraded"].append(
            f"github.publish_gate=blocked_missing_token：发布将被阻断（不回退账号级 Token）")
    result["ok"] = not result["blocking"]
    result["codex_ready"] = bool(result["codex"]["found"] and logged_in)
    result["_source_env_count"] = len(source_env)
    return result


# ---------------------------------------------------------------- supervisor
class Supervisor:
    """单并发调度器。首版每轮最多执行一个任务。

    并行扩展位：`MAX_PARALLEL` 固定为 1，`select_batch()` 决定本轮可执行的任务集合。
    要引入并行时只需重写 select_batch 并让 tick 把结果交给 worker 池；
    认领文件（claims/）与队列快照已按任务粒度隔离，不需要改存储格式。
    """

    MAX_PARALLEL = 1

    TERMINAL_P02 = ("P02_READY_FOR_HUMAN", "P02_DUPLICATE", "P02_REPO_NOT_ALLOWED",
                    "P02_TASK_REJECTED", "P02_BLOCKED_REVIEW", "P02_BLOCKED_TESTS",
                    "P02_BLOCKED_FIX_LIMIT", "P02_BLOCKED_CODEX", "P02_BLOCKED_AUTH",
                    "P02_BLOCKED_TIMEOUT", "P02_BLOCKED_CLI_MISSING", "P02_BLOCKED_MODEL",
                    "P02_BLOCKED_PATH_VIOLATION", "P02_BLOCKED_REVIEW_TAMPER",
                    "P02_BLOCKED_REVIEW_INVALID")

    def __init__(self, root: Path = ROOT_DEFAULT, *, codex_cmd: list[str] | None = None,
                 poll_interval: float = 5.0, preflight_info: dict | None = None,
                 p00_factory=None, p02_factory=None, logger=None, max_attempts: int = PID_MAX_ATTEMPTS):
        self.root = Path(root).resolve()
        self.codex_cmd = codex_cmd
        self.poll_interval = max(0.2, float(poll_interval))
        self.max_attempts = max_attempts
        self.log = logger or make_logger(self.root)
        for path in (control_dir(self.root), claims_dir(self.root), deferred_dir(self.root),
                     logs_dir(self.root)):
            path.mkdir(parents=True, exist_ok=True)
        self.preflight_info = preflight_info if preflight_info is not None else preflight(
            self.root, codex_cmd)
        self.codex_ready = bool(self.preflight_info.get("codex_ready"))
        self._p00_factory = p00_factory
        self._p02_factory = p02_factory
        self._p00 = None
        self._p02 = None
        self.deferred: dict[str, dict] = {}
        self.counts = {"processed": 0, "failed": 0, "deferred": 0, "skipped": 0}
        self.last_result: dict | None = None
        self.current_task: dict | None = None
        self.started_at = utc_now()

    # ------------------------------------------------------------- bridges
    def p00(self):
        if self._p00 is None:
            if self._p00_factory is not None:
                self._p00 = self._p00_factory()
            else:
                import p00_bridge

                self._p00 = p00_bridge.SoloShipP00(self.root)
        return self._p00

    def p02(self):
        if self._p02 is None:
            if self._p02_factory is not None:
                self._p02 = self._p02_factory()
            else:
                import p02_bridge

                self._p02 = p02_bridge.P02Bridge(self.root, codex_cmd=self.codex_cmd)
        return self._p02

    # -------------------------------------------------------------- state
    def snapshot(self) -> dict:
        return {
            "schema": SCHEMA,
            "pid": os.getpid(),
            "state": "running",
            "root": str(self.root),
            "started_at": self.started_at,
            "last_heartbeat": utc_now(),
            "current_task": self.current_task,
            "counts": dict(self.counts),
            "last_result": self.last_result,
            "deferred_count": len(self.deferred),
            "codex_ready": self.codex_ready,
            "execution_mode": self.preflight_info.get("sandbox", {}).get("execution_mode"),
            "queue": self.queue_counts(),
        }

    def heartbeat(self, state: str = "running") -> dict:
        payload = self.snapshot()
        payload["state"] = state
        _write_json(state_path(self.root), payload)
        return payload

    def close_log(self) -> None:
        """释放日志句柄（测试与优雅退出时用，避免文件被占用）。"""
        for handler in list(self.log.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
            self.log.removeHandler(handler)

    # ------------------------------------------------------------- recovery
    def detect_unfinished(self) -> list[dict]:
        """启动时登记「已受理但未收尾」的任务；本阶段绝不自动重跑。"""
        found: list[dict] = []
        db = self.root / ".control" / "p02" / "p02.db"
        if db.is_file():
            import sqlite3

            try:
                conn = sqlite3.connect(str(db), timeout=10.0)
                rows = conn.execute(
                    "SELECT job_id, project_id, request_id, status, created_at"
                    " FROM p02_jobs WHERE finished_at IS NULL").fetchall()
                conn.close()
            except sqlite3.Error:
                rows = []
            for job_id, project_id, request_id, status, created_at in rows:
                found.append({"kind": "p02", "job_id": job_id, "project_id": project_id,
                              "task_id": request_id, "status": status,
                              "created_at": created_at, "action": "deferred_until_p05c"})
        for item in found:
            self.deferred[f"p02:{item['task_id']}"] = item
        if found:
            _write_json(recovery_path(self.root), {
                "schema": SCHEMA, "detected_at": utc_now(),
                "note": "这些任务上次未正常收尾；P05-B 不自动重跑，等待 P05-C 恢复策略。",
                "items": found,
            })
            self.log.warning("检测到 %d 个未收尾任务，已登记为待恢复（不重跑）", len(found))
            self.counts["deferred"] = len(found)
        return found

    # ---------------------------------------------------------------- scan
    def scan(self) -> list[dict]:
        candidates = []
        p00_inbox = self.root / "exchange" / "inbox"
        p02_inbox = self.root / "exchange" / "p02" / "inbox"
        for kind, inbox in (("p00", p00_inbox), ("p02", p02_inbox)):
            if not inbox.is_dir():
                continue
            for path in sorted(inbox.glob("*.ready.json")):
                task_id = path.name[:-len(".ready.json")]
                if f"{kind}:{task_id}" in self.deferred:
                    continue
                candidates.append({
                    "kind": kind,
                    "task_id": task_id,
                    "path": str(path),
                    "mtime": path.stat().st_mtime,
                })
        # P00 交接快、不消耗 Codex 额度，优先；同类按到达顺序
        candidates.sort(key=lambda item: (0 if item["kind"] == "p00" else 1, item["mtime"]))
        return candidates

    def select_batch(self, candidates: list[dict]) -> list[dict]:
        """本轮要执行的任务集合。首版恒为 1 个（并行扩展点）。"""
        return candidates[:max(1, int(self.MAX_PARALLEL))]

    def queue_counts(self) -> dict:
        counts = {"p00": 0, "p02": 0}
        for item in self.scan():
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return counts

    def enqueue(self, candidates: list[dict]) -> list[dict]:
        """把扫描结果并入队列快照（审计用，不作为唯一真相）。"""
        data = _read_json(queue_path(self.root), {"items": []}) or {"items": []}
        known = {(item["kind"], item["task_id"]) for item in data.get("items", [])}
        changed = []
        for cand in candidates:
            key = (cand["kind"], cand["task_id"])
            if key in known:
                continue
            entry = {**{k: cand[k] for k in ("kind", "task_id", "path")},
                     "enqueued_at": utc_now(), "attempts": 0, "state": "queued"}
            data["items"].append(entry)
            known.add(key)
            changed.append(entry)
        if changed:
            _write_json(queue_path(self.root), data)
        return changed

    # -------------------------------------------------------------- claims
    def _claim_path(self, cand: dict) -> Path:
        return claims_dir(self.root) / f"{cand['kind']}__{cand['task_id']}.json"

    def claim(self, cand: dict) -> tuple[str, str]:
        """返回 (状态, 说明)。状态：acquired / busy / interrupted。"""
        path = self._claim_path(cand)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"kind": cand["kind"], "task_id": cand["task_id"],
                              "pid": os.getpid(), "at": utc_now(), "state": "running"},
                             ensure_ascii=False)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_json(path, {}) or {}
            holder = existing.get("pid")
            state = existing.get("state")
            if holder == os.getpid() and state == "running":
                return "busy", "本进程已持有该任务认领"
            if state == "running" and pid_alive(holder):
                return "busy", f"已被 pid={holder} 认领"
            if state == "running":
                return "interrupted", f"上次认领未收尾（pid={holder}），按 P05-C 策略不重跑"
            return "duplicate", f"任务已处理过（state={state}），不重复执行"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        return "acquired", ""

    def close_claim(self, cand: dict, outcome: str, detail: str = "") -> None:
        path = self._claim_path(cand)
        existing = _read_json(path, {}) or {}
        existing.update({"state": outcome, "closed_at": utc_now(), "detail": detail[:500],
                         "pid": existing.get("pid", os.getpid())})
        try:
            _write_json(path, existing)
        except OSError as exc:  # 认领文件写不动不影响任务结论
            self.log.warning("写入认领结果失败: %s", exc)

    # ------------------------------------------------------------ execution
    def _defer(self, cand: dict, reason: str) -> dict:
        target = deferred_dir(self.root) / f"{cand['kind']}__{Path(cand['path']).name}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(cand["path"], target)
        except OSError as exc:
            self.log.warning("推迟任务失败（%s）: %s", reason, exc)
        self.deferred[f"{cand['kind']}:{cand['task_id']}"] = {
            "kind": cand["kind"], "task_id": cand["task_id"], "reason": reason,
            "at": utc_now(), "moved_to": str(target)}
        self.counts["deferred"] += 1
        self.log.warning("推迟任务 %s/%s：%s", cand["kind"], cand["task_id"], reason)
        return {"task_id": cand["task_id"], "kind": cand["kind"], "status": "DEFERRED",
                "reason": reason}

    def execute(self, cand: dict) -> dict:
        started = utc_now()
        self.current_task = {"kind": cand["kind"], "task_id": cand["task_id"], "started_at": started}
        self.heartbeat()
        status, error = "UNKNOWN", ""
        try:
            if cand["kind"] == "p00":
                status = str(self.p00().process_one(Path(cand["path"])))
            else:
                status = str(self.p02().process_one(Path(cand["path"])))
        except Exception as exc:  # noqa: BLE001 - 单任务失败不得影响 Supervisor
            status, error = "SUPERVISOR_ERROR", f"{type(exc).__name__}: {exc}"
            self.log.error("任务 %s/%s 执行异常: %s", cand["kind"], cand["task_id"], exc)
            self.log.debug("%s", traceback.format_exc())
        finally:
            self.current_task = None
        return {"task_id": cand["task_id"], "kind": cand["kind"], "status": status,
                "error": error, "started_at": started, "finished_at": utc_now()}

    def _after_task(self) -> None:
        try:
            status_index.refresh(self.root)
        except Exception as exc:  # noqa: BLE001 - 索引是旁路
            self.log.warning("刷新 P04 状态索引失败: %s", exc)

    def tick(self) -> dict:
        """一个调度周期：至多执行一个任务。"""
        if stop_path(self.root).is_file():
            self.log.info("收到停止请求，走完当前周期后退出")
            return {"stop": True, "executed": None}

        candidates = self.scan()
        self.enqueue(candidates)
        if not candidates:
            self.heartbeat()
            return {"stop": False, "executed": None, "queued": 0}

        batch = self.select_batch(candidates)
        if not batch:
            self.heartbeat()
            return {"stop": False, "executed": None, "queued": len(candidates)}

        cand = batch[0]
        if cand["kind"] == "p02" and not self.codex_ready:
            result = self._defer(cand, "CODEX_UNAVAILABLE")
            self.last_result = result
            self.heartbeat()
            return {"stop": False, "executed": result}

        claim_state, detail = self.claim(cand)
        if claim_state == "interrupted":
            result = self._defer(cand, f"UNFINISHED_CLAIM: {detail}")
            self.last_result = result
            self.heartbeat()
            return {"stop": False, "executed": result}
        if claim_state == "duplicate":
            # 已处理过的任务再次投递：不执行，但也不留在 inbox 里反复扫描。
            result = self._defer(cand, f"ALREADY_PROCESSED: {detail}")
            self.last_result = result
            self.heartbeat()
            return {"stop": False, "executed": result}
        if claim_state == "busy":
            self.log.info("跳过 %s/%s：%s", cand["kind"], cand["task_id"], detail)
            self.counts["skipped"] += 1
            self.heartbeat()
            return {"stop": False, "executed": None, "skipped": detail}

        result = self.execute(cand)
        outcome = "done" if result["status"] not in ("SUPERVISOR_ERROR",) else "failed"
        self.close_claim(cand, outcome, result["status"])
        if outcome == "failed":
            self.counts["failed"] += 1
        else:
            self.counts["processed"] += 1
        self.last_result = result
        self._after_task()
        self.heartbeat()
        self.log.info("完成 %s/%s → %s", cand["kind"], cand["task_id"], result["status"])
        return {"stop": False, "executed": result}

    def wait(self, seconds: float) -> bool:
        """可被 stop.request 打断的等待：停止请求最多 0.2s 内生效。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop_path(self.root).is_file():
                return True
            time.sleep(min(0.2, max(0.02, deadline - time.time())))
        return stop_path(self.root).is_file()

    def run(self, max_cycles: int | None = None, install_signal_handlers: bool = True) -> int:
        self.heartbeat()
        self.log.info("Supervisor 启动 pid=%s root=%s codex_ready=%s",
                      os.getpid(), self.root, self.codex_ready)
        cycles = 0
        try:
            while True:
                if max_cycles is not None and cycles >= max_cycles:
                    break
                cycles += 1
                summary = self.tick()
                if summary.get("stop"):
                    break
                if max_cycles is None and self.wait(self.poll_interval):
                    self.log.info("等待期间收到停止请求")
                    break
        except KeyboardInterrupt:
            self.log.info("收到中断信号，优雅退出")
        finally:
            self.heartbeat(state="stopped")
            self.log.info("Supervisor 已停止 pid=%s cycles=%s counts=%s",
                          os.getpid(), cycles, self.counts)
            self.close_log()
        return EXIT_OK


# --------------------------------------------------------------------- status
def build_status(root: Path) -> dict:
    root = Path(root).resolve()
    data = _read_json(state_path(root), {}) or {}
    held = lock_is_held(root)
    pid = data.get("pid")
    alive = pid_alive(pid)
    heartbeat_at = data.get("last_heartbeat")
    age = None
    if heartbeat_at:
        try:
            age = round(time.time() - datetime.fromisoformat(heartbeat_at).timestamp(), 1)
        except ValueError:
            age = None
    # 锁是唯一权威：PID/state.json 只用于展示与排障（PID 可能被系统复用）。
    running = bool(held)
    return {
        "schema": SCHEMA,
        "root": str(root),
        "running": running,
        "lock_held": held,
        "pid": pid,
        "pid_alive": alive,
        "state": data.get("state") or ("running" if running else "stopped"),
        "started_at": data.get("started_at"),
        "last_heartbeat": heartbeat_at,
        "heartbeat_age_s": age,
        "current_task": data.get("current_task"),
        "counts": data.get("counts") or {},
        "last_result": data.get("last_result"),
        "codex_ready": data.get("codex_ready"),
        "execution_mode": data.get("execution_mode"),
        "queue": (data.get("queue") or {"p00": 0, "p02": 0}),
        "stop_requested": stop_path(root).is_file(),
        "recovery_pending": len((_read_json(recovery_path(root), {}) or {}).get("items") or []),
        "paths": {
            "state": str(state_path(root)),
            "logs": str(logs_dir(root)),
            "claims": str(claims_dir(root)),
        },
    }


# ------------------------------------------------------------------------ CLI
def _spawn_background(root: Path, poll_interval: float, codex_cmd: list[str] | None) -> dict:
    import subprocess

    logs_dir(root).mkdir(parents=True, exist_ok=True)
    log_file = logs_dir(root) / "supervisor-boot.log"
    cmd = [sys.executable, str(Path(__file__).resolve()), "_run",
           "--root", str(root), "--poll", str(poll_interval)]
    if codex_cmd:
        cmd += ["--codex", json.dumps(codex_cmd)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    with open(log_file, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=flags)
    return {"pid": proc.pid, "cmd": cmd, "log": str(log_file)}


def _wait_ready(root: Path, pid: int, timeout_s: float = 20.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = _read_json(state_path(root), {}) or {}
        if data.get("pid") == pid and data.get("state") == "running":
            return {"ready": True, "state": data}
        if data.get("pid") == pid and data.get("state") == "stopped":
            return {"ready": False, "state": data}
        if not pid_alive(pid):
            return {"ready": False, "state": data, "reason": "进程已退出"}
        time.sleep(0.2)
    return {"ready": False, "state": _read_json(state_path(root), {}) or {},
            "reason": f"等待 {timeout_s}s 仍未就绪"}


def cmd_start(args) -> int:
    root = Path(args.root).resolve()
    if lock_is_held(root):
        status = build_status(root)
        print(json.dumps({"ok": False, "reason": "ALREADY_RUNNING", "status": status},
                         ensure_ascii=False, indent=2))
        return EXIT_ALREADY_RUNNING

    if args.foreground:
        lock = SingleInstanceLock(root)
        if not lock.acquire():
            print(json.dumps({"ok": False, "reason": "ALREADY_RUNNING"},
                             ensure_ascii=False, indent=2))
            return EXIT_ALREADY_RUNNING
        try:
            stop_path(root).unlink(missing_ok=True)
            sup = Supervisor(root, codex_cmd=args.codex_cmd, poll_interval=args.poll,
                             preflight_info=preflight(root, args.codex_cmd))
            sup.detect_unfinished()
            print(json.dumps({"ok": True, "mode": "foreground", "pid": os.getpid(),
                              "codex_ready": sup.codex_ready,
                              "preflight": sup.preflight_info}, ensure_ascii=False, indent=2))
            return sup.run(max_cycles=args.max_cycles)
        finally:
            lock.release()

    stop_path(root).unlink(missing_ok=True)
    spawned = _spawn_background(root, args.poll, args.codex_cmd)
    ready = _wait_ready(root, spawned["pid"], args.start_timeout)
    payload = {"ok": ready["ready"], "mode": "background", "pid": spawned["pid"],
               "log": spawned["log"], "ready": ready}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if ready["ready"] else EXIT_START_FAILED


def cmd_stop(args) -> int:
    root = Path(args.root).resolve()
    status = build_status(root)
    if not status["running"]:
        stop_path(root).unlink(missing_ok=True)
        print(json.dumps({"ok": True, "reason": "NOT_RUNNING", "status": status},
                         ensure_ascii=False, indent=2))
        return EXIT_OK

    atomic_text(stop_path(root), json.dumps({"requested_at": utc_now(),
                                             "by_pid": os.getpid()}, ensure_ascii=False))
    pid = status.get("pid")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        data = _read_json(state_path(root), {}) or {}
        if data.get("state") == "stopped" and not lock_is_held(root):
            stop_path(root).unlink(missing_ok=True)
            print(json.dumps({"ok": True, "stopped": True, "graceful": True,
                              "pid": pid, "counts": data.get("counts")},
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        if not pid_alive(pid) and not lock_is_held(root):
            stop_path(root).unlink(missing_ok=True)
            print(json.dumps({"ok": True, "stopped": True, "graceful": True, "pid": pid},
                             ensure_ascii=False, indent=2))
            return EXIT_OK
        time.sleep(0.2)

    if args.force and pid:
        _terminate(pid)
        stop_path(root).unlink(missing_ok=True)
        print(json.dumps({"ok": True, "stopped": True, "graceful": False, "pid": pid,
                          "note": "已强制结束；可能留下未收尾任务，P05-C 处理"},
                         ensure_ascii=False, indent=2))
        return EXIT_OK

    print(json.dumps({"ok": False, "reason": "STOP_TIMEOUT", "pid": pid,
                      "timeout_s": args.timeout,
                      "note": "未强制结束；可加 --force（不推荐）"},
                     ensure_ascii=False, indent=2))
    return EXIT_STOP_TIMEOUT


def _terminate(pid: int) -> None:
    if os.name != "nt":
        os.kill(pid, 15)
        return
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)


def cmd_status(args) -> int:
    status = build_status(Path(args.root))
    if args.as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"SoloShip Supervisor @ {status['root']}")
        print(f"  状态       : {status['state']}（running={status['running']}, lock={status['lock_held']}）")
        print(f"  PID        : {status['pid']}（alive={status['pid_alive']}）")
        print(f"  启动时间   : {status['started_at']}")
        print(f"  最近心跳   : {status['last_heartbeat']}（{status['heartbeat_age_s']}s 前）")
        print(f"  当前任务   : {status['current_task'] or '-'}")
        print(f"  队列       : P00={status['queue'].get('p00', 0)} / P02={status['queue'].get('p02', 0)}")
        print(f"  累计       : {status['counts']}")
        print(f"  待恢复     : {status['recovery_pending']}")
        print(f"  最近结果   : {status['last_result'] or '-'}")
        print(f"  Codex 就绪 : {status['codex_ready']}；执行模式 {status['execution_mode']}")
        print(f"  日志       : {status['paths']['logs']}")
    return EXIT_OK if status["running"] else EXIT_NOT_RUNNING


def cmd_restart(args) -> int:
    stop_args = argparse.Namespace(root=args.root, timeout=args.timeout, force=False)
    code = cmd_stop(stop_args)
    if code not in (EXIT_OK,):
        return code
    return cmd_start(args)


def cmd_doctor(args) -> int:
    info = preflight(Path(args.root), args.codex_cmd)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info["ok"] else EXIT_PREFLIGHT_BLOCKED


def cmd_run(args) -> int:
    """后台子进程入口：自己抢锁，拿不到就静默退出。"""
    root = Path(args.root).resolve()
    lock = SingleInstanceLock(root)
    if not lock.acquire():
        print(json.dumps({"ok": False, "reason": "ALREADY_RUNNING"}, ensure_ascii=False))
        return EXIT_ALREADY_RUNNING
    try:
        sup = Supervisor(root, codex_cmd=args.codex_cmd, poll_interval=args.poll)
        sup.detect_unfinished()
        return sup.run(max_cycles=args.max_cycles)
    finally:
        lock.release()


def cmd_hold_lock(args) -> int:
    """测试辅助：占住锁 N 秒，验证跨进程单实例。"""
    lock = SingleInstanceLock(Path(args.root))
    if not lock.acquire():
        print(json.dumps({"ok": False, "reason": "ALREADY_RUNNING"}, ensure_ascii=False))
        return EXIT_ALREADY_RUNNING
    print(json.dumps({"ok": True, "pid": os.getpid()}, ensure_ascii=False), flush=True)
    try:
        time.sleep(args.seconds)
    finally:
        lock.release()
    return EXIT_OK


def cmd_recovery(args) -> int:
    data = _read_json(recovery_path(Path(args.root)), None)
    print(json.dumps(data or {"items": [], "note": "没有待恢复任务记录"},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SoloShip P05-B Supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, poll_default=5.0):
        p.add_argument("--root", default=str(ROOT_DEFAULT))
        p.add_argument("--codex", dest="codex_json", default=None,
                       help="Codex 命令前缀（shell 字符串或 JSON 数组）")
        p.add_argument("--poll", type=float, default=poll_default)
        return p

    p_start = common(sub.add_parser("start", help="启动 Supervisor（默认后台）"))
    p_start.add_argument("--foreground", action="store_true", help="前台运行（测试/排障）")
    p_start.add_argument("--max-cycles", type=int, default=None, help="跑 N 轮后退出（测试用）")
    p_start.add_argument("--start-timeout", type=float, default=20.0)

    p_stop = sub.add_parser("stop", help="优雅停止")
    p_stop.add_argument("--root", default=str(ROOT_DEFAULT))
    p_stop.add_argument("--timeout", type=float, default=30.0)
    p_stop.add_argument("--force", action="store_true")

    p_status = sub.add_parser("status", help="查询状态")
    p_status.add_argument("--root", default=str(ROOT_DEFAULT))
    p_status.add_argument("--json", dest="as_json", action="store_true")

    p_restart = common(sub.add_parser("restart", help="重启"))
    p_restart.add_argument("--foreground", action="store_true")
    p_restart.add_argument("--max-cycles", type=int, default=None)
    p_restart.add_argument("--start-timeout", type=float, default=20.0)
    p_restart.add_argument("--timeout", type=float, default=30.0)

    p_doctor = sub.add_parser("doctor", help="后台环境与凭据自检（只读）")
    p_doctor.add_argument("--root", default=str(ROOT_DEFAULT))
    p_doctor.add_argument("--codex", dest="codex_json", default=None)

    p_run = sub.add_parser("_run", help="内部：后台进程入口")
    p_run.add_argument("--root", default=str(ROOT_DEFAULT))
    p_run.add_argument("--codex", dest="codex_json", default=None)
    p_run.add_argument("--poll", type=float, default=5.0)
    p_run.add_argument("--max-cycles", type=int, default=None)

    p_hold = sub.add_parser("_hold-lock", help="内部：占锁测试")
    p_hold.add_argument("--root", default=str(ROOT_DEFAULT))
    p_hold.add_argument("--seconds", type=float, default=5.0)

    p_rec = sub.add_parser("recovery", help="查看待恢复任务（P05-C 输入）")
    p_rec.add_argument("--root", default=str(ROOT_DEFAULT))
    return parser


def _parse_codex(value: str | None) -> list[str] | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("["):
        data = json.loads(text)
        return [str(item) for item in data]
    return shlex.split(text, posix=False)


def main(argv: list[str] | None = None) -> int:
    # 必须在 argparse 之前：`start` 的 JSON 也会被重定向到日志文件。
    force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.codex_cmd = _parse_codex(getattr(args, "codex_json", None))
    handler = {
        "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
        "restart": cmd_restart, "doctor": cmd_doctor, "_run": cmd_run,
        "_hold-lock": cmd_hold_lock, "recovery": cmd_recovery,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
