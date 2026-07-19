"""
医联体分级疑难病例会诊系统（Flask 纯网页版 Demo）
=================================================

功能亮点：
1. 原有会诊能力：登录、分级权限、病例提问、附件上传、上级回复、回复通知。
2. 通用好友体系：手机号/微信号搜索、好友申请、同意/拒绝、好友分组、实名资料。
3. 医疗聊天协作：一对一私聊、任意好友建群、病例讨论群、聊天发起病例会诊。
4. 医疗合规性：
   - 全实名展示：姓名 + 医院 + 科室 + 职称
   - 消息永久留痕：不提供删除和撤回
   - 操作全留痕：加好友、建群、发消息、退群等写入 operation_logs
   - 风险提示：涉及诊断、用药、治疗方案等内容提示“仅作临床参考”
5. 业务扩展性：
   - 好友分组预留科室同事、医联体专家、学术同行等管理方式
   - 群类型预留病例讨论、工作、学术交流、培训等场景
   - 专家库预留后续专家预约、远程会诊、培训授课扩展

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
import hmac
import hashlib
import json
import re
import secrets
import sqlite3
import time
import base64
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from flask import (
        Flask,
        abort,
        flash,
        jsonify,
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
CHAT_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 8
CHAT_SECRET_KEY = "demo-secret-key-for-hospital-consult-system"
SMS_CODE_TTL_SECONDS = 5 * 60
SMS_CODE_SCENES = {"login", "register"}
DEMO_SMS_CHANNELS = {"web", "wechat_mini_program"}
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

# 群聊类型。case 用作“病例聊天室”，通过 groups.consultation_id 绑定原有病例提问。
GROUP_TYPE_LABELS = {
    "case": "病例讨论群",
    "work": "科室工作群",
    "academic": "学术交流群",
    "training": "培训群",
}

# 医疗敏感词：命中后页面展示临床参考提示，并在发送前弹窗提醒。
SENSITIVE_TERMS = ["诊断", "用药", "治疗方案", "处方", "剂量", "手术", "转诊", "药物"]


def now_text() -> str:
    """返回统一格式的当前时间文本。"""
    return datetime.now().strftime(TIME_FORMAT)


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_chat_token(user_id: int) -> str:
    """为独立 WebSocket 服务签发轻量身份 token。"""
    payload = {
        "user_id": int(user_id),
        "iat": int(time.time()),
    }
    payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_part = _base64url_encode(payload_text.encode("utf-8"))
    signature = hmac.new(
        CHAT_SECRET_KEY.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_part}.{_base64url_encode(signature)}"


def verify_chat_token(token: str) -> Optional[int]:
    """校验聊天 token，返回用户 id。独立服务会复用同样逻辑。"""
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        CHAT_SECRET_KEY.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual = _base64url_decode(signature_part)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        payload = json.loads(_base64url_decode(payload_part).decode("utf-8"))
    except Exception:
        return None
    issued_at = int(payload.get("iat", 0))
    if issued_at <= 0 or time.time() - issued_at > CHAT_TOKEN_MAX_AGE_SECONDS:
        return None
    user_id = payload.get("user_id")
    return int(user_id) if isinstance(user_id, int) else None


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
        raise ValueError("附件格式不支持，请上传图片、视频或文档文件")

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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friend_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    group_name TEXT,
                    create_time TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS friend_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    friend_id INTEGER,
                    status TEXT,
                    apply_msg TEXT,
                    friend_group TEXT,
                    create_time TEXT,
                    update_time TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT,
                    group_type TEXT,
                    creator_id INTEGER,
                    create_time TEXT,
                    description TEXT,
                    consultation_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    user_id INTEGER,
                    join_time TEXT,
                    role TEXT,
                    last_read_time TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS private_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER,
                    receiver_id INTEGER,
                    content TEXT,
                    send_time TEXT,
                    is_read INTEGER DEFAULT 0,
                    is_deleted INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS group_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    sender_id INTEGER,
                    sender_name TEXT,
                    sender_hospital TEXT,
                    sender_department TEXT,
                    content TEXT,
                    send_time TEXT,
                    attachment_path TEXT,
                    attachment_name TEXT,
                    is_deleted INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    operation_type TEXT,
                    operation_content TEXT,
                    operation_time TEXT,
                    ip_address TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expert_library (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    expert_user_id INTEGER,
                    create_time TEXT,
                    note TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT,
                    code TEXT,
                    scene TEXT,
                    channel TEXT,
                    wechat_openid TEXT,
                    create_time TEXT,
                    expires_at INTEGER,
                    used_time TEXT,
                    request_ip TEXT
                )
                """
            )

            # 兼容旧数据库：给 users 表补充实名社交和专家扩展字段。
            user_columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            ]
            for column_name in [
                "phone",
                "wechat_id",
                "title",
                "avatar",
                "wechat_openid",
                "register_source",
            ]:
                if column_name not in user_columns:
                    conn.execute(
                        f"ALTER TABLE users ADD COLUMN {column_name} TEXT DEFAULT ''"
                    )

            # 兼容旧数据库：给 groups / group_members 补病例聊天室和未读能力字段。
            group_columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(groups)").fetchall()
            ]
            if "consultation_id" not in group_columns:
                conn.execute("ALTER TABLE groups ADD COLUMN consultation_id INTEGER")
            member_columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(group_members)").fetchall()
            ]
            if "last_read_time" not in member_columns:
                conn.execute(
                    "ALTER TABLE group_members ADD COLUMN last_read_time TEXT DEFAULT ''"
                )
            message_columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(group_messages)").fetchall()
            ]
            for column_name in ["attachment_path", "attachment_name"]:
                if column_name not in message_columns:
                    conn.execute(
                        f"ALTER TABLE group_messages ADD COLUMN {column_name} TEXT DEFAULT ''"
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
            # Demo 阶段为了方便老师和同学现场演示，手机号/微信号先用 1、2、3、4。
            # 正式系统再替换为真实手机号、微信号或医院统一身份标识。
            user_profiles = [
                ("1", "1", "主任医师", "张", 1),
                ("2", "2", "主治医师", "李", 2),
                ("3", "3", "主治医师", "王", 3),
                ("4", "4", "住院医师", "赵", 4),
            ]
            conn.executemany(
                """
                UPDATE users
                SET phone = ?, wechat_id = ?, title = ?, avatar = ?
                WHERE id = ?
                """,
                user_profiles,
            )

            friend_groups = [
                (1, 1, "我的好友", "2026-06-25 08:00"),
                (2, 1, "医联体专家", "2026-06-25 08:00"),
                (3, 2, "我的好友", "2026-06-25 08:00"),
                (4, 2, "科室同事", "2026-06-25 08:00"),
                (5, 3, "我的好友", "2026-06-25 08:00"),
                (6, 4, "我的好友", "2026-06-25 08:00"),
                (7, 4, "医联体专家", "2026-06-25 08:00"),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO friend_groups (id, user_id, group_name, create_time)
                VALUES (?, ?, ?, ?)
                """,
                friend_groups,
            )

            friend_relations = [
                (1, 1, 2, "accepted", "心内科会诊协作", "医联体专家", "2026-06-25 08:30", "2026-06-25 08:35"),
                (2, 2, 1, "accepted", "心内科会诊协作", "我的好友", "2026-06-25 08:30", "2026-06-25 08:35"),
                (3, 2, 4, "accepted", "社区与二甲转诊协作", "我的好友", "2026-06-25 08:40", "2026-06-25 08:45"),
                (4, 4, 2, "accepted", "社区与二甲转诊协作", "医联体专家", "2026-06-25 08:40", "2026-06-25 08:45"),
                (5, 4, 1, "pending", "我是街道社区卫生服务中心全科的赵医生，希望请教疑难病例。", "我的好友", "2026-06-25 09:00", "2026-06-25 09:00"),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO friend_relations (
                    id, user_id, friend_id, status, apply_msg, friend_group, create_time, update_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                friend_relations,
            )
            test_friend_groups = [
                (1200 + user_id, user_id, "群聊测试成员", "2026-06-25 10:00")
                for user_id in range(1, 5)
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO friend_groups (id, user_id, group_name, create_time)
                VALUES (?, ?, ?, ?)
                """,
                test_friend_groups,
            )
            test_friend_relations = []
            relation_id = 5000
            for base_user_id in range(1, 5):
                for test_user_id in range(101, 111):
                    test_friend_relations.append(
                        (
                            relation_id,
                            base_user_id,
                            test_user_id,
                            "accepted",
                            "群聊测试成员",
                            "群聊测试成员",
                            "2026-06-25 10:00",
                            "2026-06-25 10:00",
                        )
                    )
                    relation_id += 1
                    test_friend_relations.append(
                        (
                            relation_id,
                            test_user_id,
                            base_user_id,
                            "accepted",
                            "群聊测试成员",
                            "测试成员",
                            "2026-06-25 10:00",
                            "2026-06-25 10:00",
                        )
                    )
                    relation_id += 1
            conn.executemany(
                """
                INSERT OR IGNORE INTO friend_relations (
                    id, user_id, friend_id, status, apply_msg, friend_group, create_time, update_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                test_friend_relations,
            )

            demo_groups = [
                (1, "心内科病例讨论群", "case", 1, "2026-06-25 09:20", "心内科疑难病例讨论与会诊协作", 2),
                (2, "医联体工作协作群", "work", 2, "2026-06-25 09:30", "医联体日常工作沟通与转诊协作", None),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO groups (
                    id, group_name, group_type, creator_id, create_time, description, consultation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                demo_groups,
            )

            demo_members = [
                (1, 1, 1, "2026-06-25 09:20", "owner", "2026-06-25 09:40"),
                (2, 1, 2, "2026-06-25 09:20", "member", "2026-06-25 09:35"),
                (3, 1, 4, "2026-06-25 09:20", "member", "2026-06-25 09:30"),
                (4, 2, 2, "2026-06-25 09:30", "owner", "2026-06-25 09:45"),
                (5, 2, 4, "2026-06-25 09:30", "member", "2026-06-25 09:32"),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO group_members (
                    id, group_id, user_id, join_time, role, last_read_time
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                demo_members,
            )

            # 群聊测试数据：10 个测试医生 + 1 个多人测试群，方便压测成员列表和群消息。
            test_users = [
                (101, "测试医生01", "", 1, "心内科", "医生"),
                (102, "测试医生02", "", 2, "急诊科", "医生"),
                (103, "测试医生03", "", 3, "骨科", "医生"),
                (104, "测试医生04", "", 4, "全科", "医生"),
                (105, "测试医生05", "", 1, "影像科", "医生"),
                (106, "测试医生06", "", 2, "检验科", "医生"),
                (107, "测试医生07", "", 3, "呼吸科", "医生"),
                (108, "测试医生08", "", 4, "康复科", "医生"),
                (109, "测试医生09", "", 1, "神经内科", "医生"),
                (110, "测试医生10", "", 2, "药学部", "医生"),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO users
                    (id, name, password, hospital_id, department, role)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                test_users,
            )
            test_profiles = [
                ("18800001001", "test1001", "主治医师", "测1", 101),
                ("18800001002", "test1002", "主治医师", "测2", 102),
                ("18800001003", "test1003", "住院医师", "测3", 103),
                ("18800001004", "test1004", "住院医师", "测4", 104),
                ("18800001005", "test1005", "主管技师", "测5", 105),
                ("18800001006", "test1006", "主管技师", "测6", 106),
                ("18800001007", "test1007", "主治医师", "测7", 107),
                ("18800001008", "test1008", "康复治疗师", "测8", 108),
                ("18800001009", "test1009", "副主任医师", "测9", 109),
                ("18800001010", "test1010", "主管药师", "测10", 110),
            ]
            conn.executemany(
                """
                UPDATE users
                SET phone = ?, wechat_id = ?, title = ?, avatar = ?
                WHERE id = ?
                """,
                test_profiles,
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO friend_groups (id, user_id, group_name, create_time)
                VALUES (?, ?, '测试成员', '2026-06-25 10:00')
                """,
                [(1000 + user_id, user_id) for user_id in range(101, 111)],
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO groups (
                    id, group_name, group_type, creator_id, create_time, description, consultation_id
                )
                VALUES (20, '群聊测试群', 'work', 1, '2026-06-25 10:00', '用于测试多人群聊、成员管理和消息滚动。', NULL)
                """
            )
            test_group_members = [
                (2000, 20, 1, "2026-06-25 10:00", "owner", "2026-06-25 10:00"),
                (2001, 20, 2, "2026-06-25 10:00", "member", "2026-06-25 10:00"),
                (2002, 20, 3, "2026-06-25 10:00", "member", "2026-06-25 10:00"),
                (2003, 20, 4, "2026-06-25 10:00", "member", "2026-06-25 10:00"),
            ]
            test_group_members.extend(
                (
                    2000 + user_id,
                    20,
                    user_id,
                    "2026-06-25 10:00",
                    "member",
                    "2026-06-25 10:00",
                )
                for user_id in range(101, 111)
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO group_members (
                    id, group_id, user_id, join_time, role, last_read_time
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                test_group_members,
            )

            demo_private_messages = [
                (1, 4, 2, "李医生您好，社区这边有位老人血压控制不好，想请您帮忙看一下。", "2026-06-25 09:05", 1, 0),
                (2, 2, 4, "可以，先把既往用药、血压记录和肾功能结果发来。", "2026-06-25 09:08", 1, 0),
                (3, 2, 4, "涉及诊断和用药调整时，请以正式会诊意见为准。", "2026-06-25 09:09", 0, 0),
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO private_messages (
                    id, sender_id, receiver_id, content, send_time, is_read, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                demo_private_messages,
            )

            demo_group_messages = [
                (1, 1, 1, "张主任", "市中心医院（三甲）", "心内科", "这个病例讨论群用于心梗术后康复方案跟进。", "2026-06-25 09:25", 0),
                (2, 1, 2, "李医生", "区第一人民医院（二甲）", "心内科", "我已补充患者术后活动耐量和复查计划。", "2026-06-25 09:28", 0),
                (3, 2, 4, "赵医生", "街道社区卫生服务中心", "全科", "社区近期会同步整理转诊患者的随访数据。", "2026-06-25 09:35", 0),
            ]
            test_group_messages = [
                (
                    3000 + idx,
                    20,
                    user_id,
                    f"测试医生{idx:02d}",
                    hospital_name,
                    department,
                    f"群聊测试消息 {idx:02d}：用于检查多人头像、消息滚动和实时推送。",
                    f"2026-06-25 10:{idx:02d}",
                    0,
                )
                for idx, user_id, hospital_name, department in [
                    (1, 101, "市中心医院（三甲）", "心内科"),
                    (2, 102, "区第一人民医院（二甲）", "急诊科"),
                    (3, 103, "区第二人民医院（二甲）", "骨科"),
                    (4, 104, "街道社区卫生服务中心", "全科"),
                    (5, 105, "市中心医院（三甲）", "影像科"),
                    (6, 106, "区第一人民医院（二甲）", "检验科"),
                    (7, 107, "区第二人民医院（二甲）", "呼吸科"),
                    (8, 108, "街道社区卫生服务中心", "康复科"),
                    (9, 109, "市中心医院（三甲）", "神经内科"),
                    (10, 110, "区第一人民医院（二甲）", "药学部"),
                ]
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO group_messages (
                    id, group_id, sender_id, sender_name, sender_hospital,
                    sender_department, content, send_time, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                demo_group_messages,
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO group_messages (
                    id, group_id, sender_id, sender_name, sender_hospital,
                    sender_department, content, send_time, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                test_group_messages,
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
                u.phone AS phone,
                u.wechat_id AS wechat_id,
                u.wechat_openid AS wechat_openid,
                u.register_source AS register_source,
                u.title AS title,
                u.avatar AS avatar,
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


def update_user_profile(
    user_id: int,
    name: str,
    department: str,
    title: str,
    phone: str,
    wechat_id: str,
    avatar: str,
) -> None:
    """保存医生自己填写的个人资料；医院和角色不允许在个人页修改。"""
    name = name.strip()
    department = department.strip()
    title = title.strip()
    phone = phone.strip()
    wechat_id = wechat_id.strip()
    avatar = avatar.strip()

    if not name:
        raise ValueError("真实姓名不能为空")

    # Demo 里做基础长度限制，避免表单误输入过长内容撑破页面。
    name = name[:30]
    department = department[:30]
    title = title[:30]
    phone = phone[:30]
    wechat_id = wechat_id[:30]
    avatar = avatar[:2]

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE users
                SET name = ?, department = ?, title = ?, phone = ?, wechat_id = ?, avatar = ?
                WHERE id = ?
                """,
                (name, department, title, phone, wechat_id, avatar, user_id),
            )
    finally:
        conn.close()


def normalize_phone(phone: str) -> str:
    """统一手机号格式；演示账号允许 1/2/3/4 这类短号码。"""
    return re.sub(r"[\s\-]", "", (phone or "").strip())


def valid_phone(phone: str) -> bool:
    """基础手机号校验。生产环境可改成严格大陆手机号或医院统一身份号规则。"""
    return bool(re.fullmatch(r"\+?\d{1,20}", phone or ""))


def get_user_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """按手机号查找医生。"""
    phone = normalize_phone(phone)
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id
            FROM users
            WHERE phone = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
        if row is None:
            return None
        return get_user_with_hospital(row["id"])
    finally:
        conn.close()


def get_hospitals() -> List[sqlite3.Row]:
    """注册资料使用：查询可选择医院。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT id, name, level
            FROM hospitals
            ORDER BY level, id
            """
        ).fetchall()
    finally:
        conn.close()


