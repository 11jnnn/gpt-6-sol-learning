"""本项目的工具集：只读工具 + 提出修改 + 运行检查。

只读工具：list_local_projects / list_project_files / read_project_file / search_project_code。
提出修改：create_project_file / replace_project_text —— 只登记待确认修改，不落盘。
运行检查：run_project_check —— python_syntax（编译检查）或 pytest（执行测试）。

说明「结果如何返回模型」：每个 @tool 函数返回一个字符串，LangChain 会把这个
字符串包装成 ToolMessage 交回模型，模型再据此生成最终回答。

路径与文件校验统一放在 fsutil.py，读取/搜索/提出修改/应用修改共用同一套规则。
"""
import os
import shutil
import subprocess
import sys
import time

from langchain_core.tools import tool

import changes
import fsutil
from fsutil import (
    MAX_FILE_BYTES,
    MAX_LINE_CHARS,
    MAX_RESULT_CHARS,
    SKIP_DIRS,
    TEXT_EXTENSIONS,
    _clip,
    _has_skipped_dir,
    _is_sensitive,
    _is_text_file,
    _rel_display,
    _resolve_project_path,
)

# 工具专属上限。
MAX_LIST_ITEMS = 200
MAX_SEARCH_RESULTS = 30
MAX_FALLBACK_FILES = 500

# pytest 执行超时（秒）。
PYTEST_TIMEOUT = 120


# --- 工具 1：列出本地项目（原有） ---

@tool
def list_local_projects() -> str:
    """列出本地项目目录。

    读取 PROJECTS_DIR 环境变量指定的目录，返回其直接子目录（项目）的名称与完整路径。
    只列出一级子目录，不递归扫描、不读取文件内容、不执行命令。
    """
    base = os.path.abspath(os.getenv("PROJECTS_DIR", r"D:\Projects"))

    if not os.path.isdir(base):
        return f"目录不存在：{base}。请检查 .env 中 PROJECTS_DIR 的配置。"

    try:
        entries = os.listdir(base)
    except OSError as exc:
        return f"读取目录失败：{base}。真实错误信息：{exc}"

    names = sorted(
        name for name in entries
        if os.path.isdir(os.path.join(base, name))
    )

    if not names:
        return f"目录 {base} 下没有任何子目录。"

    lines = [f"目录 {base} 下共有 {len(names)} 个本地项目："]
    for name in names:
        lines.append(f"- {name}（路径：{os.path.join(base, name)}）")
    return "\n".join(lines)


# --- 工具 2：列出项目内的文件（本项目新增） ---

@tool
def list_project_files(project_name: str, relative_dir: str = "") -> str:
    """列出项目内某个目录下的直接文件和文件夹（不递归）。

    参数：
      project_name —— PROJECTS_DIR 下的项目目录名称。
      relative_dir —— 项目内的相对目录，默认空字符串表示项目根目录。
    返回名称、类型和相对路径。跳过 .git/.venv/node_modules 等目录，并对返回数量设上限。
    """
    path, real_root, err = _resolve_project_path(project_name, relative_dir)
    if err:
        return err

    # 检查请求的目录本身是否经过被排除的目录（例如 relative_dir=".venv"）。
    if _has_skipped_dir(os.path.relpath(path, real_root)):
        return f"拒绝访问：{_rel_display(path)} 位于被排除的目录（.git/.venv/node_modules 等）。"

    if not os.path.isdir(path):
        if os.path.isfile(path):
            return f"{_rel_display(path)} 是文件而非目录，请用 read_project_file 读取。"
        return f"目录不存在：{_rel_display(path)}"

    try:
        entries = os.listdir(path)
    except OSError as exc:
        return f"读取目录失败：{_rel_display(path)}。真实错误信息：{exc}"

    items = []
    for name in entries:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            if name in SKIP_DIRS:
                continue
            items.append((name, "目录"))
        elif os.path.isfile(full):
            items.append((name, "文件"))

    items.sort(key=lambda x: x[0].lower())
    truncated = len(items) > MAX_LIST_ITEMS
    shown = items[:MAX_LIST_ITEMS]

    lines = [f"{_rel_display(path)} 下共有 {len(items)} 项："]
    for name, kind in shown:
        rel = os.path.relpath(os.path.join(path, name), fsutil._projects_base()).replace("\\", "/")
        lines.append(f"- [{kind}] {name}  （{rel}）")
    if truncated:
        lines.append(f"（结果过多，仅显示前 {MAX_LIST_ITEMS} 项）")
    return "\n".join(lines)


