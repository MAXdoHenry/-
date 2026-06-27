"""
医联体分级疑难病例会诊系统（Flask 纯网页版 Demo）
=================================================

【运行 & 部署说明】

一、部署端（只需要 1 台服务器/电脑做一次）
1. 安装唯一依赖：
   pip install flask

2. 运行系统：
   python app.py

3. 第一次运行会自动完成：
   - 创建 SQLite 数据库文件：hospital_consult.db
   - 自动建表：hospitals / users / consultations
   - 自动插入演示医院、医生和示例提问

4. 局域网访问：
   本程序默认监听 0.0.0.0:5000。
   同一网络内的其他电脑/手机可以访问：
   http://部署机器IP:5000

二、用户端（所有医院医生，零安装）
1. 不用装任何软件
2. 不用配置任何环境
3. 不用下载客户端或 App
4. 打开电脑/手机浏览器，输入部署机器的 IP:5000 就能用
5. 支持 Chrome、Edge、微信内置浏览器、手机浏览器等主流浏览器

演示账号：
- 医生姓名：张主任（市中心医院·心内科） / 李医生（区一医院·心内科）
          王医生（区二医院·骨科） / 赵医生（社区卫生服务中心·全科）
- 密码均为：123456
"""

from __future__ import annotations

import functools
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from flask import (
        Flask,
        abort,
        flash,
        redirect,
        render_template,
        request,
        send_from_directory,
        session,
        url_for,
    )
except ModuleNotFoundError:
    # 这样做是为了让没有安装 Flask 的环境也能做语法检查和核心数据库函数测试。
    # 真正运行网页时，部署端仍然需要先执行：pip install flask
    Flask = None  # type: ignore[assignment]


# =============================
# 全局配置
# =============================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hospital_consult.db"
UPLOAD_DIR = BASE_DIR / "uploads"
TIME_FORMAT = "%Y-%m-%d %H:%M"
ALLOWED_ATTACHMENT_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "pdf",
    "mp4",
    "mov",
    "avi",
    "webm",
}

# visible_levels 字段中只保存等级数字，页面展示时统一用这里的名称。
LEVEL_LABELS = {
    1: "三甲医院",
    2: "二甲医院",
    3: "社区医院",
}


def now_text() -> str:
    """返回统一格式的当前时间文本。"""
    return datetime.now().strftime(TIME_FORMAT)


def clean_filename(filename: str) -> str:
    """
    清理上传文件名，避免路径穿越和特殊字符问题。

    文件保存时会再加 UUID，原始文件名仍会保存到数据库用于下载展示。
    """
    safe_name = Path(filename or "").name
    safe_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", safe_name).strip("._")
    return safe_name or "attachment"


def allowed_attachment(filename: str) -> bool:
    """判断附件扩展名是否允许上传。"""
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_ATTACHMENT_EXTENSIONS


def save_attachment(uploaded_file) -> Dict[str, str]:
    """
    保存上传附件。

    返回值包含：
    - path：服务端保存文件名，用于后续下载路由定位
    - name：用户上传时的原始文件名，用于页面展示
    """
    if not uploaded_file or not uploaded_file.filename:
        return {"path": "", "name": ""}

    original_name = clean_filename(uploaded_file.filename)
    if not allowed_attachment(original_name):
        raise ValueError("附件格式不支持，请上传图片、视频或 PDF 文件")

    UPLOAD_DIR.mkdir(exist_ok=True)
    ext = original_name.rsplit(".", 1)[1].lower()
    saved_name = f"{uuid.uuid4().hex}.{ext}"
    uploaded_file.save(UPLOAD_DIR / saved_name)
    return {"path": saved_name, "name": original_name}


def is_image_file(filename: str) -> bool:
    """模板中使用：判断附件是否为图片。"""
    return filename.lower().rsplit(".", 1)[-1] in {"png", "jpg", "jpeg", "gif", "webp"}