def generate_sms_code() -> str:
    """生成 6 位短信验证码。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_sms_code(phone: str, code: str, scene: str, channel: str) -> None:
    """
    发送短信验证码。

    当前项目没有接入短信厂商，先写入服务日志；生产环境只替换这里即可。
    """
    print(
        f"[DEMO SMS] channel={channel} scene={scene} phone={phone} code={code}",
        flush=True,
    )


def issue_verification_code(
    phone: str,
    scene: str,
    channel: str = "web",
    wechat_openid: str = "",
    request_ip: str = "",
) -> str:
    """签发短信验证码并持久化，返回演示验证码。"""
    phone = normalize_phone(phone)
    scene = (scene or "login").strip()
    channel = (channel or "web").strip()
    wechat_openid = (wechat_openid or "").strip()
    if scene not in SMS_CODE_SCENES:
        raise ValueError("验证码用途不正确")
    if channel not in DEMO_SMS_CHANNELS:
        raise ValueError("验证码渠道不正确")
    if not valid_phone(phone):
        raise ValueError("请输入正确的手机号")

    if scene == "login" and get_user_by_phone(phone) is None:
        raise ValueError("该手机号尚未注册，请先在微信小程序完成注册")
    if scene == "register" and get_user_by_phone(phone) is not None:
        raise ValueError("该手机号已注册，请直接登录")

    code = generate_sms_code()
    expires_at = int(time.time()) + SMS_CODE_TTL_SECONDS
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO verification_codes (
                    phone, code, scene, channel, wechat_openid,
                    create_time, expires_at, used_time, request_ip
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    phone,
                    code,
                    scene,
                    channel,
                    wechat_openid,
                    now_text(),
                    expires_at,
                    request_ip,
                ),
            )
    finally:
        conn.close()
    send_sms_code(phone, code, scene, channel)
    return code


def verify_sms_code(phone: str, code: str, scene: str) -> Optional[str]:
    """校验短信验证码；成功返回 None，失败返回提示。"""
    phone = normalize_phone(phone)
    code = (code or "").strip()
    scene = (scene or "login").strip()
    if not valid_phone(phone):
        return "请输入正确的手机号"
    if not re.fullmatch(r"\d{6}", code):
        return "请输入 6 位短信验证码"
    if scene not in SMS_CODE_SCENES:
        return "验证码用途不正确"

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, expires_at
            FROM verification_codes
            WHERE phone = ?
              AND code = ?
              AND scene = ?
              AND used_time = ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (phone, code, scene),
        ).fetchone()
        if row is None:
            return "验证码不正确或已使用"
        if int(row["expires_at"]) < int(time.time()):
            return "验证码已过期，请重新获取"
        with conn:
            conn.execute(
                """
                UPDATE verification_codes
                SET used_time = ?
                WHERE id = ?
                """,
                (now_text(), row["id"]),
            )
    finally:
        conn.close()
    return None


def register_user_by_sms(
    phone: str,
    name: str,
    hospital_id: int,
    department: str,
    title: str,
    wechat_id: str = "",
    avatar: str = "",
    wechat_openid: str = "",
    register_source: str = "wechat_mini_program",
) -> Dict[str, Any]:
    """小程序注册：验证码校验后创建医生实名账号。"""
    phone = normalize_phone(phone)
    name = (name or "").strip()[:30]
    department = (department or "").strip()[:30]
    title = (title or "医生").strip()[:30]
    wechat_id = (wechat_id or "").strip()[:30]
    avatar = ((avatar or name[:1] or "医").strip())[:2]
    wechat_openid = (wechat_openid or "").strip()[:80]
    register_source = (register_source or "wechat_mini_program").strip()[:30]
    if not valid_phone(phone):
        raise ValueError("请输入正确的手机号")
    if not name:
        raise ValueError("真实姓名不能为空")
    if get_user_by_phone(phone) is not None:
        raise ValueError("该手机号已注册，请直接登录")

    conn = get_connection()
    try:
        hospital = conn.execute(
            "SELECT id FROM hospitals WHERE id = ?",
            (hospital_id,),
        ).fetchone()
        if hospital is None:
            raise ValueError("请选择有效医院")
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    name, password, hospital_id, department, role,
                    phone, wechat_id, title, avatar, wechat_openid, register_source
                )
                VALUES (?, '', ?, ?, '医生', ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    hospital_id,
                    department,
                    phone,
                    wechat_id,
                    title,
                    avatar,
                    wechat_openid,
                    register_source,
                ),
            )
            user_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT OR IGNORE INTO friend_groups (user_id, group_name, create_time)
                VALUES (?, '我的好友', ?)
                """,
                (user_id, now_text()),
            )
    finally:
        conn.close()

    user = get_user_with_hospital(user_id)
    if user is None:
        raise ValueError("注册失败，请稍后重试")
    write_operation_log(user_id, "register", f"通过 {register_source} 注册账号")
    return user


