# -*- coding: utf-8 -*-
"""
minecraftconsole —— Vue 工作流编排面板：工作流引擎 + Flask 配置 API + 前端接管

本模块三个职责：
1. WorkflowEngine —— 轻量级可视化工作流引擎：
   - 工作流持久化于 ctx.get_data_dir()/workflows.json（JSON 列表）
   - 节点类型：trigger(入口) / action(执行) / condition(条件分支) / end(结束)
   - match() 按 trigger 的 match 匹配指令文本；run() 沿 edges 遍历执行
2. register_routes() —— 用底层 url_map.add() 向框架 Flask 注册
   /api/mc/workflow 系列配置 API（热重载安全，参照 custom_ui 范式）
3. takeover() —— ctx.override_webui() 接管整个 Web 前端，并注册
   /minecraftconsole/ 与 /minecraftconsole/<path> 静态路由服务 web/ 目录
4. register_auth_routes() —— 注册 /api/mc/auth 登录授权端点（login/me/logout），
   复用框架 admin_users 表（bcrypt 哈希 + 2048 位 token + HttpOnly Cookie zcbot_token），
   与框架 /api/login 签发的 token 完全互通
5. register_fs_routes() —— 注册 POST /api/mc/fs 文件管理端点（需登录），
   将 {op,path,...} 翻译为 agent FS 指令（FS|READ/LIST/EXISTS/WRITE/APPEND/REPLACE/SETLINE，
   路径与数据 base64，遵循旧插件 AstrBotRconBridge FS 协议）并同步等回执

鉴权约定：/api/mc/workflow CRUD、/api/mc/fs 均需登录
（Authorization Bearer 或 Cookie zcbot_token，参照 custom_ui _auth 范式）。

数据结构：
    workflow = {
        id, name, enabled,
        nodes: [{id, type, label, x, y, config}],
        edges: [{id, from, to, port}],
    }
    - trigger 节点 config: {match, match_type: prefix|regex}
    - action  节点 config: {action: exec|query|fs, command}
    - condition 节点 config: {test, match_type: contains|regex, true_port, false_port}
    - end     节点 config: {return: true}
    - 边 port：condition 节点用 port 区分 true/false 分支；其它节点 port 为空串
"""

import asyncio
import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta

NODE_TRIGGER = "trigger"        # 入口：匹配指令文本
NODE_ACTION = "action"          # 动作：把 command 交给 send_fn 执行
NODE_CONDITION = "condition"    # 条件：对 {last} 做 contains/regex 判断，走 true/false 端口
NODE_END = "end"                # 结束：返回累积结果

MAX_STEPS = 200                 # 单次 run 最大步数（防环路死循环）


