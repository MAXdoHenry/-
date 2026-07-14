# 医联体分级疑难病例会诊系统 Demo

这是一个用于课程演示和功能验证的纯网页 Demo。医生端不用安装客户端，只需要浏览器访问部署好的网址即可使用。

## 功能概览

- 医生登录
- 发起疑难病例会诊
- 同级、上级、跨级医院请教
- 按可见等级控制病例查看权限
- 病例回复和回复通知
- 附件上传，支持图片、视频和文档
- 好友申请、好友列表、手机号或微信号搜索
- 群聊、病例讨论群、群聊附件
- FastAPI WebSocket 独立实时聊天服务
- 医疗敏感内容提示
- 操作日志留痕
- 个人资料填写
- 群主退出时自动转让群主

## 目录结构

```text
app.py                 Flask 主站
ws_service.py          FastAPI WebSocket 实时聊天服务
requirements.txt       Python 依赖
templates/             HTML 页面模板
.gitignore             Git 忽略规则
```

以下文件是运行后自动生成的，不需要上传 GitHub：

```text
hospital_consult.db
uploads/
__pycache__/
*.log
```

## 本地运行

进入项目目录后安装依赖：

```bash
pip install -r requirements.txt
```

启动 Flask 主站：

```bash
python app.py
```

再打开另一个终端，启动实时聊天服务：

```bash
python -m uvicorn ws_service:app --host 0.0.0.0 --port 8001
```

浏览器访问：

```text
http://127.0.0.1:5000
```

## 演示账号

演示账号密码统一为：

```text
123456
```

预设医生：

```text
张主任
李医生
王医生
赵医生
```

手机号或微信号搜索演示：

```text
1、2、3、4
```

## 验证方式

1. 打开 `http://127.0.0.1:5000`
2. 使用任意预设医生登录
3. 进入“我”，点击“填写个人信息”
4. 进入“通讯录”，查看好友和群聊
5. 进入群聊，测试文字消息和附件上传
6. 进入“回信”，查看可处理的会诊提问
7. 发起病例会诊后，用被请教医院医生登录并回复

## 部署说明

如果只是让别人使用 Demo，对方不需要安装 Python，也不需要执行命令。只需要把项目部署在一台服务器或电脑上，其他人用浏览器访问部署地址。

同一局域网访问示例：

```text
http://部署机器IP:5000
```

正式部署建议：

- 使用云服务器或内网服务器
- 使用 PostgreSQL 或 MySQL 替代 SQLite
- 使用对象存储保存附件
- 使用 HTTPS
- 使用正式账号体系和权限审计
- 使用进程管理工具同时托管 Flask 主站和 WebSocket 服务

## GitHub 上传建议

上传前只保留：

```text
app.py
ws_service.py
requirements.txt
README.md
.gitignore
templates/
```

不要上传数据库、上传附件、日志和缓存文件。