def request_auth_value(key: str, default: str = "") -> str:
    """兼容表单提交和小程序 JSON 请求的取值。"""
    payload = request.get_json(silent=True) if request.is_json else None
    if isinstance(payload, dict) and key in payload:
        value = payload.get(key)
    else:
        value = request.form.get(key, default)
    return str(value if value is not None else default).strip()


def public_user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    """返回给小程序/前端的安全用户字段。"""
    return {
        "id": user["id"],
        "name": user["name"],
        "phone": user["phone"],
        "wechat_id": user["wechat_id"],
        "title": user["title"],
        "avatar": user["avatar"],
        "department": user["department"],
        "role": user["role"],
        "hospital_id": user["hospital_id"],
        "hospital_name": user["hospital_name"],
        "hospital_level": user["hospital_level"],
    }


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
    if is_consultation_group_member(consultation["id"], current_user["id"]):
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
) -> int:
    """新增提问记录，并返回新病例 id，供病例聊天室绑定使用。"""
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
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
            return int(cursor.lastrowid)
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
# 好友、聊天、群聊与合规审计函数
# =============================


def doctor_display_name(user: Any) -> str:
    """统一实名展示：姓名 + 医院 + 科室 + 职称，不支持昵称。"""
    name = user["name"] or ""
    hospital = user["hospital_name"] or ""
    department = user["department"] or ""
    title = user["title"] or "医生"
    return f"{name}｜{hospital}｜{department}｜{title}"


def avatar_text(user: Any) -> str:
    """文字头像：优先使用数据库 avatar，否则取姓名首字。"""
    avatar = user["avatar"] if "avatar" in user.keys() else ""
    if avatar:
        return avatar
    name = user["name"] or "医"
    return name[:1]


def contains_sensitive_terms(content: str) -> bool:
    """判断消息是否包含医疗风险敏感词，用于临床参考提示。"""
    return any(term in (content or "") for term in SENSITIVE_TERMS)


def group_type_label(group_type: str) -> str:
    """把群类型编码转换为页面展示文字。"""
    return GROUP_TYPE_LABELS.get(group_type, "工作群")


def write_operation_log(
    user_id: int,
    operation_type: str,
    operation_content: str,
    ip_address: str = "",
) -> None:
    """写入医疗合规操作日志。所有关键协作动作都应调用此函数。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO operation_logs (
                    user_id, operation_type, operation_content, operation_time, ip_address
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, operation_type, operation_content, now_text(), ip_address),
            )
    finally:
        conn.close()


def get_doctor_profile(user_id: int) -> Optional[sqlite3.Row]:
    """查询医生实名资料。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                u.*,
                h.name AS hospital_name,
                h.level AS hospital_level,
                h.path AS hospital_path
            FROM users u
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()


def get_relation_status(user_id: int, friend_id: int) -> str:
    """返回当前用户与目标用户的好友关系状态。"""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT status
            FROM friend_relations
            WHERE user_id = ? AND friend_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, friend_id),
        ).fetchone()
        return row["status"] if row else ""
    finally:
        conn.close()


def is_friend(user_id: int, friend_id: int) -> bool:
    """判断两人是否为已通过好友。"""
    return get_relation_status(user_id, friend_id) == "accepted"


def search_users(keyword: str, current_user_id: int) -> List[sqlite3.Row]:
    """按手机号或微信号搜索医生，支持简单模糊匹配。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                u.*,
                h.name AS hospital_name,
                h.level AS hospital_level
            FROM users u
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE u.id != ?
              AND (u.phone LIKE ? OR u.wechat_id LIKE ?)
            ORDER BY u.id
            LIMIT 10
            """,
            (current_user_id, f"%{keyword}%", f"%{keyword}%"),
        ).fetchall()
    finally:
        conn.close()


def ensure_friend_group(user_id: int, group_name: str) -> str:
    """确保用户有某个好友分组，返回最终分组名。"""
    group_name = (group_name or "我的好友").strip() or "我的好友"
    conn = get_connection()
    try:
        with conn:
            exists = conn.execute(
                """
                SELECT id FROM friend_groups
                WHERE user_id = ? AND group_name = ?
                """,
                (user_id, group_name),
            ).fetchone()
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO friend_groups (user_id, group_name, create_time)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, group_name, now_text()),
                )
        return group_name
    finally:
        conn.close()


def get_friend_groups(user_id: int) -> List[sqlite3.Row]:
    """查询当前用户的好友分组。"""
    ensure_friend_group(user_id, "我的好友")
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT *
            FROM friend_groups
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def create_friend_group(user_id: int, group_name: str) -> None:
    """新建好友分组并记录操作日志。"""
    final_name = ensure_friend_group(user_id, group_name)
    write_operation_log(user_id, "friend_group", f"创建或确认好友分组：{final_name}")