# --- 工具 3：读取项目内的文件（本项目新增） ---

@tool
def read_project_file(
    project_name: str,
    file_path: str,
    start_line: int = 1,
    max_lines: int = 150,
) -> str:
    """读取项目内某个文本文件的指定行范围（带行号）。

    参数：
      project_name —— 项目目录名称。
      file_path    —— 项目内的相对文件路径。
      start_line   —— 起始行（默认 1）。
      max_lines    —— 读取行数（默认 150，最大 300）。
    文件过大、二进制、无法解码时返回明确错误。可分次读取，但不会把部分读取说成已读完。
    """
    if start_line < 1:
        start_line = 1
    max_lines = max(1, min(max_lines, 300))

    path, real_root, err = _resolve_project_path(project_name, file_path)
    if err:
        return err

    # 策略检查先于「文件是否存在」，避免对排除目录/敏感路径做存在性探测。
    if _has_skipped_dir(os.path.relpath(path, real_root)):
        return f"拒绝读取：{_rel_display(path)} 位于被排除的目录（.git/.venv/node_modules 等）。"
    if _is_sensitive(os.path.basename(path)):
        return f"拒绝读取：{_rel_display(path)} 属于敏感文件（.env、密钥或凭据）。"

    if os.path.isdir(path):
        return f"{_rel_display(path)} 是目录而非文件，请用 list_project_files 列出。"
    if not os.path.isfile(path):
        return f"文件不存在：{_rel_display(path)}"
    if not _is_text_file(path):
        ext = os.path.splitext(path)[1] or "（无扩展名）"
        return f"拒绝读取：{_rel_display(path)} 的类型 {ext} 不在允许的文本格式范围内。"

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return f"读取文件失败：{_rel_display(path)}。真实错误信息：{exc}"
    if size > MAX_FILE_BYTES:
        return (
            f"文件过大：{_rel_display(path)} 有 {size} 字节，"
            f"超过单次读取上限 {MAX_FILE_BYTES} 字节。"
        )

    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as exc:
        return f"读取文件失败：{_rel_display(path)}。真实错误信息：{exc}"
    if b"\x00" in head:
        return f"二进制文件：{_rel_display(path)} 无法按文本读取。"

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="gbk", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            return f"读取文件失败：{_rel_display(path)}。真实错误信息：{exc}"
    except OSError as exc:
        return f"读取文件失败：{_rel_display(path)}。真实错误信息：{exc}"

    total = len(lines)
    if total == 0:
        return f"文件为空：{_rel_display(path)}"
    if start_line > total:
        return f"起始行 {start_line} 超出文件范围：{_rel_display(path)} 共 {total} 行。"

    selected = lines[start_line - 1: start_line - 1 + max_lines]
    end_line = start_line + len(selected) - 1
    has_more_lines = end_line < total

    header = f"文件：{_rel_display(path)}（共 {total} 行，显示第 {start_line}-{end_line} 行）"
    out = [header]
    chars = len(header) + 1
    truncated_by_size = False
    for i, raw in enumerate(selected):
        entry = f"{start_line + i}: {_clip(raw.rstrip(chr(10)), MAX_LINE_CHARS)}"
        if chars + len(entry) + 1 > MAX_RESULT_CHARS:
            truncated_by_size = True
            break
        out.append(entry)
        chars += len(entry) + 1

    last_shown = start_line + len(out) - 2
    if truncated_by_size:
        footer = f"（输出过长已截断，仅显示到第 {last_shown} 行；可增大 start_line 继续读取）"
    elif has_more_lines:
        footer = "（后面还有内容，可增大 start_line 继续读取）"
    else:
        footer = "（已到文件末尾）"
    return "\n".join(out) + "\n" + footer