# ------------------------------------------------------------------ 工作流引擎
class WorkflowEngine:
    """可视化工作流引擎：持久化 / 匹配 / 执行 / API。"""

    def __init__(self, ctx):
        self.ctx = ctx
        # 工作流数据存插件数据目录（plugins_dat/minecraftconsole/workflows.json），更新不丢
        self.path = os.path.join(ctx.get_data_dir(), "workflows.json")
        self._workflows = []     # list[dict]
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self):
        """从 workflows.json 加载全部工作流（文件缺失/损坏时回退空列表）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._workflows = data if isinstance(data, list) else []
        except Exception:
            self._workflows = []

    def save(self) -> bool:
        """把当前工作流列表写回 workflows.json。"""
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._workflows, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self.ctx.log(f"保存工作流失败: {e}", level="error")
            return False

    # ------------------------------------------------------------ 集合操作
    def all(self) -> list:
        """返回全部工作流（浅拷贝，避免外部直接改内部列表）。"""
        return list(self._workflows)

    def get(self, wid: str):
        """按 id 取工作流，未找到返回 None。"""
        return next((w for w in self._workflows if w.get("id") == wid), None)

    def upsert(self, workflow: dict) -> bool:
        """新建/覆盖：同 id 覆盖，否则追加。返回保存是否成功。"""
        if not isinstance(workflow, dict) or not workflow.get("id"):
            return False
        workflow.setdefault("nodes", [])
        workflow.setdefault("edges", [])
        workflow.setdefault("name", workflow["id"])
        workflow.setdefault("enabled", True)
        for i, w in enumerate(self._workflows):
            if w.get("id") == workflow["id"]:
                self._workflows[i] = workflow
                return self.save()
        self._workflows.append(workflow)
        return self.save()

    def delete(self, wid: str) -> bool:
        """按 id 删除工作流，返回保存是否成功。"""
        before = len(self._workflows)
        self._workflows = [w for w in self._workflows if w.get("id") != wid]
        if len(self._workflows) != before:
            return self.save()
        return True

    # ------------------------------------------------------------ 匹配
    def _trigger_hit(self, wf, text) -> bool:
        """判断单条指令文本是否命中某启用工作流的 trigger。"""
        if not wf.get("enabled", True):
            return False
        trigger = next((n for n in wf.get("nodes", [])
                        if n.get("type") == NODE_TRIGGER), None)
        if not trigger:
            return False
        cfg = trigger.get("config") or {}
        match_str = str(cfg.get("match") or "").strip()
        if not match_str:
            return False
        try:
            if (cfg.get("match_type") or "prefix") == "regex":
                return bool(re.search(match_str, text))
            return text.startswith(match_str)
        except re.error:
            return False

    def match(self, instruction):
        """按 trigger 匹配指令文本（去掉开头 /）。
        返回首个命中的「启用」工作流，无命中返回 None（兼容单命中使用）。"""
        if not instruction:
            return None
        text = str(instruction).strip().lstrip("/")
        for wf in self._workflows:
            if self._trigger_hit(wf, text):
                return wf
        return None

    def match_all(self, instruction):
        """匹配全部命中的启用工作流（同一条消息可同时触发多个流程图）。"""
        if not instruction:
            return []
        text = str(instruction).strip().lstrip("/")
        return [wf for wf in self._workflows if self._trigger_hit(wf, text)]

    # ------------------------------------------------------------ 执行
    async def run(self, workflow: dict, instruction: str, send_fn, variables: dict = None) -> str:
        """从 trigger 开始按 edges 遍历执行工作流。

        send_fn: async (command: str) -> str —— 执行动作节点命令并返回结果文本。
        variables: 可选占位符字典（如 {"player": "Steve"}），动作节点命令中的
            {key} 会被替换为对应值（与 {last} 同样处理）。常用于把绑定的 MC 玩家名
            注入到指令里。
        - action 节点：command 内 {last} 替换为上一结果、{key} 替换为变量，再 send
        - condition 节点：对 {last} 做 contains/regex 判断，走 true/false 端口
        - end 节点：返回累积结果（所有 action 结果按行拼接）
        环路防护：节点访问去重 + 最大步数上限。
        """
        nodes = workflow.get("nodes") or []
        edges = workflow.get("edges") or []
        by_id = {}
        for n in nodes:
            if n.get("id"):
                by_id[n["id"]] = n
        # 出边索引：from -> [edge, ...]
        out_edges = {}
        for e in edges:
            out_edges.setdefault(e.get("from"), []).append(e)

        start = next((n for n in nodes if n.get("type") == NODE_TRIGGER), None)
        if start is None:
            return "工作流缺少 trigger 节点"

        cur = start
        last = ""                # {last} 变量的「上一结果」
        results = []             # 累积的动作结果
        visited = set()          # 环路防护：同一步路径不重复访问节点
        steps = 0

        while cur is not None and steps < MAX_STEPS:
            steps += 1
            nid = cur.get("id")
            if nid in visited:
                # 环路：终止并把已累积的结果返回，避免死循环
                return "\n".join(filter(None, results)) or \
                    f"[cycle] 检测到环路，终止于节点 {cur.get('label') or nid}"
            visited.add(nid)

            ntype = cur.get("type")
            cfg = cur.get("config") or {}
            label = cur.get("label") or nid

            if ntype == NODE_ACTION:
                # 动作：{last} 替换后交给 send_fn（异步发命令），返回文本成为新的 last
                cmd = str(cfg.get("command") or "").replace("{last}", last)
                if variables:
                    for k, v in variables.items():
                        if v is not None:
                            cmd = cmd.replace("{" + k + "}", str(v))
                if cmd:
                    last = (await send_fn(cmd)) or ""
                    results.append(last)
                else:
                    self.ctx.log(f"[wf] 动作节点 {label} 无 command，跳过")
                nxt = self._next_edge(out_edges, nid, None)
            elif ntype == NODE_CONDITION:
                # 条件：对 last 做 contains/regex 判断，走 true/false 端口
                test = str(cfg.get("test") or "")
                mt = cfg.get("match_type") or "contains"
                if mt == "regex":
                    try:
                        matched = re.search(test, last) is not None
                    except re.error:
                        matched = False
                else:
                    matched = test in last
                port = "true" if matched else "false"
                self.ctx.log(f"[wf] 条件节点 {label} {matched and 'TRUE' or 'FALSE'}（last={last!r}）")
                nxt = self._next_edge(out_edges, nid, port)
            elif ntype == NODE_END:
                # 结束：返回累积结果（return:false 时仅返回最后一步）
                final = "\n".join(filter(None, results)) if results else (last or "")
                if not cfg.get("return", True):
                    final = last or ""
                self.ctx.log(f"[wf] 结束节点 {label}，共执行 {len(results)} 个动作")
                return final
            else:
                # 未知/trigger 节点：仅透传（trigger 是起点，不需要动作）
                nxt = self._next_edge(out_edges, nid, None)

            if nxt is None:
                break            # 无后续节点 → 流程自然结束
            cur = by_id.get(nxt.get("to"))
            if cur is None:
                break            # 出边指向不存在的节点（数据不一致），容错终止

        return "\n".join(filter(None, results)) if results else (last or "")

    @staticmethod
    def _next_edge(out_edges: dict, nid: str, port):
        """取某节点的出边：condition 按 port(true/false) 取；其它节点取 port 为空的首条。
        没有匹配边时回退到该节点的第一条出边，保证面板连线「不设端口」也能跑。"""
        edges = out_edges.get(nid) or []
        if port is not None:
            return next((e for e in edges if e.get("port") == port), None)
        return next((e for e in edges if not e.get("port")), None) or (edges[0] if edges else None)

    # ------------------------------------------------------------ Flask API
    def register_routes(self) -> bool:
        """向框架 Flask 注册 /api/mc/workflow 系列配置 API。
        用底层 url_map.add() 注册（参照 custom_ui），插件热重载/重复加载时安全。"""
        try:
            app = self.ctx._framework.web_server.app
            if app is None:
                self.ctx.log("web_server.app 为空，跳过工作流 API 注册", level="warning")
                return False
        except Exception as e:
            self.ctx.log(f"获取 Flask app 失败: {e}", level="error")
            return False

        from flask import jsonify, request
        from werkzeug.routing import Rule

        def _ok(data=None, msg="ok"):
            return jsonify({"code": 0, "msg": msg, "data": data})

        def _err(msg, code=1, http=200):
            return jsonify({"code": code, "msg": msg}), http

        # 鉴权说明：以下接口全部需要登录（Authorization Bearer 或 zcbot_token Cookie，
        # 与 custom_ui 范式一致），未登录统一返回 401。token 由 /api/mc/auth/login 签发，
        # 复用框架 admin_users 表（见 register_auth_routes）。
        def _api_list():
            """GET /api/mc/workflow —— 全部工作流（需登录）"""
            if not _require_auth(self.ctx, request):
                return _err('未登录或令牌无效', 401, 401)
            return _ok(self.all())

        def _api_save():
            """POST /api/mc/workflow —— 新建/覆盖（body 为 workflow JSON，需登录）"""
            if not _require_auth(self.ctx, request):
                return _err('未登录或令牌无效', 401, 401)
            try:
                wf = request.get_json(force=True, silent=True) or {}
            except Exception:
                wf = {}
            if not isinstance(wf, dict) or not wf.get("id"):
                return _err("无效的工作流 JSON（缺少 id）", 1)
            if not isinstance(wf.get("nodes"), list):
                wf["nodes"] = []
            if not isinstance(wf.get("edges"), list):
                wf["edges"] = []
            if self.upsert(wf):
                return _ok(wf, "已保存")
            return _err("保存失败", 1)

        def _api_delete(wid):
            """DELETE /api/mc/workflow/<id> —— 删除工作流（需登录）"""
            if not _require_auth(self.ctx, request):
                return _err('未登录或令牌无效', 401, 401)
            if self.delete(wid):
                return _ok(None, "已删除")
            return _err("删除失败", 1)

        def _api_run(wid):
            """POST /api/mc/workflow/<id>/run —— 面板「测试运行」（需登录）
            body {instruction}；不实际发命令，走注入的 mock send_fn，仅演示引擎流转。"""
            if not _require_auth(self.ctx, request):
                return _err('未登录或令牌无效', 401, 401)
            wf = self.get(wid)
            if wf is None:
                return _err("工作流不存在", 404, 404)
            try:
                data = request.get_json(force=True, silent=True) or {}
            except Exception:
                data = {}
            instruction = str(data.get("instruction") or "")

            async def _mock_send(cmd):
                """测试用假执行器：不向 MC 下发，返回模拟结果文本。"""
                return f"[mock] {cmd}"

            try:
                # Flask 运行在独立 web-server 线程，无事件循环，asyncio.run 安全
                final = asyncio.run(self.run(wf, instruction, _mock_send))
            except Exception as e:
                return _err(f"运行异常: {e}", 1)
            return _ok({
                "workflow": wf.get("name") or wf["id"],
                "instruction": instruction,
                "result": final,
            })

        rules = [
            ("/api/mc/workflow", ["GET"], _api_list),
            ("/api/mc/workflow", ["POST"], _api_save),
            ("/api/mc/workflow/<wid>", ["DELETE"], _api_delete),
            ("/api/mc/workflow/<wid>/run", ["POST"], _api_run),
        ]
        existing = {str(r.rule) for r in app.url_map.iter_rules()}
        added = 0
        for rule_path, methods, fn in rules:
            endpoint = f"mc_workflow_{fn.__name__}"
            if rule_path in existing:
                # 已注册过：仅刷新 view 函数（重载后函数对象变化）
                app.view_functions[endpoint] = fn
                continue
            app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
            app.view_functions[endpoint] = fn
            added += 1
        self.ctx.log(f"工作流 API 路由注册完成（新增 {added} 条）")
        return True


# ------------------------------------------------------------------ 前端接管
def takeover(ctx) -> bool:
    """接管整个 Web 前端：根路由 / 由框架 redirect 到 /minecraftconsole/（框架行为）。

    注册 /minecraftconsole/ 与 /minecraftconsole/<path> 静态路由，
    用 send_from_directory 服务插件 web/ 目录（workflow.html 等），并做路径穿越防护。
    """
    try:
        app = ctx._framework.web_server.app
        if app is None:
            ctx.log("web_server.app 为空，无法注册接管路由", level="warning")
            return False
    except Exception as e:
        ctx.log(f"获取 Flask app 失败: {e}", level="error")
        return False

    from flask import jsonify, send_from_directory
    from werkzeug.routing import Rule

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def _serve_entry():
        """/minecraftconsole/ —— 1Panel 风格面板入口（跳 /minecraftconsole/panel/ 保证相对资源正确；缺面板回退旧壳）"""
        from flask import redirect
        if os.path.isfile(os.path.join(web_dir, "panel", "index.html")):
            return redirect("panel/index.html", code=302)
        return send_from_directory(web_dir, "console.html")

    def _serve_path(path):
        """/minecraftconsole/<path> —— web/ 目录静态资源（防目录穿越）
        用 posixpath 规范化 URL 路径（避免 Windows normpath 转反斜杠误判非法）。"""
        from posixpath import normpath as pnorm
        norm = pnorm(path)
        if (norm.startswith("..") or norm.startswith("/") or "\\" in norm
                or ":" in norm):
            return jsonify({"code": 400, "msg": "非法路径"}), 400
        full = os.path.join(web_dir, norm.replace("/", os.sep))
        if not os.path.isfile(full):
            return jsonify({"code": 404, "msg": "资源不存在"}), 404
        return send_from_directory(web_dir, norm)

    rules = [
        ("/minecraftconsole/", ["GET"], _serve_entry),
        ("/minecraftconsole/<path:path>", ["GET"], _serve_path),
    ]
    existing = {str(r.rule) for r in app.url_map.iter_rules()}
    added = 0
    for rule_path, methods, fn in rules:
        endpoint = f"minecraftconsole_static_{fn.__name__}"
        if rule_path in existing:
            app.view_functions[endpoint] = fn
            continue
        app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
        app.view_functions[endpoint] = fn
        added += 1

    # 接管：顶掉框架默认前端，根路由 / 302 到 /minecraftconsole/
    ok = ctx.override_webui()
    ctx.log(f"minecraftconsole 已接管 Web 前端（新增 {added} 条静态路由）"
            if ok else "接管失败（插件未加载）")
    return ok


# ------------------------------------------------------------------ 登录鉴权
def _mc_db(ctx):
    """获取框架数据库实例；不可用时返回 None。"""
    try:
        return ctx._framework.db
    except Exception:
        return None


def _session_timeout(ctx) -> int:
    """Web 会话超时（秒）：取框架 web 配置 token_timeout / session_timeout。"""
    try:
        web_cfg = ctx._framework.config.get('web', {}) or {}
    except Exception:
        web_cfg = {}
    try:
        return int(web_cfg.get('token_timeout') or web_cfg.get('session_timeout') or 86400)
    except Exception:
        return 86400


def _extract_token(request) -> str:
    """从 Authorization: Bearer xxx 头或 zcbot_token Cookie 提取 token（与框架一致）。"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return request.cookies.get('zcbot_token')