def is_video_file(filename: str) -> bool:
    """模板中使用：判断附件是否为视频。"""
    return filename.lower().rsplit(".", 1)[-1] in {"mp4", "mov", "avi", "webm"}


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    创建 SQLite 数据库连接。

    sqlite3.Row 可以让查询结果既像元组又像字典：
    row["name"] 这种写法对新手更直观。
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_database(db_path: Optional[Path] = None) -> None:
    """
    初始化数据库。

    Flask 开发模式下可能会重启进程，页面访问也会多次调用数据库。
    因此这里使用 IF NOT EXISTS 和 INSERT OR IGNORE，
    确保重复执行也不会重复建表、重复插入演示数据。
    """
    UPLOAD_DIR.mkdir(exist_ok=True)

    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    parent_id INTEGER,
                    path TEXT,
                    level INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    password TEXT,
                    hospital_id INTEGER,
                    department TEXT,
                    role TEXT DEFAULT '医生'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consultations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    content TEXT,
                    asker_id INTEGER,
                    asker_hospital_id INTEGER,
                    asker_department TEXT,
                    target_hospital_id INTEGER,
                    visible_levels TEXT,
                    status TEXT,
                    reply_content TEXT,
                    replier_id INTEGER,
                    replier_department TEXT,
                    attachment_path TEXT,
                    attachment_name TEXT,
                    reply_attachment_path TEXT,
                    reply_attachment_name TEXT,
                    create_time TEXT,
                    reply_time TEXT
                )
                """
            )

            # 兼容旧数据库：旧版 consultations 表没有 visible_levels 字段。
            # 程序启动时自动补字段，用户不需要手动改库或删库。
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(consultations)").fetchall()
            ]
            if "visible_levels" not in columns:
                conn.execute(
                    "ALTER TABLE consultations ADD COLUMN visible_levels TEXT DEFAULT ''"
                )
            for column_name in [
                "attachment_path",
                "attachment_name",
                "reply_attachment_path",
                "reply_attachment_name",
            ]:
                if column_name not in columns:
                    conn.execute(
                        f"ALTER TABLE consultations ADD COLUMN {column_name} TEXT DEFAULT ''"
                    )

            hospitals = [
                (1, "市中心医院（三甲）", 0, "/1", 1),
                (2, "区第一人民医院（二甲）", 1, "/1/2", 2),
                (3, "区第二人民医院（二甲）", 1, "/1/3", 2),
                (4, "街道社区卫生服务中心", 2, "/1/2/4", 3),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO hospitals (id, name, parent_id, path, level)
                VALUES (?, ?, ?, ?, ?)
                """,
                hospitals,
            )

            users = [
                (1, "张主任", "123456", 1, "心内科", "医生"),
                (2, "李医生", "123456", 2, "心内科", "医生"),
                (3, "王医生", "123456", 3, "骨科", "医生"),
                (4, "赵医生", "123456", 4, "全科", "医生"),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO users
                    (id, name, password, hospital_id, department, role)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                users,
            )

            sample_consultations = [
                (
                    1,
                    "老年高血压持续不降",
                    "患者 76 岁，高血压病史 20 年，近期规律服药后血压仍在 170/95 mmHg 左右，伴头晕、乏力。社区已进行基础评估，希望上级医院协助判断是否需要调整降压方案。",
                    4,
                    4,
                    "全科",
                    2,
                    "2,1",
                    "待回复",
                    "",
                    None,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "2026-06-25 09:10",
                    "",
                ),
                (
                    2,
                    "心梗术后康复方案",
                    "患者急性心梗 PCI 术后 2 周，目前生命体征平稳，但活动耐量较差。想请市中心医院指导后续康复训练强度、复查计划和用药随访重点。",
                    2,
                    2,
                    "心内科",
                    1,
                    "1",
                    "已回复",
                    "建议继续规范双联抗血小板、他汀及二级预防用药。康复训练从低强度步行开始，逐步增加活动量，2-4 周后复查心电图、心超及血脂指标。",
                    1,
                    "心内科",
                    "",
                    "",
                    "",
                    "",
                    "2026-06-25 10:20",
                    "2026-06-25 11:05",
                ),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO consultations (
                    id, title, content, asker_id, asker_hospital_id,
                    asker_department, target_hospital_id, visible_levels, status, reply_content,
                    replier_id, replier_department,
                    attachment_path, attachment_name, reply_attachment_path, reply_attachment_name,
                    create_time, reply_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                sample_consultations,
            )

            # 兼容旧数据：按提问医院等级补齐 visible_levels，保持旧版默认权限效果。
            # 社区医院提问默认二甲+三甲可见；二甲医院提问默认三甲可见。
            conn.execute(
                """
                UPDATE consultations
                SET visible_levels = CASE (
                    SELECT level FROM hospitals
                    WHERE hospitals.id = consultations.asker_hospital_id
                )
                    WHEN 3 THEN '2,1'
                    WHEN 2 THEN '1'
                    ELSE ''
                END
                WHERE visible_levels IS NULL OR visible_levels = ''
                """
            )
    finally:
        conn.close()


# =============================
# 查询与权限函数
# =============================


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """把 sqlite3.Row 转成普通字典，方便保存到 Flask session。"""
    if row is None:
        return None
    return dict(row)


def get_user_with_hospital(user_id: int) -> Optional[Dict[str, Any]]:
    """根据用户 id 查询医生信息和所属医院信息。"""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                u.id AS id,
                u.name AS name,
                u.department AS department,
                u.role AS role,
                h.id AS hospital_id,
                h.name AS hospital_name,
                h.path AS hospital_path,
                h.level AS hospital_level
            FROM users u
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


def authenticate_user(name: str, password: str) -> Optional[Dict[str, Any]]:
    """
    演示版登录校验。

    真实系统不应保存明文密码；这里按需求使用 123456 明文密码，
    方便现场演示和新手理解。
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id
            FROM users
            WHERE name = ? AND password = ?
            """,
            (name, password),
        ).fetchone()
        if row is None:
            return None
        return get_user_with_hospital(row["id"])
    finally:
        conn.close()


