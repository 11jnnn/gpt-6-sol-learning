"""待确认修改、任务状态、应用与恢复（内存实现，服务重启后失效）。

职责：
  1. 保存「待确认修改」（propose 阶段只登记、不落盘）。
  2. 记录任务状态（项目、修正轮数、pytest 授权），按 task_id 隔离。
  3. 应用修改（用户确认后由后端调用，不是模型工具）、备份与恢复。

任务隔离：每个任务用独立 task_id 作为键，授权 / 修正轮数 / 待确认修改互不共享。
模型工具（propose / check）没有 task_id 参数，靠 contextvars 拿到「当前任务」。
"""
import contextvars
import difflib
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid

import fsutil
from fsutil import (
    MAX_FILE_BYTES,
    MAX_RESULT_CHARS,
    _backup_root,
    _clip,
    _rel_display,
    _validate_write_target,
)

# 待确认修改数量上限与过期时间。
MAX_PENDING = 50
CHANGE_TTL = 600  # 秒

# 最多「检查失败 -> 提出修正」轮数。
MAX_FIX_ROUNDS = 3

_LOCK = threading.RLock()

# change_id -> 记录（记录带 task_id，用于按任务过滤展示）。
PENDING = {}

# task_id -> 任务状态
TASKS = {}

# 当前任务（由 chat() 在 agent.invoke 前设置，供模型工具内部读取）。
_current_task = contextvars.ContextVar("_current_task", default="default")


def _now() -> float:
    return time.time()


def set_current_task(task_id):
    _current_task.set(task_id or "default")


def _cur_tid() -> str:
    return _current_task.get() or "default"


def _task():
    """返回当前任务的状态字典，不存在则创建。"""
    tid = _cur_tid()
    if tid not in TASKS:
        TASKS[tid] = {
            "project": None,
            "fix_rounds": 0,
            "last_check_failed": False,
            "pytest_authorized": set(),
            "pending_check": None,
        }
    return TASKS[tid]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return prefix + hashlib.sha256(os.urandom(16)).hexdigest()[:16]


def _new_task_id() -> str:
    return uuid.uuid4().hex[:16]