def get_friends(user_id: int) -> List[sqlite3.Row]:
    """查询当前用户已通过且未删除的好友。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                fr.friend_group,
                u.*,
                h.name AS hospital_name,
                h.level AS hospital_level
            FROM friend_relations fr
            JOIN users u ON fr.friend_id = u.id
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE fr.user_id = ?
              AND fr.status = 'accepted'
            ORDER BY fr.friend_group, u.id
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def get_friends_grouped(user_id: int) -> Dict[str, List[sqlite3.Row]]:
    """按好友分组组织好友列表。"""
    grouped: Dict[str, List[sqlite3.Row]] = {}
    for group in get_friend_groups(user_id):
        grouped[group["group_name"]] = []
    for friend in get_friends(user_id):
        grouped.setdefault(friend["friend_group"] or "我的好友", []).append(friend)
    return grouped


def get_pending_friend_requests(user_id: int) -> List[sqlite3.Row]:
    """查询别人发给当前用户的待处理好友申请。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                fr.*,
                u.name,
                u.department,
                u.title,
                u.avatar,
                u.phone,
                u.wechat_id,
                h.name AS hospital_name
            FROM friend_relations fr
            JOIN users u ON fr.user_id = u.id
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE fr.friend_id = ?
              AND fr.status = 'pending'
            ORDER BY fr.create_time DESC, fr.id DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def send_friend_request(
    current_user: Dict[str, Any],
    friend_id: int,
    apply_msg: str,
    ip_address: str = "",
) -> str:
    """发送好友申请，返回页面提示信息。"""
    if friend_id == current_user["id"]:
        return "不能添加自己为好友"
    target = get_doctor_profile(friend_id)
    if target is None:
        return "未找到对应用户"

    relation = get_relation_status(current_user["id"], friend_id)
    if relation == "accepted":
        return "你们已经是好友"
    if relation == "pending":
        return "好友申请已发送，请等待对方处理"

    apply_msg = apply_msg.strip() or (
        f"我是{current_user['hospital_name']}{current_user['department']}的{current_user['name']}"
    )
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO friend_relations (
                    user_id, friend_id, status, apply_msg, friend_group, create_time, update_time
                )
                VALUES (?, ?, 'pending', ?, '我的好友', ?, ?)
                """,
                (current_user["id"], friend_id, apply_msg, now_text(), now_text()),
            )
    finally:
        conn.close()

    write_operation_log(
        current_user["id"],
        "add_friend",
        f"向 {target['name']} 发送好友申请",
        ip_address,
    )
    return "好友申请已发送，等待对方通过"


def respond_friend_request(
    current_user: Dict[str, Any],
    relation_id: int,
    action: str,
    friend_group: str,
    ip_address: str = "",
) -> str:
    """同意或拒绝好友申请。"""
    conn = get_connection()
    try:
        request_row = conn.execute(
            """
            SELECT *
            FROM friend_relations
            WHERE id = ? AND friend_id = ? AND status = 'pending'
            """,
            (relation_id, current_user["id"]),
        ).fetchone()
        if request_row is None:
            return "好友申请不存在或已处理"

        applicant_id = request_row["user_id"]
        if action == "reject":
            with conn:
                conn.execute(
                    """
                    UPDATE friend_relations
                    SET status = 'rejected', update_time = ?
                    WHERE id = ?
                    """,
                    (now_text(), relation_id),
                )
            write_operation_log(
                current_user["id"],
                "reject_friend",
                f"拒绝用户 {applicant_id} 的好友申请",
                ip_address,
            )
            return "已拒绝好友申请"

        friend_group = ensure_friend_group(current_user["id"], friend_group)
        ensure_friend_group(applicant_id, "我的好友")
        with conn:
            conn.execute(
                """
                UPDATE friend_relations
                SET status = 'accepted', friend_group = '我的好友', update_time = ?
                WHERE id = ?
                """,
                (now_text(), relation_id),
            )
            conn.execute(
                """
                INSERT INTO friend_relations (
                    user_id, friend_id, status, apply_msg, friend_group, create_time, update_time
                )
                VALUES (?, ?, 'accepted', ?, ?, ?, ?)
                """,
                (
                    current_user["id"],
                    applicant_id,
                    request_row["apply_msg"],
                    friend_group,
                    now_text(),
                    now_text(),
                ),
            )
        write_operation_log(
            current_user["id"],
            "accept_friend",
            f"通过用户 {applicant_id} 的好友申请",
            ip_address,
        )
        return "已同意好友申请"
    finally:
        conn.close()


def move_friend_to_group(user_id: int, friend_id: int, group_name: str) -> None:
    """移动好友到指定分组。"""
    group_name = ensure_friend_group(user_id, group_name)
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE friend_relations
                SET friend_group = ?, update_time = ?
                WHERE user_id = ? AND friend_id = ? AND status = 'accepted'
                """,
                (group_name, now_text(), user_id, friend_id),
            )
    finally:
        conn.close()
    write_operation_log(user_id, "move_friend", f"移动好友 {friend_id} 到分组 {group_name}")


def soft_delete_friend(user_id: int, friend_id: int) -> None:
    """软删除好友关系，不删除历史消息，满足医疗留痕。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE friend_relations
                SET status = 'deleted', update_time = ?
                WHERE (user_id = ? AND friend_id = ?)
                   OR (user_id = ? AND friend_id = ?)
                """,
                (now_text(), user_id, friend_id, friend_id, user_id),
            )
    finally:
        conn.close()
    write_operation_log(user_id, "delete_friend", f"软删除好友关系：{friend_id}")


def add_to_expert_library(user_id: int, expert_user_id: int) -> None:
    """将好友加入专家库，预留专家预约、远程会诊等扩展。"""
    conn = get_connection()
    try:
        exists = conn.execute(
            """
            SELECT id FROM expert_library
            WHERE user_id = ? AND expert_user_id = ?
            """,
            (user_id, expert_user_id),
        ).fetchone()
        if exists is None:
            with conn:
                conn.execute(
                    """
                    INSERT INTO expert_library (user_id, expert_user_id, create_time, note)
                    VALUES (?, ?, ?, '')
                    """,
                    (user_id, expert_user_id, now_text()),
                )
    finally:
        conn.close()
    write_operation_log(user_id, "add_expert", f"加入专家库：{expert_user_id}")


def get_private_messages(user_id: int, friend_id: int) -> List[sqlite3.Row]:
    """查询一对一私聊消息，按时间正序展示。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT *
            FROM private_messages
            WHERE is_deleted = 0
              AND (
                  (sender_id = ? AND receiver_id = ?)
                  OR (sender_id = ? AND receiver_id = ?)
              )
            ORDER BY send_time, id
            """,
            (user_id, friend_id, friend_id, user_id),
        ).fetchall()
    finally:
        conn.close()


def mark_private_messages_read(user_id: int, friend_id: int) -> None:
    """打开私聊页时把对方发来的消息标记为已读。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE private_messages
                SET is_read = 1
                WHERE sender_id = ? AND receiver_id = ? AND is_read = 0
                """,
                (friend_id, user_id),
            )
    finally:
        conn.close()


def send_private_message(
    sender_id: int,
    receiver_id: int,
    content: str,
    ip_address: str = "",
) -> None:
    """发送私聊消息，消息永久留痕，不提供用户删除/撤回。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO private_messages (
                    sender_id, receiver_id, content, send_time, is_read, is_deleted
                )
                VALUES (?, ?, ?, ?, 0, 0)
                """,
                (sender_id, receiver_id, content, now_text()),
            )
    finally:
        conn.close()
    write_operation_log(sender_id, "send_message", f"发送私聊消息给 {receiver_id}", ip_address)


def add_group_member_conn(
    conn: sqlite3.Connection,
    group_id: int,
    user_id: int,
    role: str = "member",
) -> None:
    """向群中加入成员；已存在则不重复加入。"""
    exists = conn.execute(
        """
        SELECT id FROM group_members
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    ).fetchone()
    if exists is None:
        conn.execute(
            """
            INSERT INTO group_members (group_id, user_id, join_time, role, last_read_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, user_id, now_text(), role, now_text()),
        )


def is_group_member(group_id: int, user_id: int) -> bool:
    """判断用户是否在群内。"""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_group_role(group_id: int, user_id: int) -> str:
    """查询用户在群里的角色。"""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT role FROM group_members
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        ).fetchone()
        return row["role"] if row else ""
    finally:
        conn.close()


def get_group_detail(group_id: int) -> Optional[sqlite3.Row]:
    """查询群基础信息。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                g.*,
                u.name AS creator_name
            FROM groups g
            JOIN users u ON g.creator_id = u.id
            WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
    finally:
        conn.close()


def get_group_members(group_id: int) -> List[sqlite3.Row]:
    """查询群成员实名信息。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT
                gm.*,
                u.name,
                u.department,
                u.title,
                u.avatar,
                h.name AS hospital_name
            FROM group_members gm
            JOIN users u ON gm.user_id = u.id
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE gm.group_id = ?
            ORDER BY
                CASE gm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                gm.id
            """,
            (group_id,),
        ).fetchall()
    finally:
        conn.close()


def get_group_messages(group_id: int) -> List[sqlite3.Row]:
    """查询群聊消息，按时间正序展示。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT *
            FROM group_messages
            WHERE group_id = ? AND is_deleted = 0
            ORDER BY send_time, id
            """,
            (group_id,),
        ).fetchall()
    finally:
        conn.close()


def mark_group_read(group_id: int, user_id: int) -> None:
    """打开群聊时更新当前用户的最后阅读时间。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                UPDATE group_members
                SET last_read_time = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (now_text(), group_id, user_id),
            )
    finally:
        conn.close()


def create_group(
    creator: Dict[str, Any],
    group_name: str,
    group_type: str,
    description: str,
    member_ids: List[int],
    consultation_id: Optional[int] = None,
) -> int:
    """创建群聊，并自动加入群主和勾选成员。"""
    group_type = group_type if group_type in GROUP_TYPE_LABELS else "work"
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO groups (
                    group_name, group_type, creator_id, create_time, description, consultation_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_name,
                    group_type,
                    creator["id"],
                    now_text(),
                    description,
                    consultation_id,
                ),
            )
            group_id = int(cursor.lastrowid)
            add_group_member_conn(conn, group_id, creator["id"], "owner")
            for member_id in sorted(set(member_ids)):
                if member_id != creator["id"]:
                    add_group_member_conn(conn, group_id, member_id, "member")
        write_operation_log(creator["id"], "create_group", f"创建群聊：{group_name}")
        return group_id
    finally:
        conn.close()


def get_my_groups(user_id: int, group_type: str = "全部") -> List[sqlite3.Row]:
    """查询当前用户加入的群聊列表，包含成员数和最后一条消息。"""
    params: List[Any] = [user_id]
    type_sql = ""
    if group_type in GROUP_TYPE_LABELS:
        type_sql = " AND g.group_type = ?"
        params.append(group_type)

    conn = get_connection()
    try:
        return conn.execute(
            f"""
            SELECT
                g.*,
                COUNT(DISTINCT gm_all.user_id) AS member_count,
                (
                    SELECT content FROM group_messages m
                    WHERE m.group_id = g.id AND m.is_deleted = 0
                    ORDER BY m.send_time DESC, m.id DESC
                    LIMIT 1
                ) AS last_message,
                (
                    SELECT send_time FROM group_messages m
                    WHERE m.group_id = g.id AND m.is_deleted = 0
                    ORDER BY m.send_time DESC, m.id DESC
                    LIMIT 1
                ) AS last_message_time,
                (
                    SELECT COUNT(*) FROM group_messages m
                    WHERE m.group_id = g.id
                      AND m.sender_id != ?
                      AND m.is_deleted = 0
                      AND m.send_time > COALESCE(gm_self.last_read_time, '')
                ) AS unread_count
            FROM groups g
            JOIN group_members gm_self ON gm_self.group_id = g.id
            JOIN group_members gm_all ON gm_all.group_id = g.id
            WHERE gm_self.user_id = ?
            {type_sql}
            GROUP BY g.id
            ORDER BY COALESCE(last_message_time, g.create_time) DESC, g.id DESC
            """,
            [user_id, *params],
        ).fetchall()
    finally:
        conn.close()