def get_direct_parent_hospital(hospital_id: int) -> Optional[sqlite3.Row]:
    """查询当前医院的直接上级医院，不允许跨级选择。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT parent.*
            FROM hospitals child
            JOIN hospitals parent ON child.parent_id = parent.id
            WHERE child.id = ? AND child.parent_id != 0
            """,
            (hospital_id,),
        ).fetchone()
    finally:
        conn.close()


def get_available_target_hospitals(current_user: Dict[str, Any]) -> List[sqlite3.Row]:
    """
    发起提问页使用：可向同级、上级或跨级医院提问。

    只排除当前用户自己的医院，避免“向本院请教”这种演示上不清晰的情况。
    """
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT *
            FROM hospitals
            WHERE id != ?
            ORDER BY level, id
            """,
            (current_user["hospital_id"],),
        ).fetchall()
    finally:
        conn.close()


def is_descendant_path(child_path: str, parent_path: str) -> bool:
    """
    判断 child_path 是否是 parent_path 的下级路径。

    注意这里使用 parent_path + '/%' 的思想，排除“同一家医院”。
    例如 /1/2 是 /1 的下级，但 /1 不是 /1 自己的下级。
    这样可以严格满足：只能看自己发的 + 下级医院发的。
    """
    normalized_parent = parent_path.rstrip("/")
    return child_path.startswith(normalized_parent + "/")


def get_visible_level_options() -> List[Dict[str, Any]]:
    """
    发起提问页使用：列出所有可设置的医院等级。

    现在支持同级、跨级提问，因此可见等级不再只限制为“更高级别”。
    """
    return [
        {"value": level, "label": LEVEL_LABELS[level]}
        for level in sorted(LEVEL_LABELS)
    ]


def get_default_visible_levels(current_level: int) -> List[int]:
    """
    默认勾选除自己等级外的其他等级。

    如果要让同级医院也都能看到，用户可以手动勾选自己的等级。
    指定的请教医院即使是同级，也会因为 target_hospital_id 获得查看和回复权限。
    """
    return [level for level in sorted(LEVEL_LABELS) if level != current_level]


def normalize_visible_levels(level_values: List[str], current_level: int) -> str:
    """
    把表单提交的可见等级转换成逗号分隔字符串。

    这里只允许保存系统已定义的等级，防止用户改浏览器表单后写入异常数据。
    """
    allowed_levels = {str(item["value"]) for item in get_visible_level_options()}
    selected_levels = [value for value in level_values if value in allowed_levels]
    return ",".join(selected_levels)


def level_is_allowed(visible_levels: str, hospital_level: int) -> bool:
    """判断当前医院等级是否在某条提问的 visible_levels 里。"""
    levels = {level.strip() for level in (visible_levels or "").split(",") if level.strip()}
    return str(hospital_level) in levels


def format_visible_level_names(visible_levels: str) -> str:
    """把 visible_levels='2,1' 转成页面展示文案：二甲医院、三甲医院。"""
    names = []
    for value in (visible_levels or "").split(","):
        value = value.strip()
        if value.isdigit() and int(value) in LEVEL_LABELS:
            names.append(LEVEL_LABELS[int(value)])
    return "、".join(names) if names else "仅提问人本人"


def can_view_consultation(consultation: sqlite3.Row, current_user: Dict[str, Any]) -> bool:
    """
    详情页统一权限校验。

    可查看条件：
    1. 当前用户是提问人本人
    2. 当前用户所属医院就是指定请教医院
    3. 当前用户所属医院等级在提问者设置的 visible_levels 中
    """
    if consultation["asker_id"] == current_user["id"]:
        return True
    if consultation["target_hospital_id"] == current_user["hospital_id"]:
        return True
    return level_is_allowed(consultation["visible_levels"], current_user["hospital_level"])


def can_reply_consultation(consultation: sqlite3.Row, current_user: Dict[str, Any]) -> bool:
    """只有被请教医院的医生，且状态为待回复时，才可以提交回复。"""
    return (
        consultation["status"] == "待回复"
        and consultation["target_hospital_id"] == current_user["hospital_id"]
    )


def get_subordinate_hospitals(current_user: Dict[str, Any]) -> List[sqlite3.Row]:
    """
    查询当前用户可见的提问来源医院，用于会诊提问列表的医院筛选。

    函数名沿用旧版，减少路由和模板的大范围改动；
    实际逻辑已从“下级医院”改为“visible_levels 允许当前等级查看的医院”。
    """
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT DISTINCT h.*
            FROM consultations c
            JOIN hospitals h ON c.asker_hospital_id = h.id
            WHERE c.asker_id != ?
              AND (
                  (',' || COALESCE(c.visible_levels, '') || ',') LIKE ?
                  OR c.target_hospital_id = ?
              )
            ORDER BY h.level, h.id
            """,
            (
                current_user["id"],
                f"%,{current_user['hospital_level']},%",
                current_user["hospital_id"],
            ),
        ).fetchall()
    finally:
        conn.close()