def _verify_token(ctx, token):
    """校验 token，返回 {id, username, role} 或 None。
    复用框架 admin_users 表（token 为 2048 位随机串，登录时写入），并检查过期。"""
    if not token or len(token) != 2048:
        return None
    db = _mc_db(ctx)
    if db is None:
        return None
    try:
        row = db.query_one(
            "SELECT id, username, role, is_active, token_created_at FROM admin_users WHERE token = %s",
            (token,))
    except Exception:
        return None
    if not row or not row.get('is_active'):
        return None
    created = row.get('token_created_at')
    if created:
        # SQLite 返回字符串、MySQL 返回 datetime，统一解析
        if isinstance(created, str):
            try:
                created = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
        try:
            if datetime.now() > created + timedelta(seconds=_session_timeout(ctx)):
                return None
        except Exception:
            return None
    return {'id': row['id'], 'username': row['username'], 'role': row['role']}


def _require_auth(ctx, request):
    """请求鉴权：返回当前管理员 dict 或 None（参照 custom_ui _auth 范式）。"""
    token = _extract_token(request)
    if not token:
        return None
    return _verify_token(ctx, token)


def _require_admin(ctx, request):
    """管理操作鉴权：返回当前管理员 dict 或 None。
    放行条件：role ∈ {super,admin,owner} 或框架超管，或持有 mc.admin 权限节点。"""
    admin = _require_auth(ctx, request)
    if not admin:
        return None
    uid = admin.get('id')
    role = str(admin.get('role') or '').lower()
    if role in ('super', 'admin', 'owner') or ctx.is_superuser(uid):
        return admin
    try:
        if ctx.has_perm(uid, 'mc.admin'):
            return admin
    except Exception:
        pass
    return None