def get_private_conversation_summaries(user_id: int) -> List[Dict[str, Any]]:
    """消息首页使用：把好友私聊整理成类似微信会话列表的数据。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                fr.friend_group,
                u.*,
                h.name AS hospital_name,
                h.level AS hospital_level,
                (
                    SELECT content
                    FROM private_messages m
                    WHERE m.is_deleted = 0
                      AND (
                          (m.sender_id = ? AND m.receiver_id = u.id)
                          OR (m.sender_id = u.id AND m.receiver_id = ?)
                      )
                    ORDER BY m.send_time DESC, m.id DESC
                    LIMIT 1
                ) AS last_message,
                (
                    SELECT send_time
                    FROM private_messages m
                    WHERE m.is_deleted = 0
                      AND (
                          (m.sender_id = ? AND m.receiver_id = u.id)
                          OR (m.sender_id = u.id AND m.receiver_id = ?)
                      )
                    ORDER BY m.send_time DESC, m.id DESC
                    LIMIT 1
                ) AS last_message_time,
                (
                    SELECT COUNT(*)
                    FROM private_messages m
                    WHERE m.sender_id = u.id
                      AND m.receiver_id = ?
                      AND m.is_read = 0
                      AND m.is_deleted = 0
                ) AS unread_count
            FROM friend_relations fr
            JOIN users u ON fr.friend_id = u.id
            JOIN hospitals h ON u.hospital_id = h.id
            WHERE fr.user_id = ?
              AND fr.status = 'accepted'
            ORDER BY COALESCE(last_message_time, fr.update_time, fr.create_time) DESC, u.id
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_message_threads(current_user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """合并好友私聊和群聊，用一个页面展示所有沟通入口。"""
    threads: List[Dict[str, Any]] = []
    for friend in get_private_conversation_summaries(current_user["id"]):
        threads.append(
            {
                "kind": "private",
                "target_id": friend["id"],
                "title": f"{friend['name']}｜{friend['title']}",
                "subtitle": f"{friend['hospital_name']}｜{friend['department']}",
                "last_message": friend["last_message"] or "还没有消息，点击开始沟通",
                "last_time": friend["last_message_time"] or "",
                "sort_time": friend["last_message_time"] or "",
                "unread_count": friend["unread_count"],
                "avatar": avatar_text(friend),
            }
        )

    for group in get_my_groups(current_user["id"]):
        last_message = group["last_message"] or "暂无消息"
        if not group["last_message"] and group["last_message_time"]:
            last_message = "附件消息"
        threads.append(
            {
                "kind": "group",
                "target_id": group["id"],
                "title": group["group_name"],
                "subtitle": f"{group_type_label(group['group_type'])}｜成员 {group['member_count']} 人",
                "last_message": last_message,
                "last_time": group["last_message_time"] or group["create_time"],
                "sort_time": group["last_message_time"] or group["create_time"],
                "unread_count": group["unread_count"],
                "avatar": "群",
            }
        )

    threads.sort(key=lambda item: item["sort_time"] or "", reverse=True)
    return threads


def send_group_message(
    group_id: int,
    sender: Dict[str, Any],
    content: str,
    attachment: Dict[str, str],
    ip_address: str = "",
) -> None:
    """发送群聊消息，写入实名快照，保证历史消息可审计。"""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO group_messages (
                    group_id, sender_id, sender_name, sender_hospital,
                    sender_department, content, send_time,
                    attachment_path, attachment_name, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    group_id,
                    sender["id"],
                    sender["name"],
                    sender["hospital_name"],
                    sender["department"],
                    content,
                    now_text(),
                    attachment["path"],
                    attachment["name"],
                ),
            )
    finally:
        conn.close()
    write_operation_log(sender["id"], "send_message", f"在群 {group_id} 发送消息或附件", ip_address)


def invite_users_to_group(group_id: int, inviter_id: int, user_ids: List[int]) -> int:
    """邀请好友入群，返回新增人数。"""
    added = 0
    conn = get_connection()
    try:
        with conn:
            for user_id in sorted(set(user_ids)):
                before = conn.execute(
                    """
                    SELECT id FROM group_members
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (group_id, user_id),
                ).fetchone()
                add_group_member_conn(conn, group_id, user_id, "member")
                if before is None:
                    added += 1
    finally:
        conn.close()
    if added:
        write_operation_log(inviter_id, "join_group", f"邀请 {added} 人加入群 {group_id}")
    return added


def remove_group_member(group_id: int, actor_id: int, target_user_id: int) -> str:
    """群主移除成员；历史消息保留，不做物理删除。"""
    actor_role = get_group_role(group_id, actor_id)
    if actor_role != "owner":
        return "只有群主可以移除群成员"
    if actor_id == target_user_id:
        return "群主不能在成员管理中移除自己，请使用退出群聊"

    conn = get_connection()
    try:
        with conn:
            target = conn.execute(
                """
                SELECT role
                FROM group_members
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, target_user_id),
            ).fetchone()
            if target is None:
                return "该用户已经不在群内"
            if target["role"] == "owner":
                return "不能移除群主"
            conn.execute(
                """
                DELETE FROM group_members
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, target_user_id),
            )
    finally:
        conn.close()

    write_operation_log(
        actor_id,
        "remove_group_member",
        f"从群 {group_id} 移除成员 {target_user_id}",
    )
    return "成员已移出群聊，历史消息继续保留"


def leave_group(group_id: int, user_id: int) -> str:
    """退出群聊；如果当前用户是群主，自动转让给下一位群成员后再退出。"""
    role = get_group_role(group_id, user_id)
    successor_user_id: Optional[int] = None
    conn = get_connection()
    try:
        with conn:
            if role == "owner":
                successor = conn.execute(
                    """
                    SELECT user_id
                    FROM group_members
                    WHERE group_id = ? AND user_id != ?
                    ORDER BY
                        CASE role WHEN 'admin' THEN 0 ELSE 1 END,
                        join_time,
                        id
                    LIMIT 1
                    """,
                    (group_id, user_id),
                ).fetchone()

                if successor is None:
                    return "群里没有其他成员，无法自动转让群主并退出"

                successor_user_id = int(successor["user_id"])
                conn.execute(
                    """
                    UPDATE group_members
                    SET role = 'owner'
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (group_id, successor_user_id),
                )

            conn.execute(
                """
                DELETE FROM group_members
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            )
    finally:
        conn.close()
    if successor_user_id is not None:
        write_operation_log(
            user_id,
            "transfer_group_owner",
            f"退出群聊前自动转让群主：群 {group_id}，新群主 {successor_user_id}",
        )
    write_operation_log(user_id, "leave_group", f"退出群聊：{group_id}")
    if role == "owner":
        return "已自动转让群主并退出群聊"
    return "已退出群聊"