def get_my_consultations(user_id: int) -> List[sqlite3.Row]:
    """我的提问：严格只查询当前用户自己发起的记录。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT c.*, target_h.name AS target_hospital_name
            FROM consultations c
            JOIN hospitals target_h ON c.target_hospital_id = target_h.id
            WHERE c.asker_id = ?
            ORDER BY c.create_time DESC, c.id DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def get_subordinate_consultations(
    current_user: Dict[str, Any],
    hospital_id: Optional[int] = None,
    status: str = "全部",
    limit: Optional[int] = None,
) -> List[sqlite3.Row]:
    """查询当前用户等级可见的会诊提问，支持医院和状态筛选。"""
    params: List[Any] = [
        current_user["id"],
        f"%,{current_user['hospital_level']},%",
        current_user["hospital_id"],
    ]
    where_parts = [
        "c.asker_id != ?",
        "((',' || COALESCE(c.visible_levels, '') || ',') LIKE ? OR c.target_hospital_id = ?)",
    ]

    if hospital_id is not None:
        where_parts.append("asker_h.id = ?")
        params.append(hospital_id)

    if status in ("待回复", "已回复"):
        where_parts.append("c.status = ?")
        params.append(status)

    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)

    conn = get_connection()
    try:
        return conn.execute(
            f"""
            SELECT
                c.*,
                asker_u.name AS asker_name,
                asker_h.name AS asker_hospital_name,
                asker_h.path AS asker_hospital_path
            FROM consultations c
            JOIN users asker_u ON c.asker_id = asker_u.id
            JOIN hospitals asker_h ON c.asker_hospital_id = asker_h.id
            WHERE {" AND ".join(where_parts)}
            ORDER BY c.create_time DESC, c.id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def get_visible_latest_consultations(current_user: Dict[str, Any]) -> List[sqlite3.Row]:
    """首页最新 5 条：当前用户自己的提问 + 当前等级被允许查看的提问。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                c.*,
                asker_u.name AS asker_name,
                asker_h.name AS asker_hospital_name,
                asker_h.path AS asker_hospital_path
            FROM consultations c
            JOIN users asker_u ON c.asker_id = asker_u.id
            JOIN hospitals asker_h ON c.asker_hospital_id = asker_h.id
            WHERE c.asker_id = ?
               OR (',' || COALESCE(c.visible_levels, '') || ',') LIKE ?
               OR c.target_hospital_id = ?
            ORDER BY c.create_time DESC, c.id DESC
            LIMIT 5
            """,
            (
                current_user["id"],
                f"%,{current_user['hospital_level']},%",
                current_user["hospital_id"],
            ),
        ).fetchall()
    finally:
        conn.close()