# --- 工具 4：搜索项目代码（本项目新增） ---

@tool
def search_project_code(project_name: str, query: str) -> str:
    """在项目内按字面文字搜索，返回匹配的文件相对路径、行号和匹配行。

    参数：
      project_name —— 项目目录名称。
      query        —— 要查找的文字（按字面匹配，不是正则表达式）。
    优先使用 rg；rg 不可用时用 Python 文本搜索兜底。最多返回 30 条结果。
    """
    path, _root, err = _resolve_project_path(project_name, "")
    if err:
        return err
    if not query:
        return "搜索内容不能为空。"

    rg = shutil.which("rg")
    if rg:
        return _search_with_rg(rg, path, query)
    return _search_fallback(path, query)


def _search_with_rg(rg: str, project_path: str, query: str) -> str:
    cmd = [
        rg,
        "--line-number", "--no-heading", "--color", "never",
        "--fixed-strings",
        "--max-filesize", str(MAX_FILE_BYTES),
    ]
    for d in SKIP_DIRS:
        cmd += ["--glob", f"!{d}", "--glob", f"!{d}/**"]
    cmd += ["--", query, "."]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            cwd=str(project_path), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "搜索超时（15 秒），请缩小范围或换更具体的关键词。"
    except OSError as exc:
        return f"调用 rg 失败：{exc}"

    if proc.returncode == 1:
        return f"未找到与「{query}」匹配的内容。"
    if proc.returncode != 0:
        return f"搜索出错（rg 返回码 {proc.returncode}）：{proc.stderr.strip()}"

    matches = []
    for line in proc.stdout.splitlines():
        rel_path, sep1, rest = line.partition(":")
        line_no, sep2, content = rest.partition(":")
        if not sep1 or not sep2:
            continue
        if _has_skipped_dir(rel_path):
            continue
        if _is_sensitive(os.path.basename(rel_path)):
            continue
        if not _is_text_file(rel_path):
            continue
        matches.append((rel_path, line_no, content))

    return _format_search_matches(matches, query)


def _search_fallback(project_path: str, query: str) -> str:
    matches = []
    scanned = 0
    hit_limit = False
    for root, dirs, files in os.walk(project_path):
        kept = []
        for d in dirs:
            if d in SKIP_DIRS:
                continue
            real_dir = os.path.realpath(os.path.join(root, d))
            if not fsutil._is_within(real_dir, project_path):
                continue
            if _has_skipped_dir(os.path.relpath(real_dir, project_path)):
                continue
            kept.append(d)
        dirs[:] = kept

        for name in files:
            if scanned >= MAX_FALLBACK_FILES:
                hit_limit = True
                break
            if _is_sensitive(name) or not _is_text_file(name):
                continue
            full = os.path.join(root, name)
            real_file = os.path.realpath(full)
            if not fsutil._is_within(real_file, project_path):
                continue
            if _has_skipped_dir(os.path.relpath(real_file, project_path)):
                continue
            if _is_sensitive(os.path.basename(real_file)) or not _is_text_file(real_file):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if query in line:
                            rel = os.path.relpath(full, project_path).replace("\\", "/")
                            matches.append((rel, str(i), line.rstrip("\n")))
            except OSError:
                continue
            scanned += 1
        if hit_limit:
            break

    if not matches:
        if hit_limit:
            return (
                f"搜索未完成：已扫描 {scanned} 个文件达到上限，在已扫描文件中未找到"
                f"「{query}」。请换更具体的关键词或缩小搜索范围。"
            )
        return f"未找到与「{query}」匹配的内容。"
    return _format_search_matches(matches, query, scan_truncated=hit_limit)