def _set_token_cookie(ctx, request, resp, token: str):
    """把登录 token 同步到 HttpOnly Cookie（SameSite=Lax，与框架一致），token 为空串则清除。"""
    resp.set_cookie(
        'zcbot_token', token or '',
        max_age=_session_timeout(ctx), path='/',
        httponly=True, samesite='Lax',
        secure=bool(request.is_secure),
    )


def _json_ok(data=None, msg='ok'):
    from flask import jsonify
    return jsonify({'code': 0, 'msg': msg, 'data': data})


def _json_err(msg, code=1, http=200):
    from flask import jsonify
    return jsonify({'code': code, 'msg': msg}), http


def register_auth_routes(ctx) -> bool:
    """注册 /api/mc/auth 登录授权端点（login/me/logout）。
    复用框架 admin_users 表 + bcrypt 哈希 + 2048 位 token + HttpOnly Cookie zcbot_token，
    与框架 /api/login 签发的 token 完全互通。用 url_map.add() 注册，热重载安全。"""
    try:
        app = ctx._framework.web_server.app
        if app is None:
            ctx.log("web_server.app 为空，跳过登录授权路由注册", level='warning')
            return False
    except Exception as e:
        ctx.log(f"获取 Flask app 失败: {e}", level='error')
        return False

    from flask import jsonify, request
    from werkzeug.routing import Rule

    def _api_login():
        """POST /api/mc/auth/login —— 登录：bcrypt 校验 → 生成 token 写回 → 种 Cookie"""
        data = request.get_json(force=True, silent=True) or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '')
        if not username or not password:
            return _json_err('用户名和密码不能为空', 400, 400)
        db = _mc_db(ctx)
        if db is None:
            return _json_err('数据库不可用', 500, 500)
        try:
            row = db.query_one(
                "SELECT id, username, password_hash, role, is_active FROM admin_users WHERE username = %s",
                (username,))
        except Exception as e:
            ctx.log(f"登录查询失败: {e}", level='error')
            return _json_err('数据库错误', 500, 500)
        if not row:
            return _json_err('用户名或密码错误', 401, 401)
        if not row.get('is_active'):
            return _json_err('账号已禁用', 403, 403)
        # bcrypt 校验（框架自带依赖，懒加载避免热重载问题）
        try:
            import bcrypt
            stored = row['password_hash']
            if isinstance(stored, str):
                stored = stored.encode('utf-8')
            if isinstance(password, str):
                password = password.encode('utf-8')
            if not bcrypt.checkpw(password, stored):
                return _json_err('用户名或密码错误', 401, 401)
        except Exception as e:
            ctx.log(f"密码验证异常: {e}", level='error')
            return _json_err('密码验证失败', 500, 500)
        # 2048 位随机 token（token_hex(1024)），与框架 /api/login 完全一致
        token = secrets.token_hex(1024)
        try:
            db.execute(
                "UPDATE admin_users SET token = %s, token_created_at = NOW(), "
                "last_login_at = NOW(), last_login_ip = %s WHERE id = %s",
                (token, request.remote_addr or '', row['id']))
        except Exception as e:
            ctx.log(f"写入登录 token 失败: {e}", level='error')
            return _json_err('登录状态写入失败', 500, 500)
        try:
            ctx.audit_log(action='mc_login', target_type='minecraft', target_name='webui',
                          detail={'username': username}, result='success')
        except Exception:
            pass
        resp = jsonify({'code': 0, 'msg': '登录成功',
                        'data': {'token': token, 'username': username,
                                 'role': row.get('role', 'admin')}})
        _set_token_cookie(ctx, request, resp, token)
        return resp

    def _api_me():
        """GET /api/mc/auth/me —— 当前登录用户；未登录/令牌无效返回 401"""
        admin = _require_auth(ctx, request)
        if not admin:
            return _json_err('未登录或令牌无效', 401, 401)
        return _json_ok(admin)

    def _api_logout():
        """POST /api/mc/auth/logout —— 退出登录：清 token + 清 Cookie"""
        admin = _require_auth(ctx, request)
        if not admin:
            return _json_err('未登录或令牌无效', 401, 401)
        db = _mc_db(ctx)
        if db is not None:
            try:
                db.execute(
                    "UPDATE admin_users SET token = NULL, token_created_at = NULL WHERE id = %s",
                    (admin['id'],))
            except Exception as e:
                ctx.log(f"清除 token 失败: {e}", level='error')
        try:
            ctx.audit_log(action='mc_logout', target_type='minecraft', target_name='webui',
                          detail={'username': admin['username']}, result='success')
        except Exception:
            pass
        resp = jsonify({'code': 0, 'msg': '已退出'})
        _set_token_cookie(ctx, request, resp, '')
        return resp

    rules = [
        ('/api/mc/auth/login', ['POST'], _api_login),
        ('/api/mc/auth/me', ['GET'], _api_me),
        ('/api/mc/auth/logout', ['POST'], _api_logout),
    ]
    existing = {str(r.rule) for r in app.url_map.iter_rules()}
    added = 0
    for rule_path, methods, fn in rules:
        endpoint = f"mc_auth_{fn.__name__}"
        if rule_path in existing:
            # 已注册过：仅刷新 view 函数（重载后函数对象变化）
            app.view_functions[endpoint] = fn
            continue
        app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
        app.view_functions[endpoint] = fn
        added += 1
    ctx.log(f"登录授权 API 路由注册完成（新增 {added} 条）")
    return True