def get_consultation_detail(consultation_id: int) -> Optional[sqlite3.Row]:
    """查询详情页需要展示的完整提问、医院、医生信息。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                c.*,
                asker_u.name AS asker_name,
                asker_h.name AS asker_hospital_name,
                asker_h.path AS asker_hospital_path,
                target_h.name AS target_hospital_name,
                replier_u.name AS replier_name
            FROM consultations c
            JOIN users asker_u ON c.asker_id = asker_u.id
            JOIN hospitals asker_h ON c.asker_hospital_id = asker_h.id
            JOIN hospitals target_h ON c.target_hospital_id = target_h.id
            LEFT JOIN users replier_u ON c.replier_id = replier_u.id
            WHERE c.id = ?
            """,
            (consultation_id,),
        ).fetchone()
    finally:
        conn.close()


def create_consultation(
    title: str,
    content: str,
    current_user: Dict[str, Any],
    target_hospital_id: int,
    visible_levels: str,
    attachment: Dict[str, str],
) -> None:
    """新增提问记录。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO consultations (
                    title, content, asker_id, asker_hospital_id,
                    asker_department, target_hospital_id, visible_levels, status,
                    reply_content, replier_id, replier_department,
                    attachment_path, attachment_name, reply_attachment_path, reply_attachment_name,
                    create_time, reply_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '待回复', '', NULL, '', ?, ?, '', '', ?, '')
                """,
                (
                    title,
                    content,
                    current_user["id"],
                    current_user["hospital_id"],
                    current_user["department"],
                    target_hospital_id,
                    visible_levels,
                    attachment["path"],
                    attachment["name"],
                    now_text(),
                ),
            )
    finally:
        conn.close()