def _format_search_matches(matches, query: str, scan_truncated: bool = False) -> str:
    if not matches:
        return f"未找到与「{query}」匹配的内容。"

    shown = matches[:MAX_SEARCH_RESULTS]
    lines = [f"搜索「{query}」共 {len(matches)} 条匹配："]
    chars = len(lines[0]) + 1
    count_truncated = len(matches) > MAX_SEARCH_RESULTS
    count = 0
    for rel_path, line_no, content in shown:
        entry = f"{rel_path}:{line_no}: {_clip(content, MAX_LINE_CHARS)}"
        if chars + len(entry) + 1 > MAX_RESULT_CHARS:
            count_truncated = True
            break
        lines.append(entry)
        chars += len(entry) + 1
        count += 1

    notes = []
    if count_truncated:
        notes.append(f"结果过多或输出过长，仅显示前 {count} 条")
    if scan_truncated:
        notes.append(
            f"扫描达到上限（{MAX_FALLBACK_FILES} 个文件），后续文件未扫描，结果可能不完整"
        )
    if notes:
        lines.append("（" + "；".join(notes) + "）")
    return "\n".join(lines)


# --- 工具 5 / 6：提出修改（只登记，不落盘） ---

@tool
def create_project_file(project_name: str, file_path: str, content: str) -> str:
    """提出新建一个文本文件（不会立即写入，需用户确认）。

    参数：
      project_name —— 项目目录名称。
      file_path    —— 项目内的相对文件路径（目标必须尚不存在）。
      content      —— 新文件的完整文本内容。
    只生成待确认修改与差异，返回后请明确告诉用户「修改已提出，等待确认」。
    """
    change_id, diff, err = changes.propose_create(project_name, file_path, content)
    if err:
        return f"无法提出修改：{err}"
    return (
        f"修改已提出，等待用户确认（change_id: {change_id}）。\n"
        f"目标：新建 {project_name}/{file_path}\n"
        f"差异：\n{_clip(diff, 4000)}"
    )


@tool
def replace_project_text(project_name: str, file_path: str, old_text: str, new_text: str) -> str:
    """提出对现有文件做一次精确文本替换（不会立即写入，需用户确认）。

    参数：
      project_name —— 项目目录名称。
      file_path    —— 项目内的相对文件路径。
      old_text     —— 要被替换的原文（必须非空，且在文件中恰好出现一次）。
      new_text     —— 替换后的文本。
    old_text 找不到或出现多次会报错，请先用 read_project_file 读取补充上下文。
    """
    change_id, diff, err = changes.propose_replace(project_name, file_path, old_text, new_text)
    if err:
        return f"无法提出修改：{err}"
    return (
        f"修改已提出，等待用户确认（change_id: {change_id}）。\n"
        f"目标：替换 {project_name}/{file_path}\n"
        f"差异：\n{_clip(diff, 4000)}"
    )


# --- 工具 7：运行检查（本项目新增） ---

@tool
def run_project_check(project_name: str, check_type: str) -> str:
    """运行项目检查。

    参数：
      project_name —— 项目目录名称。
      check_type   —— 仅支持 "python_syntax"（语法检查，不执行代码）或 "pytest"（会执行项目代码）。
    pytest 首次执行某项目前需要用户在网页确认授权。
    """
    real_root, _root, err = _resolve_project_path(project_name, "")
    if err:
        return err
    if check_type not in ("python_syntax", "pytest"):
        return f"不支持的检查类型：{check_type!r}，仅支持 python_syntax 或 pytest。"

    if check_type == "pytest":
        if not changes.require_pytest_auth(project_name):
            return (
                "pytest 会执行项目代码，首次运行需要用户授权，已记录待确认。"
                "请等待用户在网页上确认后再继续。"
            )
        return _run_pytest(project_name, real_root)

    return _run_syntax_check(real_root)


