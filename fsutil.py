"""共享的路径与文件校验工具。

读取、搜索、提出修改、应用修改、运行检查都复用这里的同一套规则，
避免在多处复制出不一致的边界检查。
"""
import os

# 需要跳过的目录：虚拟环境、依赖、git 元数据、缓存，以及助手自己的备份目录。
BACKUP_DIR_NAME = ".agent_backups"
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", BACKUP_DIR_NAME,
}

# 允许读取/搜索/写入的常见代码与文本格式（按扩展名）。
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".html", ".css", ".md", ".txt",
    ".json", ".toml", ".yaml", ".yml",
}

# 单个文件的大小上限（字节）。
MAX_FILE_BYTES = 1_000_000

# 单行最大字符数，超出截断。
MAX_LINE_CHARS = 400

# 单次工具返回的最大字符数。
MAX_RESULT_CHARS = 20_000


def _projects_base() -> str:
    """返回 PROJECTS_DIR 的真实绝对路径（解析符号链接）。"""
    return os.path.realpath(os.getenv("PROJECTS_DIR", r"D:\Projects"))


def _assistant_dir() -> str:
    """返回助手项目自身所在的目录（用于放置备份目录等）。"""
    return os.path.dirname(os.path.abspath(__file__))


def _backup_root() -> str:
    """返回助手目录下独立的备份根目录。"""
    return os.path.join(_assistant_dir(), BACKUP_DIR_NAME)


def _is_within(path: str, parent: str) -> bool:
    """判断 path 是否等于 parent 或位于 parent 之内。

    基于解析后的真实路径比较（os.path.commonpath），不是字符串前缀比较，
    从而能拒绝「越界的 .. 路径」和「指向外部的符号链接/junction」。
    """
    try:
        return os.path.commonpath(
            [os.path.normcase(path), os.path.normcase(parent)]
        ) == os.path.normcase(parent)
    except ValueError:
        # 不同盘符等无法比较的情况，一律判为不在范围内（更安全）。
        return False


def _resolve_project_path(project_name: str, relative_path: str = ""):
    """把「项目名 + 项目内相对路径」解析为真实绝对路径，并做边界校验。

    返回 (真实路径, 项目根目录, 错误信息)；出错时前两个元素为 None。

    规则：
      1. project_name 必须是 PROJECTS_DIR 的直接子目录名称。
      2. 用 realpath 解析符号链接/junction，指向项目外则拒绝。
      3. relative_path 不能是绝对路径；解析后不能越出项目目录。
    """
    base = _projects_base()
    if not os.path.isdir(base):
        return None, None, f"项目根目录不存在：{base}。请检查 .env 中 PROJECTS_DIR 的配置。"

    if (
        not project_name
        or project_name in (".", "..")
        or os.path.basename(project_name) != project_name
    ):
        return None, None, (
            f"项目名称不合法：{project_name!r}。必须是 PROJECTS_DIR 下的直接子目录名称。"
        )

    project_root = os.path.join(base, project_name)
    real_root = os.path.realpath(project_root)
    if not os.path.isdir(real_root):
        return None, None, f"项目不存在：{project_name}（在 {base} 下没有这个目录）。"
    if not _is_within(real_root, base):
        return None, None, f"拒绝访问：项目 {project_name} 指向了 PROJECTS_DIR 之外的位置。"

    if relative_path in ("", ".", None):
        return real_root, real_root, None

    if os.path.isabs(relative_path):
        return None, None, f"拒绝访问：不允许绝对路径（{relative_path}）。"

    candidate = os.path.join(real_root, relative_path)
    real_candidate = os.path.realpath(candidate)
    if not _is_within(real_candidate, real_root):
        return None, None, f"拒绝访问：路径越界或指向项目之外（{relative_path}）。"
    return real_candidate, real_root, None


def _rel_display(path: str) -> str:
    """把绝对路径转成相对 PROJECTS_DIR 的展示路径（正斜杠，便于阅读）。"""
    rel = os.path.relpath(path, _projects_base())
    return rel.replace("\\", "/")


def _has_skipped_dir(rel_path: str) -> bool:
    """判断相对路径是否经过任一被排除的目录。"""
    parts = rel_path.replace("\\", "/").split("/")
    return any(p in SKIP_DIRS for p in parts)


def _is_sensitive(name: str) -> bool:
    """判断文件名是否属于敏感文件（.env、私钥、证书、凭据等）。"""
    low = name.lower()
    if low == ".env" or low.startswith(".env."):
        return True
    if os.path.splitext(low)[1] in {".pem", ".key", ".p12", ".pfx"}:
        return True
    markers = (
        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
        "private_key", "private-key", "credentials", "credential",
        "secret", "password", "token", "access_key", "accesskey",
    )
    return any(m in low for m in markers)


def _is_text_file(name: str) -> bool:
    """判断是否属于允许处理的常见代码/文本格式（按扩展名）。"""
    return os.path.splitext(name)[1].lower() in TEXT_EXTENSIONS


def _clip(text: str, limit: int) -> str:
    """把单行文本截断到 limit 字符，超出时加省略标记。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "…[已截断]"


def _policy_check(real_path: str, real_root: str):
    """对解析后的真实路径做「排除目录 + 敏感文件 + 文本扩展名」检查。

    返回错误信息字符串；通过则返回 None。读取、搜索、写入共用。
    """
    if _has_skipped_dir(os.path.relpath(real_path, real_root)):
        return (
            f"拒绝访问：{_rel_display(real_path)} 位于被排除的目录"
            f"（.git/.venv/node_modules/{BACKUP_DIR_NAME} 等）。"
        )
    if _is_sensitive(os.path.basename(real_path)):
        return f"拒绝访问：{_rel_display(real_path)} 属于敏感文件（.env、密钥或凭据）。"
    if not _is_text_file(real_path):
        ext = os.path.splitext(real_path)[1] or "（无扩展名）"
        return f"拒绝访问：{_rel_display(real_path)} 的类型 {ext} 不在允许的文本格式范围内。"
    return None


def _validate_write_target(project_name: str, file_path: str):
    """解析并校验一个「写」目标路径，返回 (真实路径, 项目根目录, 错误信息)。

    与读取/搜索同一套规则：项目边界 + 排除目录 + 敏感文件 + 文本扩展名。
    """
    real_path, real_root, err = _resolve_project_path(project_name, file_path)
    if err:
        return None, None, err
    perr = _policy_check(real_path, real_root)
    if perr:
        return None, None, perr
    return real_path, real_root, None