# ------------------------------------------------------------------ 文件管理（agent FS 协议）
FS_OPS = ('READ', 'LIST', 'EXISTS', 'WRITE', 'APPEND', 'REPLACE', 'SETLINE')


def _b64(s) -> str:
    """UTF-8 文本 → base64（agent FS 协议要求路径与数据 base64）。None 视为空串。"""
    if s is None:
        return ''
    return base64.b64encode(str(s).encode('utf-8')).decode('ascii')


def _build_fs_instruction(op, path, data=None, max_bytes=None, line_no=None,
                          search=None, replace=None, scope=None) -> str:
    """按 agent FS 协议拼装指令文本：FS|OP|<pathB64>[|<arg>...]（参考旧插件 AstrBotRconBridge）。"""
    parts = ['FS', op, _b64(path)]
    if op == 'READ':
        if max_bytes is not None:
            parts.append(str(max(int(max_bytes), 1)))
    elif op in ('WRITE', 'APPEND'):
        parts.append(_b64(data if data is not None else ''))
    elif op == 'REPLACE':
        parts.append(_b64(search))
        parts.append(_b64(replace))
        parts.append('all' if str(scope or 'all').strip().lower() == 'all' else 'first')
    elif op == 'SETLINE':
        parts.append(str(int(line_no or 1)))
        parts.append(_b64(data))
    return '|'.join(parts)