def update_reply(
    consultation_id: int,
    reply_content: str,
    current_user: Dict[str, Any],
    reply_attachment: Dict[str, str],
) -> None:
    """提交回复，并更新状态、回复医生、回复科室和回复时间。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE consultations
                SET
                    status = '已回复',
                    reply_content = ?,
                    replier_id = ?,
                    replier_department = ?,
                    reply_attachment_path = ?,
                    reply_attachment_name = ?,
                    reply_time = ?
                WHERE id = ?
                """,
                (
                    reply_content,
                    current_user["id"],
                    current_user["department"],
                    reply_attachment["path"],
                    reply_attachment["name"],
                    now_text(),
                    consultation_id,
                ),
            )
    finally:
        conn.close()


def get_dashboard_stats(current_user: Dict[str, Any]) -> Dict[str, int]:
    """首页统计卡片数据。"""
    conn = get_connection()
    try:
        my_total = conn.execute(
            "SELECT COUNT(*) FROM consultations WHERE asker_id = ?",
            (current_user["id"],),
        ).fetchone()[0]
        my_pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM consultations
            WHERE asker_id = ? AND status = '待回复'
            """,
            (current_user["id"],),
        ).fetchone()[0]
        my_replied = conn.execute(
            """
            SELECT COUNT(*)
            FROM consultations
            WHERE asker_id = ? AND status = '已回复'
            """,
            (current_user["id"],),
        ).fetchone()[0]
        subordinate_pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM consultations c
            WHERE c.asker_id != ?
              AND (
                  (',' || COALESCE(c.visible_levels, '') || ',') LIKE ?
                  OR c.target_hospital_id = ?
              )
              AND c.status = '待回复'
            """,
            (
                current_user["id"],
                f"%,{current_user['hospital_level']},%",
                current_user["hospital_id"],
            ),
        ).fetchone()[0]
        return {
            "my_total": my_total,
            "my_pending": my_pending,
            "my_replied": my_replied,
            "subordinate_pending": subordinate_pending,
        }
    finally:
        conn.close()