def _collect_py_files(real_root: str):
    """收集项目内允许检查的 .py 文件（跳过排除目录、敏感文件、超限文件、外部链接）。"""
    out = []
    for root, dirs, files in os.walk(real_root):
        kept = []
        for d in dirs:
            if d in SKIP_DIRS:
                continue
            if not fsutil._is_within(os.path.realpath(os.path.join(root, d)), real_root):
                continue
            kept.append(d)
        dirs[:] = kept
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            real = os.path.realpath(full)
            if not fsutil._is_within(real, real_root):
                continue
            if _has_skipped_dir(os.path.relpath(real, real_root)):
                continue
            if _is_sensitive(os.path.basename(real)):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, real_root).replace("\\", "/")
            out.append((rel, full))
    return out


def _run_syntax_check(real_root: str) -> str:
    """用 compile() 检查 .py 语法：不导入、不执行、不生成 pyc。"""
    checked = 0
    errors = []
    for rel, full in _collect_py_files(real_root):
        try:
            with open(full, "rb") as f:
                data = f.read()
            if b"\x00" in data[:1024]:
                continue
            source = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        checked += 1
        try:
            compile(source, rel, "exec")
        except SyntaxError as exc:
            errors.append(f"{rel}:{exc.lineno}: {exc.msg}")

    if errors:
        changes.note_check_failure()
        shown = errors[:30]
        head = f"语法检查：共检查 {checked} 个 .py 文件，发现 {len(errors)} 处错误："
        return head + "\n" + "\n".join(shown)
    changes.note_check_success()
    return f"语法检查：共检查 {checked} 个 .py 文件，未发现语法错误。"


def _find_python(real_root: str):
    """返回经过校验的项目虚拟环境 Python；没有则用助手运行时 sys.executable。"""
    for venv in (".venv", "venv"):
        for sub in ("Scripts/python.exe", "bin/python"):
            p = os.path.join(real_root, venv, sub)
            if os.path.isfile(p):
                if fsutil._is_within(os.path.realpath(p), real_root):
                    return p
    return sys.executable


def _child_env() -> dict:
    """最小必要环境变量，绝不继承 DeepSeek/Anthropic 等密钥。"""
    keep = {
        "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    }
    return {k: v for k, v in os.environ.items() if k in keep}


def _run_subprocess(cmd, cwd, env, timeout=PYTEST_TIMEOUT):
    """用参数列表、shell=False 运行子进程，设置超时并尽力清理进程树。"""
    kwargs = dict(
        cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return None, "", f"启动失败：{exc}", False, False
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        # 先杀整棵进程树（含派生进程），避免「先杀父进程后 taskkill 找不到根」。
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        # 限制收集残留输出的等待时间，避免被子进程持有的管道阻塞到自然结束。
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", "（超时，进程树清理中）"
    missing_pytest = "No module named pytest" in (err or "") or "No module named pytest" in (out or "")
    return proc.returncode, out, err, timed_out, missing_pytest


def _run_pytest(project_name: str, real_root: str) -> str:
    python = _find_python(real_root)
    if python is None:
        return "未找到可用的 Python 解释器。"
    env = _child_env()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    start = time.time()
    code, out, err, timed_out, missing = _run_subprocess([python, "-m", "pytest"], real_root, env)
    elapsed = round(time.time() - start, 2)

    if missing:
        return "缺少 pytest 依赖：项目未安装 pytest，且不会自动安装。请在项目虚拟环境中安装后再试。"
    if timed_out:
        return f"pytest 执行超时（{PYTEST_TIMEOUT} 秒），已终止。"
    if code is None:
        return f"pytest 启动失败：{_clip(err or '', 500)}"

    if code == 0:
        changes.note_check_success()
        return f"pytest：全部通过（退出码 0，耗时 {elapsed}s）。"
    if code == 5:
        return f"pytest：未收集到任何测试（退出码 5，不算通过）。请确认项目有可发现的测试。"
    if code == 1:
        changes.note_check_failure()
        summary = _clip((out or err or "").strip(), MAX_RESULT_CHARS)
        return f"pytest：测试失败（退出码 1，耗时 {elapsed}s）。\n{summary}"
    summary = _clip((out or err or "").strip(), MAX_RESULT_CHARS)
    return f"pytest：退出码 {code}（耗时 {elapsed}s）。\n{summary}"