def register_fs_routes(ctx) -> bool:
    """注册 POST /api/mc/fs 文件管理端点（需登录）。
    将 {op,path,data?,maxBytes?,lineNo?,search?,replace?,scope?} 翻译为 agent FS 指令，
    调 console.run_instruction(cmd) 同步等回执，返回 {code:0,data:{output}}。"""
    try:
        app = ctx._framework.web_server.app
        if app is None:
            ctx.log("web_server.app 为空，跳过文件管理路由注册", level='warning')
            return False
    except Exception as e:
        ctx.log(f"获取 Flask app 失败: {e}", level='error')
        return False

    from flask import request
    from werkzeug.routing import Rule

    def _api_fs():
        """POST /api/mc/fs —— 文件操作（需登录）"""
        admin = _require_auth(ctx, request)
        if not admin:
            return _json_err('未登录或令牌无效', 401, 401)
        data = request.get_json(force=True, silent=True) or {}
        op = str(data.get('op') or '').strip().upper()
        path = str(data.get('path') or '').strip()
        if op not in FS_OPS:
            return _json_err(f'不支持的 FS 操作: {op}', 400, 400)
        if not path:
            return _json_err('缺少路径参数 path', 400, 400)
        if op in ('WRITE', 'APPEND') and data.get('data') is None:
            return _json_err('缺少写入内容 data', 400, 400)
        if op == 'REPLACE' and (data.get('search') is None or data.get('replace') is None):
            return _json_err('缺少 search / replace 参数', 400, 400)
        if op == 'SETLINE' and (data.get('lineNo') is None or data.get('data') is None):
            return _json_err('缺少 lineNo / data 参数', 400, 400)
        try:
            cmd = _build_fs_instruction(op, path, data.get('data'), data.get('maxBytes'),
                                        data.get('lineNo'), data.get('search'),
                                        data.get('replace'), data.get('scope'))
        except (TypeError, ValueError) as e:
            return _json_err(f'参数错误: {e}', 400, 400)
        console = getattr(ctx, '_mc_console', None)
        if console is None:
            return _json_err('MC 控制器未就绪', 500, 500)
        if not console.status().get('online'):
            return _json_err('MC agent 离线，文件操作无法执行', 1)
        try:
            # console.run_instruction 操作主事件循环的 writer/pending future，
            # 必须提交到主 loop 执行，不能在新线程 asyncio.run（跨 loop 会挂起 drain）。
            loop = getattr(console, '_loop', None)
            if loop is None or not loop.is_running():
                return _json_err('MC 控制器事件循环未就绪', 500, 500)
            fut = asyncio.run_coroutine_threadsafe(console.run_instruction(cmd, timeout=20), loop)
            output = fut.result(timeout=25)
        except Exception as e:
            ctx.log(f"FS 指令执行异常: {e}", level='error')
            return _json_err(f'指令执行异常: {e}', 500, 500)
        return _json_ok({'op': op, 'output': output})

    rules = [
        ('/api/mc/fs', ['POST'], _api_fs),
    ]
    existing = {str(r.rule) for r in app.url_map.iter_rules()}
    added = 0
    for rule_path, methods, fn in rules:
        endpoint = f"mc_fs_{fn.__name__}"
        if rule_path in existing:
            # 已注册过：仅刷新 view 函数（重载后函数对象变化）
            app.view_functions[endpoint] = fn
            continue
        app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
        app.view_functions[endpoint] = fn
        added += 1
    ctx.log(f"文件管理 API 路由注册完成（新增 {added} 条）")
    return True