def get_unread_summary(current_user: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """导航栏红点数据：好友申请、私聊未读、群聊未读。"""
    if current_user is None:
        return {"friend_requests": 0, "private_messages": 0, "group_messages": 0}
    conn = get_connection()
    try:
        friend_requests = conn.execute(
            """
            SELECT COUNT(*) FROM friend_relations
            WHERE friend_id = ? AND status = 'pending'
            """,
            (current_user["id"],),
        ).fetchone()[0]
        private_messages = conn.execute(
            """
            SELECT COUNT(*) FROM private_messages
            WHERE receiver_id = ? AND is_read = 0 AND is_deleted = 0
            """,
            (current_user["id"],),
        ).fetchone()[0]
        group_messages = conn.execute(
            """
            SELECT COUNT(*)
            FROM group_messages m
            JOIN group_members gm ON gm.group_id = m.group_id
            WHERE gm.user_id = ?
              AND m.sender_id != ?
              AND m.is_deleted = 0
              AND m.send_time > COALESCE(gm.last_read_time, '')
            """,
            (current_user["id"], current_user["id"]),
        ).fetchone()[0]
        return {
            "friend_requests": friend_requests,
            "private_messages": private_messages,
            "group_messages": group_messages,
        }
    finally:
        conn.close()


def get_case_group(consultation_id: int) -> Optional[sqlite3.Row]:
    """查询某个病例绑定的病例讨论群。"""
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT *
            FROM groups
            WHERE consultation_id = ? AND group_type = 'case'
            ORDER BY id
            LIMIT 1
            """,
            (consultation_id,),
        ).fetchone()
    finally:
        conn.close()


def get_hospital_user_ids(hospital_id: int) -> List[int]:
    """查询某家医院的所有演示医生，用于病例群自动加入被请教医院。"""
    conn = get_connection()
    try:
        return [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM users WHERE hospital_id = ? ORDER BY id",
                (hospital_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def get_group_user_ids(group_id: int) -> List[int]:
    """查询群成员用户 id。"""
    conn = get_connection()
    try:
        return [
            row["user_id"]
            for row in conn.execute(
                "SELECT user_id FROM group_members WHERE group_id = ?",
                (group_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def ensure_case_group(
    consultation_id: int,
    current_user: Dict[str, Any],
    target_hospital_id: int,
    invite_user_id: Optional[int] = None,
    source_group_id: Optional[int] = None,
) -> int:
    """确保某个病例有绑定病例群，并自动加入相关协作成员。"""
    existing = get_case_group(consultation_id)
    if existing is not None:
        group_id = existing["id"]
    else:
        consultation = get_consultation_detail(consultation_id)
        group_name = f"病例讨论：{consultation['title'] if consultation else consultation_id}"
        group_id = create_group(
            current_user,
            group_name,
            "case",
            "由病例会诊自动创建的病例讨论群",
            [],
            consultation_id,
        )

    member_ids = set(get_hospital_user_ids(target_hospital_id))
    member_ids.add(current_user["id"])
    if invite_user_id:
        member_ids.add(invite_user_id)
    if source_group_id:
        member_ids.update(get_group_user_ids(source_group_id))

    added = invite_users_to_group(group_id, current_user["id"], list(member_ids))
    if added:
        write_operation_log(
            current_user["id"],
            "join_group",
            f"病例 {consultation_id} 自动加入 {added} 名协作成员",
        )
    return group_id


def is_consultation_group_member(consultation_id: int, user_id: int) -> bool:
    """病例查看扩展权限：病例群成员可查看对应病例。"""
    group = get_case_group(consultation_id)
    if group is None:
        return False
    return is_group_member(group["id"], user_id)


# =============================
# Flask 应用与路由
# =============================


app = Flask(__name__) if Flask is not None else None
if app is not None:
    # Demo 用固定密钥即可；真实系统应从环境变量读取。
    app.secret_key = "demo-secret-key-for-hospital-consult-system"
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    init_database()


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
            "doctor_display_name": doctor_display_name,
            "avatar_text": avatar_text,
            "contains_sensitive_terms": contains_sensitive_terms,
            "group_type_label": group_type_label,
            "group_type_labels": GROUP_TYPE_LABELS,
            "sensitive_terms": SENSITIVE_TERMS,
            "unread_summary": get_unread_summary(get_current_user()),
            "chat_token": sign_chat_token(get_current_user()["id"]) if get_current_user() else "",
            "chat_ws_base": f"{'wss' if request.is_secure else 'ws'}://{request.host.split(':')[0]}:8001/ws",
        }

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """短信验证码登录。"""
        if request.method == "POST":
            phone = normalize_phone(request.form.get("phone", ""))
            sms_code = request.form.get("sms_code", "").strip()
            error = verify_sms_code(phone, sms_code, "login")
            if error:
                flash(error, "danger")
                return render_template("login.html", phone=phone)

            user = get_user_by_phone(phone)
            if user is None:
                flash("该手机号尚未注册，请先在微信小程序完成注册", "danger")
                return render_template("login.html", phone=phone)

            session.clear()
            session["user_id"] = user["id"]
            write_operation_log(
                user["id"],
                "sms_login",
                "通过短信验证码登录",
                request.remote_addr or "",
            )
            flash(f"欢迎登录，{user['name']}医生", "success")
            return redirect(request.args.get("next") or url_for("home"))

        return render_template("login.html", phone="")

    @app.route("/auth/send-code", methods=["POST"])
    def send_auth_code():
        """网页登录使用：发送短信验证码。"""
        phone = request_auth_value("phone")
        scene = request_auth_value("scene", "login")
        try:
            code = issue_verification_code(
                phone=phone,
                scene=scene,
                channel="web",
                request_ip=request.remote_addr or "",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": "验证码已发送",
                "expires_in": SMS_CODE_TTL_SECONDS,
                "demo_code": code,
            }
        )

    @app.route("/api/wechat-mini/hospitals")
    def api_wechat_mini_hospitals():
        """微信小程序注册页使用：医院选项。"""
        return jsonify(
            {
                "ok": True,
                "hospitals": [
                    {"id": item["id"], "name": item["name"], "level": item["level"]}
                    for item in get_hospitals()
                ],
            }
        )

    @app.route("/api/wechat-mini/register/code", methods=["POST"])
    def api_wechat_mini_register_code():
        """微信小程序注册：获取短信验证码。"""
        phone = request_auth_value("phone")
        wechat_openid = (
            request_auth_value("wechat_openid")
            or request_auth_value("openid")
            or request_auth_value("wx_code")
        )
        try:
            code = issue_verification_code(
                phone=phone,
                scene="register",
                channel="wechat_mini_program",
                wechat_openid=wechat_openid,
                request_ip=request.remote_addr or "",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": "注册验证码已发送",
                "expires_in": SMS_CODE_TTL_SECONDS,
                "demo_code": code,
            }
        )

    @app.route("/api/wechat-mini/register", methods=["POST"])
    def api_wechat_mini_register():
        """微信小程序注册：验证码通过后创建医生账号。"""
        phone = normalize_phone(request_auth_value("phone"))
        sms_code = request_auth_value("sms_code") or request_auth_value("code")
        error = verify_sms_code(phone, sms_code, "register")
        if error:
            return jsonify({"ok": False, "message": error}), 400
        try:
            hospital_id = int(request_auth_value("hospital_id", "0"))
            user = register_user_by_sms(
                phone=phone,
                name=request_auth_value("name"),
                hospital_id=hospital_id,
                department=request_auth_value("department"),
                title=request_auth_value("title", "医生"),
                wechat_id=request_auth_value("wechat_id"),
                avatar=request_auth_value("avatar"),
                wechat_openid=(
                    request_auth_value("wechat_openid")
                    or request_auth_value("openid")
                    or request_auth_value("wx_code")
                ),
                register_source="wechat_mini_program",
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

        session.clear()
        session["user_id"] = user["id"]
        return jsonify(
            {
                "ok": True,
                "message": "注册成功",
                "user": public_user_payload(user),
                "chat_token": sign_chat_token(user["id"]),
            }
        )

    @app.route("/api/wechat-mini/login/code", methods=["POST"])
    def api_wechat_mini_login_code():
        """微信小程序验证码登录：获取短信验证码。"""
        phone = request_auth_value("phone")
        try:
            code = issue_verification_code(
                phone=phone,
                scene="login",
                channel="wechat_mini_program",
                request_ip=request.remote_addr or "",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "message": "登录验证码已发送",
                "expires_in": SMS_CODE_TTL_SECONDS,
                "demo_code": code,
            }
        )

    @app.route("/api/wechat-mini/login", methods=["POST"])
    def api_wechat_mini_login():
        """微信小程序验证码登录。"""
        phone = normalize_phone(request_auth_value("phone"))
        sms_code = request_auth_value("sms_code") or request_auth_value("code")
        error = verify_sms_code(phone, sms_code, "login")
        if error:
            return jsonify({"ok": False, "message": error}), 400
        user = get_user_by_phone(phone)
        if user is None:
            return jsonify({"ok": False, "message": "该手机号尚未注册"}), 404
        session.clear()
        session["user_id"] = user["id"]
        write_operation_log(
            user["id"],
            "sms_login",
            "通过微信小程序短信验证码登录",
            request.remote_addr or "",
        )
        return jsonify(
            {
                "ok": True,
                "message": "登录成功",
                "user": public_user_payload(user),
                "chat_token": sign_chat_token(user["id"]),
            }
        )

    @app.route("/logout")
    def logout():
        """退出登录。"""
        session.clear()
        flash("已退出登录", "success")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def home(current_user: Dict[str, Any]):
        """默认进入消息页，贴近微信式工作台。"""
        return redirect(url_for("messages"))

    @app.route("/messages")
    @login_required
    def messages(current_user: Dict[str, Any]):
        """消息：私聊和群聊放在同一个会话列表。"""
        return render_template(
            "messages.html",
            threads=get_message_threads(current_user),
        )

    @app.route("/contacts")
    @login_required
    def contacts(current_user: Dict[str, Any]):
        """通讯录：展示全部好友和群聊。"""
        return render_template(
            "contacts.html",
            grouped_friends=get_friends_grouped(current_user["id"]),
            groups=get_my_groups(current_user["id"]),
            pending_count=len(get_pending_friend_requests(current_user["id"])),
        )

    @app.route("/discover")
    @login_required
    def discover(current_user: Dict[str, Any]):
        """回信：展示我的提问、我的待回复和可处理的他人提问。"""
        stats = get_dashboard_stats(current_user)
        my_consultations = get_my_consultations(current_user["id"])
        my_pending_consultations = [
            item for item in my_consultations if item["status"] == "待回复"
        ]
        my_replied_consultations = [
            item for item in my_consultations if item["status"] == "已回复"
        ]
        visible_consultations = get_subordinate_consultations(
            current_user=current_user,
            limit=20,
        )
        actionable_consultations = [
            item for item in visible_consultations if item["status"] == "待回复"
        ]
        return render_template(
            "discover.html",
            stats=stats,
            my_consultations=my_consultations,
            my_pending_consultations=my_pending_consultations,
            my_replied_consultations=my_replied_consultations,
            visible_consultations=visible_consultations,
            actionable_consultations=actionable_consultations,
        )

    @app.route("/me")
    @login_required
    def me(current_user: Dict[str, Any]):
        """我：个人资料和自己发起的提问。"""
        my_items = get_my_consultations(current_user["id"])
        stats = get_dashboard_stats(current_user)
        return render_template(
            "me.html",
            my_consultations=my_items,
            stats=stats,
        )

    @app.route("/me/edit", methods=["GET", "POST"])
    @login_required
    def edit_profile(current_user: Dict[str, Any]):
        """医生个人资料编辑页：只允许维护个人展示资料，不允许改医院归属。"""
        if request.method == "POST":
            try:
                update_user_profile(
                    user_id=current_user["id"],
                    name=request.form.get("name", ""),
                    department=request.form.get("department", ""),
                    title=request.form.get("title", ""),
                    phone=request.form.get("phone", ""),
                    wechat_id=request.form.get("wechat_id", ""),
                    avatar=request.form.get("avatar", ""),
                )
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("edit_profile"))

            write_operation_log(
                current_user["id"],
                "update_profile",
                "更新个人资料",
                request.remote_addr or "",
            )
            flash("个人资料已保存", "success")
            return redirect(url_for("me"))

        return render_template("edit_profile.html")

    @app.route("/consultations/new", methods=["GET", "POST"])
    @login_required
    def new_consultation(current_user: Dict[str, Any]):
        """发起提问：支持同级、上级和跨级医院。"""
        target_hospitals = get_available_target_hospitals(current_user)
        visible_level_options = get_visible_level_options()
        default_visible_levels = get_default_visible_levels(current_user["hospital_level"])
        invite_user_id = request.args.get("invite_user_id", "")
        source_group_id = request.args.get("source_group_id", "")
        invite_user = None
        source_group = None
        if invite_user_id.isdigit() and is_friend(current_user["id"], int(invite_user_id)):
            invite_user = get_doctor_profile(int(invite_user_id))
        else:
            invite_user_id = ""
        if source_group_id.isdigit() and is_group_member(int(source_group_id), current_user["id"]):
            source_group = get_group_detail(int(source_group_id))
        else:
            source_group_id = ""

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            target_hospital_id = request.form.get("target_hospital_id", "").strip()
            post_invite_user_id = request.form.get("invite_user_id", "").strip()
            post_source_group_id = request.form.get("source_group_id", "").strip()
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
                    invite_user=invite_user,
                    invite_user_id=invite_user_id,
                    source_group=source_group,
                    source_group_id=source_group_id,
                )
            if not content:
                flash("病情描述不能为空", "danger")
                return render_template(
                    "new_consultation.html",
                    target_hospitals=target_hospitals,
                    visible_level_options=visible_level_options,
                    default_visible_levels=default_visible_levels,
                    invite_user=invite_user,
                    invite_user_id=invite_user_id,
                    source_group=source_group,
                    source_group_id=source_group_id,
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
                    invite_user=invite_user,
                    invite_user_id=invite_user_id,
                    source_group=source_group,
                    source_group_id=source_group_id,
                )

            valid_invite_user_id: Optional[int] = None
            if post_invite_user_id.isdigit() and is_friend(
                current_user["id"], int(post_invite_user_id)
            ):
                valid_invite_user_id = int(post_invite_user_id)

            valid_source_group_id: Optional[int] = None
            if post_source_group_id.isdigit() and is_group_member(
                int(post_source_group_id), current_user["id"]
            ):
                valid_source_group_id = int(post_source_group_id)

            consultation_id = create_consultation(
                title=title,
                content=content,
                current_user=current_user,
                target_hospital_id=int(target_hospital_id),
                visible_levels=visible_levels,
                attachment=attachment,
            )
            ensure_case_group(
                consultation_id,
                current_user,
                int(target_hospital_id),
                valid_invite_user_id,
                valid_source_group_id,
            )
            write_operation_log(
                current_user["id"],
                "create_consultation",
                f"发起病例会诊：{title}",
                request.remote_addr or "",
            )
            flash("提问提交成功，等待对方医院回复", "success")
            return redirect(url_for("my_consultations"))

        return render_template(
            "new_consultation.html",
            target_hospitals=target_hospitals,
            visible_level_options=visible_level_options,
            default_visible_levels=default_visible_levels,
            invite_user=invite_user,
            invite_user_id=invite_user_id,
            source_group=source_group,
            source_group_id=source_group_id,
        )

    @app.route("/my-consultations")
    @login_required
    def my_consultations(current_user: Dict[str, Any]):
        """我的提问列表。"""
        consultations = get_my_consultations(current_user["id"])
        return render_template("my_consultations.html", consultations=consultations)

    @app.route("/friends")
    @login_required
    def friends(current_user: Dict[str, Any]):
        """好友列表：按分组展示实名好友。"""
        grouped_friends = get_friends_grouped(current_user["id"])
        friend_count = sum(len(items) for items in grouped_friends.values())
        active_group_count = sum(1 for items in grouped_friends.values() if items)
        pending_count = len(get_pending_friend_requests(current_user["id"]))
        return render_template(
            "friends.html",
            grouped_friends=grouped_friends,
            friend_count=friend_count,
            active_group_count=active_group_count,
            pending_count=pending_count,
        )

    @app.route("/friends/add", methods=["GET", "POST"])
    @login_required
    def add_friend(current_user: Dict[str, Any]):
        """添加好友：支持手机号或微信号搜索并发送验证申请。"""
        query = request.args.get("q", "").strip()
        results = search_users(query, current_user["id"]) if query else []
        if request.method == "POST":
            friend_id = request.form.get("friend_id", "").strip()
            apply_msg = request.form.get("apply_msg", "").strip()
            if not friend_id.isdigit():
                flash("请选择有效的医生", "danger")
                return redirect(url_for("add_friend"))
            message = send_friend_request(
                current_user,
                int(friend_id),
                apply_msg,
                request.remote_addr or "",
            )
            flash(message, "success" if "已发送" in message else "warning")
            return redirect(url_for("add_friend"))
        return render_template("add_friend.html", query=query, results=results)

    @app.route("/friends/search-json")
    @login_required
    def search_friend_json(current_user: Dict[str, Any]):
        """添加好友页的轻量实时搜索接口。"""
        query = request.args.get("q", "").strip()
        data = []
        for user in search_users(query, current_user["id"]):
            data.append(
                {
                    "id": user["id"],
                    "name": user["name"],
                    "avatar": avatar_text(user),
                    "hospital": user["hospital_name"],
                    "department": user["department"],
                    "title": user["title"],
                    "phone": user["phone"],
                    "wechat_id": user["wechat_id"],
                    "status": get_relation_status(current_user["id"], user["id"]),
                }
            )
        return jsonify(data)

    @app.route("/friends/requests", methods=["GET", "POST"])
    @login_required
    def friend_requests(current_user: Dict[str, Any]):
        """新的朋友：处理好友申请。"""
        if request.method == "POST":
            relation_id = request.form.get("relation_id", "").strip()
            action = request.form.get("action", "").strip()
            group_name = request.form.get("friend_group", "我的好友").strip()
            if not relation_id.isdigit() or action not in ("accept", "reject"):
                flash("请求参数无效", "danger")
                return redirect(url_for("friend_requests"))
            message = respond_friend_request(
                current_user,
                int(relation_id),
                action,
                group_name,
                request.remote_addr or "",
            )
            flash(message, "success")
            return redirect(url_for("friend_requests"))
        return render_template(
            "friend_requests.html",
            requests=get_pending_friend_requests(current_user["id"]),
            friend_groups=get_friend_groups(current_user["id"]),
        )

    @app.route("/friends/groups", methods=["GET", "POST"])
    @login_required
    def friend_group_manager(current_user: Dict[str, Any]):
        """好友分组管理：新建自定义分组。"""
        if request.method == "POST":
            group_name = request.form.get("group_name", "").strip()
            if not group_name:
                flash("分组名称不能为空", "danger")
                return redirect(url_for("friend_group_manager"))
            create_friend_group(current_user["id"], group_name)
            flash("好友分组已保存", "success")
            return redirect(url_for("friend_group_manager"))
        return render_template(
            "friend_groups.html",
            friend_groups=get_friend_groups(current_user["id"]),
        )

    @app.route("/friends/<int:friend_id>/profile")
    @login_required
    def friend_profile(current_user: Dict[str, Any], friend_id: int):
        """好友资料页：实名信息和专家库入口。"""
        profile = get_doctor_profile(friend_id)
        if profile is None:
            abort(404)
        conn = get_connection()
        try:
            expert = conn.execute(
                """
                SELECT id FROM expert_library
                WHERE user_id = ? AND expert_user_id = ?
                """,
                (current_user["id"], friend_id),
            ).fetchone()
        finally:
            conn.close()
        return render_template(
            "friend_profile.html",
            profile=profile,
            relation_status=get_relation_status(current_user["id"], friend_id),
            is_expert=expert is not None,
        )

    @app.route("/friends/<int:friend_id>/move-group", methods=["POST"])
    @login_required
    def move_friend_group(current_user: Dict[str, Any], friend_id: int):
        """移动好友分组。"""
        if not is_friend(current_user["id"], friend_id):
            flash("只能移动已通过的好友", "danger")
            return redirect(url_for("friends"))
        move_friend_to_group(
            current_user["id"],
            friend_id,
            request.form.get("friend_group", "我的好友"),
        )
        flash("好友分组已更新", "success")
        return redirect(url_for("friends"))

    @app.route("/friends/<int:friend_id>/delete", methods=["POST"])
    @login_required
    def delete_friend(current_user: Dict[str, Any], friend_id: int):
        """软删除好友关系，历史消息和审计日志仍保留。"""
        soft_delete_friend(current_user["id"], friend_id)
        flash("已删除好友，历史消息仍按医疗留痕要求保留", "success")
        return redirect(url_for("friends"))

    @app.route("/friends/<int:friend_id>/expert", methods=["POST"])
    @login_required
    def add_expert(current_user: Dict[str, Any], friend_id: int):
        """把好友加入专家库。"""
        if not is_friend(current_user["id"], friend_id):
            flash("只能把好友加入专家库", "danger")
            return redirect(url_for("friend_profile", friend_id=friend_id))
        add_to_expert_library(current_user["id"], friend_id)
        flash("已加入专家库", "success")
        return redirect(url_for("friend_profile", friend_id=friend_id))

    @app.route("/chats/private/<int:friend_id>", methods=["GET", "POST"])
    @login_required
    def private_chat(current_user: Dict[str, Any], friend_id: int):
        """一对一私聊。只有好友之间可以进入。"""
        if not is_friend(current_user["id"], friend_id):
            flash("只有好友之间可以私聊", "danger")
            return redirect(url_for("friends"))
        friend = get_doctor_profile(friend_id)
        if friend is None:
            abort(404)
        if request.method == "POST":
            content = request.form.get("content", "").strip()
            if not content:
                flash("消息内容不能为空", "danger")
                return redirect(url_for("private_chat", friend_id=friend_id))
            send_private_message(
                current_user["id"],
                friend_id,
                content,
                request.remote_addr or "",
            )
            flash("消息已发送", "success")
            return redirect(url_for("private_chat", friend_id=friend_id))
        mark_private_messages_read(current_user["id"], friend_id)
        return render_template(
            "private_chat.html",
            friend=friend,
            messages=get_private_messages(current_user["id"], friend_id),
        )

    @app.route("/groups")
    @login_required
    def groups(current_user: Dict[str, Any]):
        """我的群聊列表。"""
        selected_type = request.args.get("type", "全部")
        return render_template(
            "groups.html",
            groups=get_my_groups(current_user["id"], selected_type),
            selected_type=selected_type,
        )

    @app.route("/groups/new", methods=["GET", "POST"])
    @login_required
    def new_group(current_user: Dict[str, Any]):
        """新建群聊：从好友列表勾选成员。"""
        friends_list = get_friends(current_user["id"])
        if request.method == "POST":
            group_name = request.form.get("group_name", "").strip()
            group_type = request.form.get("group_type", "work").strip()
            description = request.form.get("description", "").strip()
            member_ids = [
                int(value)
                for value in request.form.getlist("member_ids")
                if value.isdigit() and is_friend(current_user["id"], int(value))
            ]
            if not group_name:
                flash("群名称不能为空", "danger")
                return redirect(url_for("new_group"))
            group_id = create_group(
                current_user,
                group_name,
                group_type,
                description,
                member_ids,
            )
            flash("群聊创建成功", "success")
            return redirect(url_for("group_chat", group_id=group_id))
        return render_template("new_group.html", friends=friends_list)

    @app.route("/groups/<int:group_id>", methods=["GET", "POST"])
    @login_required
    def group_chat(current_user: Dict[str, Any], group_id: int):
        """群聊聊天页。"""
        if not is_group_member(group_id, current_user["id"]):
            flash("您不是该群成员", "danger")
            return redirect(url_for("groups"))
        group = get_group_detail(group_id)
        if group is None:
            abort(404)
        if request.method == "POST":
            content = request.form.get("content", "").strip()
            try:
                attachment = save_attachment(request.files.get("group_attachment"))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("group_chat", group_id=group_id))
            if not content and not attachment["path"]:
                flash("消息内容和附件不能同时为空", "danger")
                return redirect(url_for("group_chat", group_id=group_id))
            send_group_message(
                group_id,
                current_user,
                content,
                attachment,
                request.remote_addr or "",
            )
            flash("消息已发送", "success")
            return redirect(url_for("group_chat", group_id=group_id))
        mark_group_read(group_id, current_user["id"])
        return render_template(
            "group_chat.html",
            group=group,
            members=get_group_members(group_id),
            messages=get_group_messages(group_id),
            current_role=get_group_role(group_id, current_user["id"]),
        )

    @app.route("/groups/<int:group_id>/settings", methods=["GET", "POST"])
    @login_required
    def group_settings(current_user: Dict[str, Any], group_id: int):
        """群设置：查看成员，群主可修改群资料和移除成员。"""
        if not is_group_member(group_id, current_user["id"]):
            flash("您不是该群成员", "danger")
            return redirect(url_for("groups"))
        group = get_group_detail(group_id)
        if group is None:
            abort(404)
        role = get_group_role(group_id, current_user["id"])
        if request.method == "POST":
            if role != "owner":
                flash("只有群主可以修改群设置", "danger")
                return redirect(url_for("group_settings", group_id=group_id))
            group_name = request.form.get("group_name", "").strip()
            description = request.form.get("description", "").strip()
            if not group_name:
                flash("群名称不能为空", "danger")
                return redirect(url_for("group_settings", group_id=group_id))
            conn = get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        UPDATE groups
                        SET group_name = ?, description = ?
                        WHERE id = ?
                        """,
                        (group_name, description, group_id),
                    )
            finally:
                conn.close()
            write_operation_log(current_user["id"], "update_group", f"修改群设置：{group_id}")
            flash("群设置已更新", "success")
            return redirect(url_for("group_settings", group_id=group_id))
        return render_template(
            "group_settings.html",
            group=group,
            members=get_group_members(group_id),
            current_role=role,
        )

    @app.route("/groups/<int:group_id>/members/<int:user_id>/remove", methods=["POST"])
    @login_required
    def group_remove_member(current_user: Dict[str, Any], group_id: int, user_id: int):
        """群主从群聊中移除成员。"""
        if not is_group_member(group_id, current_user["id"]):
            flash("您不是该群成员", "danger")
            return redirect(url_for("groups"))
        group = get_group_detail(group_id)
        if group is None:
            abort(404)
        message = remove_group_member(group_id, current_user["id"], user_id)
        flash(message, "success" if message.startswith("成员已") else "danger")
        return redirect(url_for("group_settings", group_id=group_id))

    @app.route("/groups/<int:group_id>/invite", methods=["GET", "POST"])
    @login_required
    def group_invite(current_user: Dict[str, Any], group_id: int):
        """邀请好友入群。"""
        if not is_group_member(group_id, current_user["id"]):
            flash("您不是该群成员", "danger")
            return redirect(url_for("groups"))
        group = get_group_detail(group_id)
        if group is None:
            abort(404)
        if request.method == "POST":
            member_ids = [
                int(value)
                for value in request.form.getlist("member_ids")
                if value.isdigit() and is_friend(current_user["id"], int(value))
            ]
            added = invite_users_to_group(group_id, current_user["id"], member_ids)
            flash(f"已邀请 {added} 名好友入群", "success")
            return redirect(url_for("group_settings", group_id=group_id))
        existing_user_ids = set(get_group_user_ids(group_id))
        available_friends = [
            friend
            for friend in get_friends(current_user["id"])
            if friend["id"] not in existing_user_ids
        ]
        return render_template(
            "invite_friends.html",
            group=group,
            friends=available_friends,
            mode="group",
        )

    @app.route("/groups/<int:group_id>/leave", methods=["POST"])
    @login_required
    def group_leave(current_user: Dict[str, Any], group_id: int):
        """退出群聊。"""
        message = leave_group(group_id, current_user["id"])
        flash(message, "danger" if "无法" in message else "success")
        return redirect(url_for("groups"))

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
            case_group=get_case_group(consultation_id),
            no_permission=False,
        )

    @app.route("/consultations/<int:consultation_id>/case-chat")
    @login_required
    def case_chat(current_user: Dict[str, Any], consultation_id: int):
        """进入病例聊天室；没有绑定群时自动创建病例讨论群。"""
        consultation = get_consultation_detail(consultation_id)
        if consultation is None:
            abort(404)
        if not can_view_consultation(consultation, current_user):
            flash("您没有权限查看该内容", "danger")
            return redirect(url_for("home"))
        group_id = ensure_case_group(
            consultation_id,
            current_user,
            consultation["target_hospital_id"],
        )
        return redirect(url_for("group_chat", group_id=group_id))

    @app.route("/consultations/<int:consultation_id>/invite-friends", methods=["GET", "POST"])
    @login_required
    def invite_friends_to_case(current_user: Dict[str, Any], consultation_id: int):
        """从病例详情页邀请好友加入病例聊天室。"""
        consultation = get_consultation_detail(consultation_id)
        if consultation is None:
            abort(404)
        if not can_view_consultation(consultation, current_user):
            flash("您没有权限查看该内容", "danger")
            return redirect(url_for("home"))
        group_id = ensure_case_group(
            consultation_id,
            current_user,
            consultation["target_hospital_id"],
        )
        if request.method == "POST":
            member_ids = [
                int(value)
                for value in request.form.getlist("member_ids")
                if value.isdigit() and is_friend(current_user["id"], int(value))
            ]
            added = invite_users_to_group(group_id, current_user["id"], member_ids)
            flash(f"已邀请 {added} 名好友加入病例聊天室", "success")
            return redirect(url_for("case_chat", consultation_id=consultation_id))
        return render_template(
            "invite_friends.html",
            group=get_group_detail(group_id),
            consultation=consultation,
            friends=get_friends(current_user["id"]),
            mode="case",
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
            conn = get_connection()
            try:
                group_row = conn.execute(
                    """
                    SELECT group_id, attachment_name
                    FROM group_messages
                    WHERE attachment_path = ?
                    """,
                    (filename,),
                ).fetchone()
            finally:
                conn.close()

            if group_row is None:
                abort(404)
            if not is_group_member(group_row["group_id"], current_user["id"]):
                abort(403)
            return send_from_directory(
                UPLOAD_DIR,
                filename,
                as_attachment=True,
                download_name=group_row["attachment_name"] or filename,
            )

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
        write_operation_log(
            current_user["id"],
            "reply_consultation",
            f"回复病例会诊：{consultation_id}",
            request.remote_addr or "",
        )
        flash("回复提交成功", "success")
        return redirect(url_for("consultation_detail", consultation_id=consultation_id))


if __name__ == "__main__":
    init_database()
    if app is None:
        raise RuntimeError("未安装 Flask，请先运行：pip install flask")
    app.run(host="0.0.0.0", port=5000, debug=False)