def get_answer_notifications(current_user: Optional[Dict[str, Any]]) -> List[sqlite3.Row]:
    """
    查询“您的提问已被回答”通知。

    这是纯网页 Demo，不使用浏览器推送；每次用户打开页面时，
    顶部都会显示自己最近已回复的提问，达到演示通知效果。
    """
    if current_user is None:
        return []

    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT id, title, reply_time
            FROM consultations
            WHERE asker_id = ? AND status = '已回复'
            ORDER BY reply_time DESC, id DESC
            LIMIT 5
            """,
            (current_user["id"],),
        ).fetchall()
    finally:
        conn.close()


# =============================
# Flask 应用与路由
# =============================


app = Flask(__name__) if Flask is not None else None
if app is not None:
    # Demo 用固定密钥即可；真实系统应从环境变量读取。
    app.secret_key = "demo-secret-key-for-hospital-consult-system"


def get_current_user() -> Optional[Dict[str, Any]]:
    """从 session 中读取当前登录医生。"""
    if Flask is None:
        return None
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return get_user_with_hospital(int(user_id))


def login_required(view_func):
    """登录保护装饰器：未登录访问业务页面时自动跳回登录页。"""

    @functools.wraps(view_func)
    def wrapped_view(*args, **kwargs):
        current_user = get_current_user()
        if current_user is None:
            return redirect(url_for("login", next=request.path))
        return view_func(current_user, *args, **kwargs)

    return wrapped_view


if app is not None:

    @app.context_processor
    def inject_template_helpers() -> Dict[str, Any]:
        """
        给所有模板提供通用变量和函数。

        status_class 用来把“待回复/已回复”映射成 Bootstrap 标签颜色。
        """

        def status_class(status: str) -> str:
            return "text-bg-warning" if status == "待回复" else "text-bg-success"

        return {
            "current_user": get_current_user(),
            "status_class": status_class,
            "format_visible_level_names": format_visible_level_names,
            "answer_notifications": get_answer_notifications(get_current_user()),
            "is_image_file": is_image_file,
            "is_video_file": is_video_file,
        }

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """登录页：演示版只校验医生姓名和明文密码。"""
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "").strip()
            if not name or not password:
                flash("请输入医生姓名和密码", "danger")
                return render_template("login.html")

            user = authenticate_user(name, password)
            if user is None:
                flash("登录失败，请检查医生姓名或密码", "danger")
                return render_template("login.html")

            session.clear()
            session["user_id"] = user["id"]
            flash(f"欢迎登录，{user['name']}医生", "success")
            return redirect(request.args.get("next") or url_for("home"))

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        """退出登录。"""
        session.clear()
        flash("已退出登录", "success")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def home(current_user: Dict[str, Any]):
        """首页：欢迎语、统计卡片、最新 5 条可见提问。"""
        stats = get_dashboard_stats(current_user)
        latest_consultations = get_visible_latest_consultations(current_user)
        return render_template(
            "home.html",
            stats=stats,
            latest_consultations=latest_consultations,
        )

    @app.route("/consultations/new", methods=["GET", "POST"])
    @login_required
    def new_consultation(current_user: Dict[str, Any]):
        """发起提问：支持同级、上级和跨级医院。"""
        target_hospitals = get_available_target_hospitals(current_user)
        visible_level_options = get_visible_level_options()
        default_visible_levels = get_default_visible_levels(current_user["hospital_level"])

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            target_hospital_id = request.form.get("target_hospital_id", "").strip()
            visible_levels = normalize_visible_levels(
                request.form.getlist("visible_levels"),
                current_user["hospital_level"],
            )
            attachment = {"path": "", "name": ""}

            if not title:
                flash("标题不能为空", "danger")
                return render_template(
                    "new_consultation.html",
                    target_hospitals=target_hospitals,
                    visible_level_options=visible_level_options,
                    default_visible_levels=default_visible_levels,
                )
            if not content:
                flash("病情描述不能为空", "danger")
                return render_template(
                    "new_consultation.html",
                    target_hospitals=target_hospitals,
                    visible_level_options=visible_level_options,
                    default_visible_levels=default_visible_levels,
                )

            # 后端再次校验目标医院，防止用户改浏览器表单提交不存在或本院医院。
            allowed_target_ids = {hospital["id"] for hospital in target_hospitals}
            if not target_hospital_id.isdigit() or int(target_hospital_id) not in allowed_target_ids:
                flash("请选择有效的请教医院", "danger")
                return redirect(url_for("new_consultation"))

            try:
                attachment = save_attachment(request.files.get("attachment"))
            except ValueError as exc:
                flash(str(exc), "danger")
                return render_template(
                    "new_consultation.html",
                    target_hospitals=target_hospitals,
                    visible_level_options=visible_level_options,
                    default_visible_levels=default_visible_levels,
                )

            create_consultation(
                title=title,
                content=content,
                current_user=current_user,
                target_hospital_id=int(target_hospital_id),
                visible_levels=visible_levels,
                attachment=attachment,
            )
            flash("提问提交成功，等待对方医院回复", "success")
            return redirect(url_for("my_consultations"))

        return render_template(
            "new_consultation.html",
            target_hospitals=target_hospitals,
            visible_level_options=visible_level_options,
            default_visible_levels=default_visible_levels,
        )

    @app.route("/my-consultations")
    @login_required
    def my_consultations(current_user: Dict[str, Any]):
        """我的提问列表。"""
        consultations = get_my_consultations(current_user["id"])
        return render_template("my_consultations.html", consultations=consultations)

    @app.route("/subordinate-consultations")
    @login_required
    def subordinate_consultations(current_user: Dict[str, Any]):
        """会诊提问列表，仅 level 1 和 level 2 医院可访问。"""
        if current_user["hospital_level"] not in (1, 2):
            flash("您没有权限访问会诊提问列表", "danger")
            return redirect(url_for("home"))

        subordinate_hospitals = get_subordinate_hospitals(current_user)
        selected_hospital_id = request.args.get("hospital_id", "")
        selected_status = request.args.get("status", "全部")

        hospital_id: Optional[int] = None
        if selected_hospital_id:
            allowed_ids = {hospital["id"] for hospital in subordinate_hospitals}
            candidate_id = int(selected_hospital_id)
            if candidate_id in allowed_ids:
                hospital_id = candidate_id

        if selected_status not in ("全部", "待回复", "已回复"):
            selected_status = "全部"

        consultations = get_subordinate_consultations(
            current_user=current_user,
            hospital_id=hospital_id,
            status=selected_status,
        )
        return render_template(
            "subordinate_consultations.html",
            consultations=consultations,
            subordinate_hospitals=subordinate_hospitals,
            selected_hospital_id=str(hospital_id or ""),
            selected_status=selected_status,
        )

    @app.route("/consultations/<int:consultation_id>")
    @login_required
    def consultation_detail(current_user: Dict[str, Any], consultation_id: int):
        """提问详情页。"""
        consultation = get_consultation_detail(consultation_id)
        if consultation is None:
            abort(404)

        if not can_view_consultation(consultation, current_user):
            return render_template("detail.html", no_permission=True)

        return render_template(
            "detail.html",
            consultation=consultation,
            can_reply=can_reply_consultation(consultation, current_user),
            no_permission=False,
        )

    @app.route("/attachments/<path:filename>")
    @login_required
    def download_attachment(current_user: Dict[str, Any], filename: str):
        """下载附件：必须登录，并且必须对附件所属提问有查看权限。"""
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT id
                FROM consultations
                WHERE attachment_path = ? OR reply_attachment_path = ?
                """,
                (filename, filename),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            abort(404)

        consultation = get_consultation_detail(row["id"])
        if consultation is None or not can_view_consultation(consultation, current_user):
            abort(403)

        download_name = filename
        if consultation["attachment_path"] == filename and consultation["attachment_name"]:
            download_name = consultation["attachment_name"]
        if (
            consultation["reply_attachment_path"] == filename
            and consultation["reply_attachment_name"]
        ):
            download_name = consultation["reply_attachment_name"]

        return send_from_directory(
            UPLOAD_DIR,
            filename,
            as_attachment=True,
            download_name=download_name,
        )

    @app.route("/consultations/<int:consultation_id>/reply", methods=["POST"])
    @login_required
    def reply(current_user: Dict[str, Any], consultation_id: int):
        """提交回复。"""
        consultation = get_consultation_detail(consultation_id)
        if consultation is None:
            abort(404)

        if not can_view_consultation(consultation, current_user):
            flash("您没有权限查看该内容", "danger")
            return redirect(url_for("home"))

        if not can_reply_consultation(consultation, current_user):
            flash("您不是被请教医院医生，或该提问已回复", "danger")
            return redirect(url_for("consultation_detail", consultation_id=consultation_id))

        reply_content = request.form.get("reply_content", "").strip()
        if not reply_content:
            flash("回复内容不能为空", "danger")
            return redirect(url_for("consultation_detail", consultation_id=consultation_id))

        try:
            reply_attachment = save_attachment(request.files.get("reply_attachment"))
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("consultation_detail", consultation_id=consultation_id))

        update_reply(consultation_id, reply_content, current_user, reply_attachment)
        flash("回复提交成功", "success")
        return redirect(url_for("consultation_detail", consultation_id=consultation_id))


if __name__ == "__main__":
    init_database()
    if app is None:
        raise RuntimeError("未安装 Flask，请先运行：pip install flask")
    app.run(host="0.0.0.0", port=5000, debug=False)