# ===================================================================
# 本地文件服务（上传/下载/编辑/新建/重命名/删除）
# MC 服务器目录即本机 minecraftconsole/mc-server —— 直接操作文件系统，
# 突破 agent FS 文本协议限制，支持任意二进制与多文件上传。走框架鉴权 + 防目录穿越。
# ===================================================================
def _mc_fs_root(ctx) -> str:
    """定位 MC 服务器根目录：配置 minecraftconsole.mc_root 优先，否则插件目录向上两级 /mc-server。"""
    try:
        cfg = ctx._framework.config or {}
        mc_cfg = cfg.get('minecraftconsole') or {}
        r = (mc_cfg.get('mc_root') or '').strip()
        if r:
            return os.path.abspath(r)
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, '..', '..', 'mc-server'))


def _safe_rel(root: str, rel: str):
    """把相对路径安全解析为 (rel, abs)；非法(穿越/绝对)返回 None。"""
    if rel is None:
        return None
    rel = str(rel).replace('\\', '/').strip().lstrip('/')
    if rel in ('', '.'):
        return '', root
    # 拒绝 .. 穿越
    parts = []
    for seg in rel.split('/'):
        if seg in ('', '.'):
            continue
        if seg == '..':
            return None
        parts.append(seg)
    clean = '/'.join(parts)
    return clean, os.path.normpath(os.path.join(root, clean))