def _read_strict_utf8(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def _atomic_write_bytes(path: str, data: bytes):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".agent_tmp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _make_diff(old_text: str, new_text: str, file_path: str) -> str:
    a = (old_text or "").splitlines()
    b = new_text.splitlines()
    diff = difflib.unified_diff(
        a, b, fromfile=f"a/{file_path}", tofile=f"b/{file_path}", lineterm="\n"
    )
    return "\n".join(diff)


def _find_unique(content: str, old_text: str):
    variants = [old_text]
    if "\r\n" in old_text:
        variants.append(old_text.replace("\r\n", "\n"))
    if "\n" in old_text and "\r\n" not in old_text:
        variants.append(old_text.replace("\n", "\r\n"))
    seen = set()
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        c = content.count(v)
        if c == 1:
            return v, 1
        if c > 1:
            return v, c
    return None, 0


def _note_fix_round(t):
    if t["last_check_failed"]:
        t["fix_rounds"] += 1
        t["last_check_failed"] = False


def _fix_limit_error(t):
    if t["fix_rounds"] > MAX_FIX_ROUNDS:
        return (
            f"已达到修正次数上限（{MAX_FIX_ROUNDS} 轮），停止提出新的修改。"
            "请报告当前状态与剩余问题。"
        )
    return None


def _enforce_pending_limit():
    if len(PENDING) <= MAX_PENDING:
        return
    for cid, rec in sorted(PENDING.items(), key=lambda kv: kv[1]["created_at"]):
        if len(PENDING) <= MAX_PENDING:
            break
        if rec["status"] in ("pending", "expired"):
            del PENDING[cid]


# --- 提出修改（只登记，不落盘） ---

def propose_create(project_name: str, file_path: str, content: str):
    with _LOCK:
        t = _task()
        _note_fix_round(t)
        if (err := _fix_limit_error(t)):
            return None, None, err

        if not isinstance(content, str):
            return None, None, "新建文件内容必须是字符串。"
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return None, None, (
                f"新建文件内容过大（{len(content.encode('utf-8'))} 字节，"
                f"上限 {MAX_FILE_BYTES} 字节）。"
            )

        real_path, _root, err = _validate_write_target(project_name, file_path)
        if err:
            return None, None, err
        if os.path.exists(real_path):
            return None, None, (
                f"文件已存在：{_rel_display(real_path)}。新建不能覆盖已有文件，"
                "请改用 replace_project_text 或先说明。"
            )
        parent = os.path.dirname(real_path)
        if not os.path.isdir(parent):
            return None, None, f"父目录不存在：{_rel_display(parent)}。"

        diff = _make_diff("", content, file_path)
        change_id = _new_id("chg_")
        PENDING[change_id] = {
            "id": change_id,
            "type": "create",
            "task_id": _cur_tid(),
            "project": project_name,
            "file_path": file_path,
            "content": content,
            "old_hash": None,
            "new_hash": _sha256_text(content),
            "diff": diff,
            "created_at": _now(),
            "expires_at": _now() + CHANGE_TTL,
            "status": "pending",
            "backup_id": None,
        }
        t["project"] = project_name
        _enforce_pending_limit()
        return change_id, diff, None


def propose_replace(project_name: str, file_path: str, old_text: str, new_text: str):
    with _LOCK:
        t = _task()
        _note_fix_round(t)
        if (err := _fix_limit_error(t)):
            return None, None, err

        if not isinstance(old_text, str) or old_text == "":
            return None, None, "old_text 不能为空（不允许用空字符串变相覆盖整个文件）。"

        real_path, _root, err = _validate_write_target(project_name, file_path)
        if err:
            return None, None, err
        if not os.path.isfile(real_path):
            return None, None, f"文件不存在：{_rel_display(real_path)}"

        try:
            content = _read_strict_utf8(real_path)
        except UnicodeDecodeError:
            return None, None, (
                f"文件不是可严格解码的 UTF-8 文本：{_rel_display(real_path)}。"
                "首版仅支持 UTF-8 文本，避免破坏原文件。"
            )
        except OSError as exc:
            return None, None, f"读取文件失败：{exc}"

        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return None, None, f"文件过大：{_rel_display(real_path)}。"

        matched, count = _find_unique(content, old_text)
        if count == 0:
            return None, None, (
                f"old_text 未在当前文件中找到。请先用 read_project_file 重新读取，"
                "确保 old_text 与文件内容完全一致（含换行符）。"
            )
        if count > 1:
            return None, None, (
                f"old_text 在当前文件中出现了 {count} 次，无法唯一确定替换位置。"
                "请读取更多上下文，让 old_text 足够长以唯一定位。"
            )

        new_content = content.replace(matched, new_text, 1)
        # 关键：检查替换后的最终大小，而不是只看原文件大小。
        if len(new_content.encode("utf-8")) > MAX_FILE_BYTES:
            return None, None, (
                f"替换后内容过大（{len(new_content.encode('utf-8'))} 字节，"
                f"上限 {MAX_FILE_BYTES} 字节）。"
            )

        diff = _make_diff(content, new_content, file_path)
        change_id = _new_id("chg_")
        PENDING[change_id] = {
            "id": change_id,
            "type": "replace",
            "task_id": _cur_tid(),
            "project": project_name,
            "file_path": file_path,
            "old_text": matched,
            "new_text": new_text,
            "old_hash": _sha256_text(content),
            "new_hash": _sha256_text(new_content),
            "diff": diff,
            "created_at": _now(),
            "expires_at": _now() + CHANGE_TTL,
            "status": "pending",
            "backup_id": None,
        }
        t["project"] = project_name
        _enforce_pending_limit()
        return change_id, diff, None


# --- 应用 / 拒绝 / 恢复（只由后端调用，不注册为模型工具） ---

def _make_backup(real_path: str, original_bytes: bytes, rec: dict) -> str:
    backup_id = _new_id("bak_")
    backup_dir = os.path.join(_backup_root(), backup_id)
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, "original"), "wb") as f:
        f.write(original_bytes)
    meta = {
        "backup_id": backup_id,
        "project": rec["project"],
        "file_path": rec["file_path"],
        "created_at": _now(),
        "old_hash": rec["old_hash"],
        "new_hash": rec["new_hash"],
    }
    with open(os.path.join(backup_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return backup_id


def apply_change(change_id: str) -> dict:
    with _LOCK:
        rec = PENDING.get(change_id)
        if rec is None:
            return {"ok": False, "error": "找不到该修改建议（可能已过期或服务重启后失效）。"}
        if rec["status"] != "pending":
            return {"ok": False, "error": f"该修改建议状态为 {rec['status']}，不能应用。"}
        if _now() > rec["expires_at"]:
            rec["status"] = "expired"
            return {"ok": False, "error": "该修改建议已过期，请重新生成。"}

        real_path, _root, err = _validate_write_target(rec["project"], rec["file_path"])
        if err:
            return {"ok": False, "error": err}

        if rec["type"] == "create":
            if os.path.exists(real_path):
                return {"ok": False, "error": f"目标文件已存在：{_rel_display(real_path)}，不能覆盖。"}
            if len(rec["content"].encode("utf-8")) > MAX_FILE_BYTES:
                return {"ok": False, "error": f"新建文件内容过大，超过上限 {MAX_FILE_BYTES} 字节。"}
            try:
                _atomic_write_bytes(real_path, rec["content"].encode("utf-8"))
            except OSError as exc:
                return {"ok": False, "error": f"写入失败：{exc}"}
            rec["status"] = "applied"
            rec["backup_id"] = None
            return {"ok": True, "change_id": change_id, "backup_id": None,
                    "message": "新建文件已写入。"}

        try:
            with open(real_path, "rb") as f:
                current_bytes = f.read()
        except OSError as exc:
            return {"ok": False, "error": f"读取原文件失败：{exc}"}

        if _sha256_bytes(current_bytes) != rec["old_hash"]:
            return {"ok": False, "error": "原文件在提出建议后被修改，无法应用。请重新读取并生成新的建议。"}

        try:
            new_content = _read_strict_utf8(real_path).replace(rec["old_text"], rec["new_text"], 1)
        except UnicodeDecodeError:
            return {"ok": False, "error": "原文件无法严格按 UTF-8 解码，拒绝写入。"}

        # 应用时再次检查替换后的最终大小。
        if len(new_content.encode("utf-8")) > MAX_FILE_BYTES:
            return {"ok": False, "error": f"替换后内容过大，超过上限 {MAX_FILE_BYTES} 字节，拒绝写入。"}

        backup_id = _make_backup(real_path, current_bytes, rec)
        try:
            _atomic_write_bytes(real_path, new_content.encode("utf-8"))
        except OSError as exc:
            return {"ok": False, "error": f"写入失败：{exc}"}

        rec["status"] = "applied"
        rec["backup_id"] = backup_id
        return {"ok": True, "change_id": change_id, "backup_id": backup_id,
                "message": "替换已应用，原文件已备份。"}


def reject_change(change_id: str) -> dict:
    with _LOCK:
        rec = PENDING.get(change_id)
        if rec is None:
            return {"ok": False, "error": "找不到该修改建议。"}
        if rec["status"] != "pending":
            return {"ok": False, "error": f"该修改建议状态为 {rec['status']}。"}
        rec["status"] = "rejected"
        return {"ok": True, "message": "已拒绝该修改建议。"}


def restore_change(change_id: str) -> dict:
    with _LOCK:
        rec = PENDING.get(change_id)
        if rec is None:
            return {"ok": False, "error": "找不到该修改记录。"}
        if rec["status"] != "applied":
            return {"ok": False, "error": f"该修改状态为 {rec['status']}，不能恢复。"}
        if rec["type"] == "create":
            return {"ok": False, "error": "新建文件的恢复暂不支持（首版不删除新建文件）。"}
        backup_id = rec.get("backup_id")
        if not backup_id:
            return {"ok": False, "error": "该修改没有备份记录。"}

        original_path = os.path.join(_backup_root(), backup_id, "original")
        if not os.path.isfile(original_path):
            return {"ok": False, "error": "备份文件不存在。"}

        real_path, _root, err = _validate_write_target(rec["project"], rec["file_path"])
        if err:
            return {"ok": False, "error": err}

        try:
            with open(real_path, "rb") as f:
                current = f.read()
        except OSError as exc:
            return {"ok": False, "error": f"读取当前文件失败：{exc}"}

        if _sha256_bytes(current) != rec["new_hash"]:
            return {"ok": False, "error": "文件在应用后又发生了变化，拒绝自动恢复以避免覆盖新修改。"}

        try:
            with open(original_path, "rb") as f:
                original = f.read()
            _atomic_write_bytes(real_path, original)
        except OSError as exc:
            return {"ok": False, "error": f"恢复失败：{exc}"}

        rec["status"] = "restored"
        return {"ok": True, "change_id": change_id, "message": "已恢复到修改前的内容。"}


# --- 检查授权与任务状态（按 task_id 隔离） ---

def require_pytest_auth(project_name: str) -> bool:
    with _LOCK:
        t = _task()
        if project_name in t["pytest_authorized"]:
            return True
        t["pending_check"] = {"project": project_name, "check_type": "pytest"}
        return False


def authorize_pytest(task_id: str, project_name: str) -> dict:
    with _LOCK:
        if task_id not in TASKS:
            TASKS[task_id] = {
                "project": None, "fix_rounds": 0, "last_check_failed": False,
                "pytest_authorized": set(), "pending_check": None,
            }
        t = TASKS[task_id]
        t["pytest_authorized"].add(project_name)
        t["pending_check"] = None
        t["project"] = project_name
    return {"ok": True}


def deny_pytest(task_id: str) -> dict:
    with _LOCK:
        if task_id in TASKS:
            TASKS[task_id]["pending_check"] = None
    return {"ok": True}


def note_check_failure():
    with _LOCK:
        _task()["last_check_failed"] = True


def note_check_success():
    with _LOCK:
        _task()["last_check_failed"] = False


def reset_task() -> dict:
    """开始新任务：返回一个全新的 task_id（前端持有它，实现任务隔离）。"""
    return {"ok": True, "task_id": _new_task_id()}


def list_pending_summaries(task_id: str) -> list:
    with _LOCK:
        out = []
        for cid, rec in PENDING.items():
            if rec.get("task_id") != task_id:
                continue
            if rec["status"] != "pending":
                continue
            if _now() > rec["expires_at"]:
                rec["status"] = "expired"
                continue
            out.append({
                "id": cid,
                "type": rec["type"],
                "project": rec["project"],
                "file_path": rec["file_path"],
                "diff": _clip(rec["diff"], MAX_RESULT_CHARS),
            })
        return out


def drain_pending_check(task_id: str):
    with _LOCK:
        t = TASKS.get(task_id)
        if t is None:
            return None
        pc = t["pending_check"]
        t["pending_check"] = None
        return pc


def get_task_summary(task_id: str) -> dict:
    with _LOCK:
        t = TASKS.get(task_id)
        project = t["project"] if t else None
        fix_rounds = t["fix_rounds"] if t else 0
        return {
            "project": project,
            "fix_rounds": fix_rounds,
            "max_fix_rounds": MAX_FIX_ROUNDS,
        }