def register_local_fs_routes(ctx) -> bool:
    """注册 /api/mc/lfs/* 本地文件操作端点（需登录；写操作需管理员）。"""
    try:
        app = ctx._framework.web_server.app
        if app is None:
            ctx.log("web_server.app 为空，跳过本地文件路由注册", level='warning')
            return False
    except Exception as e:
        ctx.log(f"获取 Flask app 失败: {e}", level='error')
        return False

    from flask import request, send_file
    from werkzeug.routing import Rule

    def _root():
        r = _mc_fs_root(ctx)
        if not os.path.isdir(r):
            raise RuntimeError(f'MC 服务器目录不存在: {r}')
        return r

    def _json_file_list(root, rel):
        """列目录 -> 结构化 JSON"""
        _, absp = _safe_rel(root, rel)
        if absp is None or not os.path.isdir(absp):
            return _json_err('不是目录: ' + str(rel), 400, 400)
        rows = []
        try:
            names = sorted(os.listdir(absp))
        except PermissionError:
            return _json_err('无权限访问该目录', 403, 403)
        for n in names:
            p = os.path.join(absp, n)
            try:
                st = os.stat(p)
                rows.append({
                    'name': n,
                    'is_dir': os.path.isdir(p),
                    'size': st.st_size if not os.path.isdir(p) else 0,
                    'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
                    'rel': (rel + '/' if rel and rel != '.' else '') + n,
                })
            except OSError:
                continue
        return _json_ok({'root': rel or '', 'rows': rows})

    def _api_list():
        if not _require_auth(ctx, request):
            return _json_err('未登录或令牌无效', 401, 401)
        try:
            root = _root()
            rel = request.args.get('path') or request.args.get('rel') or ''
            safe = _safe_rel(root, rel)
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, _ = safe
            return _json_file_list(root, rel)
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            ctx.log(f"lfs list 异常: {e}", level='error')
            return _json_err('列表失败', 500, 500)

    def _api_read():
        if not _require_auth(ctx, request):
            return _json_err('未登录或令牌无效', 401, 401)
        try:
            root = _root()
            rel = request.args.get('path') or ''
            safe = _safe_rel(root, rel)
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            if not os.path.isfile(absp):
                return _json_err('文件不存在: ' + rel, 404, 404)
            with open(absp, 'rb') as f:
                raw = f.read()
            binary = False
            encoding = 'utf-8'
            content = ''
            size = len(raw)
            if raw[:3] == b'\xef\xbb\xbf':  # UTF-8 BOM
                content = raw[3:].decode('utf-8', errors='replace')
                encoding = 'utf-8'
            elif raw and raw.count(b'\x00') / max(1, len(raw)) > 0.02:
                binary = True
            else:
                for enc in ('utf-8', 'gbk'):
                    try:
                        content = raw.decode(enc)
                        encoding = enc
                        break
                    except (UnicodeDecodeError, ValueError):
                        continue
                else:
                    content = raw.decode('utf-8', errors='replace')
            return _json_ok({'rel': rel, 'content': content,
                            'encoding': encoding, 'binary': binary, 'size': size})
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            ctx.log(f"lfs read 异常: {e}", level='error')
            return _json_err('读取失败', 500, 500)

    def _api_download():
        if not _require_auth(ctx, request):
            return _json_err('未登录或令牌无效', 401, 401)
        try:
            root = _root()
            rel = request.args.get('path') or ''
            safe = _safe_rel(root, rel)
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            if not os.path.isfile(absp):
                return _json_err('文件不存在: ' + rel, 404, 404)
            return send_file(absp, as_attachment=True, download_name=os.path.basename(absp), conditional=True, max_age=0)
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            ctx.log(f"lfs download 异常: {e}", level='error')
            return _json_err('下载失败', 500, 500)

    def _api_write():
        if not _require_admin(ctx, request):
            return _json_err('需要管理员权限', 403, 403)
        try:
            root = _root()
            d = request.get_json(force=True, silent=True) or {}
            safe = _safe_rel(root, d.get('path') or '')
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            content = d.get('content')
            if content is None:
                return _json_err('缺少 content', 400, 400)
            if os.path.isdir(absp):
                return _json_err('目标是目录', 400, 400)
            parent = os.path.dirname(absp)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                enc = str(d.get('encoding') or 'utf-8')
                with open(absp, 'w', encoding=enc, errors='replace', newline='') as f:
                    f.write(str(content))
            except OSError as e:
                return _json_err('保存失败: ' + str(e), 500, 500)
            return _json_ok(None, '已保存')
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            ctx.log(f"lfs write 异常: {e}", level='error')
            return _json_err('保存失败: ' + str(e), 500, 500)

    def _api_mkdir():
        if not _require_admin(ctx, request):
            return _json_err('需要管理员权限', 403, 403)
        try:
            root = _root()
            d = request.get_json(force=True, silent=True) or {}
            safe = _safe_rel(root, d.get('path') or '')
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            if os.path.exists(absp):
                return _json_err('已存在同名文件/目录', 1)
            os.makedirs(absp, exist_ok=True)
            return _json_ok(None, '已创建')
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            return _json_err('创建失败: ' + str(e), 500, 500)

    def _api_rename():
        if not _require_admin(ctx, request):
            return _json_err('需要管理员权限', 403, 403)
        try:
            root = _root()
            d = request.get_json(force=True, silent=True) or {}
            safe = _safe_rel(root, d.get('path') or '')
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            newname = str(d.get('newname') or '').strip()
            if not newname or '/' in newname or '\\' in newname or newname in ('.', '..'):
                return _json_err('非法新名称', 400, 400)
            if not os.path.exists(absp):
                return _json_err('文件不存在: ' + rel, 404, 404)
            os.rename(absp, os.path.join(os.path.dirname(absp), newname))
            return _json_ok(None, '已重命名')
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            return _json_err('重命名失败: ' + str(e), 500, 500)

    def _api_delete():
        if not _require_admin(ctx, request):
            return _json_err('需要管理员权限', 403, 403)
        try:
            import shutil as _sh
            root = _root()
            d = request.get_json(force=True, silent=True) or {}
            rel = str(d.get('path') or '')
            if rel in ('', '.', '/'):
                return _json_err('不能删除根目录', 400, 400)
            safe = _safe_rel(root, rel)
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            if not os.path.exists(absp):
                return _json_err('文件不存在: ' + rel, 404, 404)
            if os.path.isdir(absp):
                _sh.rmtree(absp)
            else:
                os.remove(absp)
            return _json_ok(None, '已删除')
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            return _json_err('删除失败: ' + str(e), 500, 500)

    def _api_upload():
        if not _require_admin(ctx, request):
            return _json_err('需要管理员权限', 403, 403)
        try:
            root = _root()
            rel = (request.form.get('dir') or '').replace('\\', '/').lstrip('/')
            safe = _safe_rel(root, rel)
            if safe is None:
                return _json_err('非法路径', 400, 400)
            rel, absp = safe
            files = request.files.getlist('files')
            if not files:
                return _json_err('未收到文件', 400, 400)
            n = 0
            for f in files:
                name = os.path.basename((f.filename or '').replace('\\', '/'))
                if not name:
                    continue
                f.save(os.path.join(absp, name))
                n += 1
            return _json_ok({'count': n}, f'已上传 {n} 个文件')
        except RuntimeError as e:
            return _json_err(str(e), 500, 500)
        except Exception as e:
            ctx.log(f"lfs upload 异常: {e}", level='error')
            return _json_err('上传失败: ' + str(e), 500, 500)

    rules = [
        ('/api/mc/lfs/list', ['GET'], _api_list),
        ('/api/mc/lfs/read', ['GET'], _api_read),
        ('/api/mc/lfs/download', ['GET'], _api_download),
        ('/api/mc/lfs/write', ['POST'], _api_write),
        ('/api/mc/lfs/mkdir', ['POST'], _api_mkdir),
        ('/api/mc/lfs/rename', ['POST'], _api_rename),
        ('/api/mc/lfs/delete', ['POST'], _api_delete),
        ('/api/mc/lfs/upload', ['POST'], _api_upload),
    ]
    existing = {str(r.rule) for r in app.url_map.iter_rules()}
    added = 0
    for rule_path, methods, fn in rules:
        endpoint = f"mc_lfs_{fn.__name__}"
        if rule_path in existing:
            app.view_functions[endpoint] = fn
            continue
        app.url_map.add(Rule(rule_path, endpoint=endpoint, methods=methods))
        app.view_functions[endpoint] = fn
        added += 1
    ctx.log(f"本地文件 API 路由注册完成（新增 {added} 条）")
    return True
