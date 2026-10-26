#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy「Buddy 加油站」每日自动签到

仅使用 Python 3 标准库，零第三方依赖。适用于：
  - GitHub Actions 定时运行（推荐）
  - 任意云函数 / 云服务器 crontab
  - 本地手动执行

设计原则：
  - 单账号、串行、每日一次，严格控制请求频率
  - 幂等：先查状态，已领取则不重复请求
  - 凭证只从环境变量或本地配置文件读取，绝不硬编码
  - 日志与邮件中的 token 一律脱敏
"""

import argparse
import base64
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

APP_NAME = "wb-checkin"
VERSION = "1.0.0"
USER_AGENT = "%s/%s" % (APP_NAME, VERSION)

DEFAULT_DOMAIN = "www.workbuddy.cn"
PATH_STATUS = "/v2/billing/meter/checkin-activity-status"
PATH_CHECKIN = "/v2/billing/meter/daily-checkin"
PATH_REFRESH = "/v2/plugin/auth/token/refresh"   # 注意有 /plugin 前缀（已实测：不带前缀返回 404）

# 接口返回码
CODE_OK = 0
CODE_ALREADY_CHECKED_IN = 10001  # “今天已签到，请明天再来” —— 幂等跳过，不是失败

# 策略常量
HTTP_TIMEOUT = 10          # 单次请求超时（秒）
MAX_RETRIES = 3            # 临时性失败最大重试次数
RETRY_BACKOFF = 2          # 退避基数：2s / 4s / 8s
GAP_BETWEEN_CALLS = 1.0    # 两次请求之间的间隔，控频用
ACCESS_REFRESH_MARGIN = 7 * 86400     # access token 剩余不足 7 天时，尝试自动续期（尽力而为）
EXPIRE_WARN_MARGIN = 21 * 86400       # 凭证剩余不足 21 天时，邮件提醒重新抓取

# 关于自动续期（重要，已实测）：
#   客户端真正使用的路径是 /v2/plugin/auth/token/refresh（网关前缀是 /plugin）。
#   之前误用 /v2/auth/token/refresh 才会 404。实测该路由：
#       - 不要求有效的 Authorization（用无效 access + 伪 refresh 探测，返回
#         401 {"code":12153,"msg":"refresh token failed"} 而非 404），
#       - 需要请求头 X-Refresh-Token 与 X-Auth-Refresh-Source: plugin。
#   因此：只要能拿到一个**有效的 refresh token**，就能长期自动续期。
#   请注意：客户端自身也会刷新，且每次刷新都会轮换 refresh token，
#   因此刷新成功后必须把新 token 写回状态文件（Actions 用 actions/cache 持久化），
#   否则缓存丢失时会回退到已失效的旧 token。

CN_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def mask(secret, head=6, tail=4):
    """token 脱敏：只保留前 head 后 tail。

    注意 tail 传 0 时不能写成 s[-0:]（Python 里 -0 == 0，会返回整个字符串），
    否则 Cookie 这类「只留前缀」的场景会把原文泄漏到日志里。
    """
    if not secret:
        return "<empty>"
    s = str(secret)
    if len(s) <= head + tail:
        return "*" * len(s)
    return s[:head] + "****" + (s[-tail:] if tail else "")


def now_cn():
    return datetime.now(CN_TZ)


def log(msg, level="INFO"):
    line = "%s [%s] %s" % (now_cn().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("utf-8", "replace").decode("utf-8", "replace"))
    sys.stdout.flush()
    extra = os.environ.get("WB_LOG_FILE")
    if extra:
        try:
            with open(extra, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def jwt_exp(token):
    """解析 JWT 的 exp（不验签）。返回 int 秒级时间戳，解析失败返回 None。"""
    try:
        parts = str(token).split(".")
        if len(parts) != 3:
            return None
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("utf-8")))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def read_json_file(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception as exc:
        log("配置文件读取失败 %s: %s" % (path, exc), "WARN")
        return {}


def write_json_file(path, obj):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log("状态文件写入失败 %s: %s" % (path, exc), "WARN")
        return False


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class Config(object):
    def __init__(self, cli):
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        cfg_path = cli.config or os.environ.get("WB_CONFIG_FILE") \
            or os.path.join(self.script_dir, "config.json")
        self.config_path = cfg_path
        file_cfg = read_json_file(cfg_path)

        env = os.environ
        self.domain = (env.get("WB_DOMAIN") or file_cfg.get("domain")
                       or DEFAULT_DOMAIN).strip().rstrip("/")
        if not self.domain.startswith("http"):
            self.domain = "https://" + self.domain

        self.access_token = (env.get("WB_ACCESS_TOKEN") or file_cfg.get("access_token") or "").strip()
        self.refresh_token = (env.get("WB_REFRESH_TOKEN") or file_cfg.get("refresh_token") or "").strip()
        self.uid = (env.get("WB_UID") or file_cfg.get("uid") or "").strip()

        # Cookie 备选凭证：Bearer token 不可用时的第二条路。
        # 网页端（www.workbuddy.cn 登录后的 XHR 请求）携带的通常就是 Cookie，
        # 客户端导出 Bearer 失败时可以用它顶上。
        self.cookie = (env.get("WB_COOKIE") or file_cfg.get("cookie") or "").strip()

        self.state_path = env.get("WB_STATE_FILE") or file_cfg.get("state_file") \
            or os.path.join(self.script_dir, "state", "token.json")

        # 邮件告警（全部可选，缺失则只打日志不发信）
        self.smtp_host = (env.get("SMTP_HOST") or file_cfg.get("smtp_host") or "").strip()
        self.smtp_port = int(env.get("SMTP_PORT") or file_cfg.get("smtp_port") or 465)
        self.smtp_user = (env.get("SMTP_USER") or file_cfg.get("smtp_user") or "").strip()
        self.smtp_pass = env.get("SMTP_PASS") or file_cfg.get("smtp_pass") or ""
        self.alert_to = (env.get("ALERT_TO") or file_cfg.get("alert_to") or "").strip()
        self.mail_from = (env.get("SMTP_FROM") or file_cfg.get("smtp_from")
                          or self.smtp_user or self.alert_to).strip()

    @property
    def mail_ready(self):
        return all([self.smtp_host, self.smtp_user, self.smtp_pass, self.alert_to])

    def auth_headers(self, token):
        """构造鉴权头。Bearer token 优先，缺失时回退到 Cookie。"""
        headers = {}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def describe_cred(self):
        """凭证概要（脱敏），用于日志。"""
        if self.cookie:
            return "Cookie(%s)" % mask(self.cookie, 12, 0)
        return "Bearer(%s)" % mask(self.access_token)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_post_json(url, headers, payload, retries=MAX_RETRIES):
    """
    POST JSON。返回 dict：
      {ok, http, data, error, retryable, attempts}
    仅在「临时性失败」上重试：网络异常 / 超时 / HTTP 429 / HTTP 5xx。
    """
    last = {"ok": False, "http": None, "data": {}, "error": "unknown",
            "retryable": False, "attempts": 0}
    for attempt in range(1, retries + 1):
        if attempt > 1:
            delay = RETRY_BACKOFF ** (attempt - 1)
            log("第 %d 次重试，等待 %ds 后请求 %s" % (attempt - 1, delay, url), "WARN")
            time.sleep(delay)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.getcode()
                try:
                    data = json.loads(raw) if raw else {}
                except ValueError:
                    data = {"_raw": raw[:500]}
                if 200 <= status < 300:
                    return {"ok": True, "http": status, "data": data,
                            "error": "", "retryable": False, "attempts": attempt}
                if status == 429 or 500 <= status < 600:
                    last = {"ok": False, "http": status, "data": data,
                            "error": "HTTP %d" % status, "retryable": True, "attempts": attempt}
                    continue
                return {"ok": False, "http": status, "data": data,
                        "error": "HTTP %d" % status, "retryable": False, "attempts": attempt}
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"_raw": raw[:500]}
            if exc.code == 429 or 500 <= exc.code < 600:
                last = {"ok": False, "http": exc.code, "data": data,
                        "error": "HTTP %d" % exc.code, "retryable": True, "attempts": attempt}
                continue
            return {"ok": False, "http": exc.code, "data": data,
                    "error": "HTTP %d" % exc.code, "retryable": False, "attempts": attempt}
        except Exception as exc:
            last = {"ok": False, "http": None, "data": {},
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "retryable": True, "attempts": attempt}
    return last


def api_url(cfg, path):
    return cfg.domain + path


# --------------------------------------------------------------------------
# 业务：刷新 / 查询 / 领取
# --------------------------------------------------------------------------
def extract_auth(obj):
    """鲁棒解析刷新接口返回：兼容 data.data / data / 顶层三种形态。"""
    if not isinstance(obj, dict):
        return None
    cands = []
    d = obj.get("data")
    if isinstance(d, dict):
        inner = d.get("data")
        if isinstance(inner, dict):
            cands.append(inner)
        cands.append(d)
    cands.append(obj)
    for c in cands:
        if not isinstance(c, dict):
            continue
        at = c.get("accessToken") or c.get("access_token")
        if at:
            norm = dict(c)
            norm["accessToken"] = at
            rt = c.get("refreshToken") or c.get("refresh_token")
            if rt:
                norm["refreshToken"] = rt
            return norm
    return None


def try_refresh(cfg, refresh_token, access_token):
    """刷新 token。成功返回新 auth dict，失败返回 None。"""
    if not refresh_token:
        log("缺少 refresh token，无法刷新", "WARN")
        return None
    # 刷新时 access token 往往已失效，不带 Authorization 以免网关先行拦截
    # （实测该路由不依赖 Authorization，仅认 X-Refresh-Token）
    headers = {
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
    }
    log("尝试刷新 access token（当前 %s）" % mask(access_token))
    resp = http_post_json(api_url(cfg, PATH_REFRESH), headers, {}, retries=2)
    if not resp["ok"]:
        log("刷新失败：%s %s" % (resp["error"], resp["data"].get("msg", "")), "WARN")
        return None
    auth = extract_auth(resp["data"])
    if not auth:
        log("刷新返回结构异常：%s" % json.dumps(resp["data"], ensure_ascii=False)[:300], "WARN")
        return None
    log("刷新成功，新 access token = %s" % mask(auth.get("accessToken")))
    return auth


def query_status(cfg, token):
    resp = http_post_json(api_url(cfg, PATH_STATUS), cfg.auth_headers(token), {})
    return resp


def do_checkin(cfg, token):
    resp = http_post_json(api_url(cfg, PATH_CHECKIN), cfg.auth_headers(token), {})
    return resp


def is_auth_error(resp):
    return (not resp["ok"]) and resp["http"] in (401, 403)


def is_temp_error(resp):
    return (not resp["ok"]) and resp["retryable"]


# --------------------------------------------------------------------------
# 邮件告警
# --------------------------------------------------------------------------
def send_alert(cfg, subject, body):
    if not cfg.mail_ready:
        log("邮件未配置完整（SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_TO），跳过发信：%s" % subject, "WARN")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("WB Checkin", cfg.mail_from))
    msg["To"] = cfg.alert_to
    msg["Date"] = formatdate(localtime=True)
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                      timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
            server.ehlo()
            try:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            except smtplib.SMTPException:
                pass  # 部分内网 SMTP 不支持 STARTTLS，直接明文继续
        with server:
            if cfg.smtp_user and cfg.smtp_pass:
                server.login(cfg.smtp_user, cfg.smtp_pass)
            server.sendmail(cfg.mail_from, [cfg.alert_to], msg.as_string())
        log("告警邮件已发送至 %s" % cfg.alert_to)
        return True
    except Exception as exc:
        log("告警邮件发送失败：%s: %s" % (type(exc).__name__, exc), "ERROR")
        return False


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="WorkBuddy「Buddy 加油站」每日自动签到",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python checkin.py --check-only      # 只查状态，绝不领取（首次验证用）\n"
               "  python checkin.py --dry-run         # 走完整流程但不发领取请求\n"
               "  python checkin.py                   # 正常执行\n")
    p.add_argument("--check-only", action="store_true", help="只查询签到状态，绝不发起领取请求")
    p.add_argument("--dry-run", action="store_true", help="走完整流程但不发起领取请求")
    p.add_argument("--notify-on-success", action="store_true", help="成功时也发邮件（默认静默）")
    p.add_argument("--config", help="配置文件路径（默认脚本同目录 config.json）")
    p.add_argument("--verbose", "-v", action="store_true", help="输出接口原始返回")
    p.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    return p.parse_args(argv)


def finish(result, detail, extra=None, cfg=None, notify=False, subject=None):
    """统一输出并退出。"""
    payload = {"ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "result": result, "detail": detail}
    if extra:
        payload.update(extra)
    print("RESULT_JSON " + json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    if cfg and notify and subject:
        body = "\n".join([
            "执行时间：%s" % payload["ts"],
            "执行结果：%s" % result,
            "",
        ] + ["%s：%s" % (k, v) for k, v in payload.items() if k not in ("ts", "result")])
        send_alert(cfg, subject, body)
    sys.exit(0 if result in ("success", "skipped") else 1)


def main(argv=None):
    args = parse_args(argv)
    cfg = Config(args)
    log("%s %s 启动（域名 %s，时区 UTC+8）" % (APP_NAME, VERSION, cfg.domain))

    # ---------- 1. 载入 token（缓存状态优先，回退环境变量/配置文件） ----------
    state = read_json_file(cfg.state_path)
    access = (state.get("accessToken") or "").strip() or cfg.access_token
    refresh = (state.get("refreshToken") or "").strip() or cfg.refresh_token
    cookie = (state.get("cookie") or "").strip() or cfg.cookie

    if not access and not cookie:
        log("未找到任何凭证。请设置环境变量 WB_ACCESS_TOKEN 或 WB_COOKIE", "ERROR")
        finish("error", "登录凭证缺失，未执行任何请求", cfg=cfg, notify=True,
               subject="[WB签到] 失败：缺少登录凭证",
               extra={"hint": "用浏览器 F12 复制 Authorization 或 Cookie，更新仓库 Secret"})

    if access:
        log("使用 access token %s（来源：%s）" %
            (mask(access), "缓存状态" if state.get("accessToken") else "环境变量/配置文件"))
    else:
        log("使用 Cookie %s（来源：%s）" %
            (mask(cookie, 12, 0), "缓存状态" if state.get("cookie") else "环境变量/配置文件"))

    warnings = []
    refreshed_in_this_run = False

    # ---------- 2. token 有效期检查（含提前告警与尽力而为的续期） ----------
    now_ts = int(time.time())
    acc_exp = jwt_exp(access) if access else None
    if acc_exp is not None:
        left = acc_exp - now_ts
        exp_str = datetime.fromtimestamp(acc_exp, CN_TZ).strftime("%Y-%m-%d %H:%M")
        log("access token 剩余有效期：%d 天（到期 %s）" % (max(left // 86400, 0), exp_str))
        if left <= 0:
            detail = "access token 已于 %s 过期，请重新抓取凭证" % exp_str
            log(detail, "ERROR")
            finish("error", detail, cfg=cfg, notify=True,
                   subject="[WB签到] 失败：登录凭证已过期",
                   extra={"credential": cfg.describe_cred(), "expire_at": exp_str,
                          "hint": "用浏览器 F12 重新抓取凭证，更新 WB_ACCESS_TOKEN 或 WB_COOKIE Secret"})
        if left < EXPIRE_WARN_MARGIN:
            warnings.append("access token 将在 %d 天后过期（%s），请尽快重新抓取并更新凭证"
                            % (max(left // 86400, 0), exp_str))
        if left < ACCESS_REFRESH_MARGIN:
            # 尽力而为：服务端若哪天开放了刷新路由即可自动续期，失败不影响本次执行
            new_auth = try_refresh(cfg, refresh, access)
            refreshed_in_this_run = True
            if new_auth:
                access = new_auth.get("accessToken") or access
                refresh = new_auth.get("refreshToken") or refresh
                write_json_file(cfg.state_path, {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "domain": cfg.domain,
                    "updatedAt": now_cn().isoformat(),
                })
                warnings = []  # 续期成功，撤销过期告警
            else:
                warnings.append("尝试自动续期失败（服务端未提供可用的刷新接口），请手动更新凭证")
    elif access:
        warnings.append("无法解析 access token 有效期（token 格式可能已变化，过期时脚本不会再提前预警）")
    else:
        # Cookie 凭证无法本地预判过期时间，只能等服务端返回 401/403 才能感知
        warnings.append("当前使用 Cookie 凭证，无法预判有效期；一旦服务端拒绝请重新抓取 Cookie")

    ref_exp = jwt_exp(refresh)
    if ref_exp:
        rleft = ref_exp - now_ts
        if rleft < EXPIRE_WARN_MARGIN:
            warnings.append("refresh token 将在 %d 天后过期（%s），届时同样需要重新抓取凭证"
                            % (max(rleft // 86400, 0),
                               datetime.fromtimestamp(ref_exp, CN_TZ).strftime("%Y-%m-%d")))

    # ---------- 3. 查询签到状态 ----------
    resp = query_status(cfg, access)
    if is_auth_error(resp):
        log("状态查询返回登录态失效（HTTP %s），尝试刷新后重试一次" % resp["http"], "WARN")
        if not refreshed_in_this_run:
            new_auth = try_refresh(cfg, refresh, access)
            refreshed_in_this_run = True
            if new_auth:
                access = new_auth.get("accessToken") or access
                refresh = new_auth.get("refreshToken") or refresh
                write_json_file(cfg.state_path, {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "domain": cfg.domain,
                    "updatedAt": now_cn().isoformat(),
                })
                resp = query_status(cfg, access)
    if not resp["ok"]:
        msg = resp["error"] or "未知错误"
        api_msg = resp["data"].get("msg") if isinstance(resp["data"], dict) else ""
        detail = "状态查询失败：%s %s" % (msg, api_msg or "")
        log(detail, "ERROR")
        finish("error", detail, cfg=cfg, notify=True,
               subject="[WB签到] 失败：状态查询异常",
               extra={"http": resp["http"], "attempts": resp["attempts"],
                      "credential": cfg.describe_cred(), "warnings": warnings})

    if args.verbose:
        log("status 原始返回：%s" % json.dumps(resp["data"], ensure_ascii=False), "DEBUG")

    data = resp["data"].get("data") or {}
    code = resp["data"].get("code")
    if code not in (None, CODE_OK):
        detail = "状态查询返回异常 code=%s msg=%s" % (code, resp["data"].get("msg"))
        log(detail, "ERROR")
        finish("error", detail, cfg=cfg, notify=True,
               subject="[WB签到] 失败：接口返回异常",
               extra={"code": code, "token": mask(access), "warnings": warnings})

    active = bool(data.get("active", True))
    today_checked_in = bool(data.get("today_checked_in"))
    streak_days = data.get("streak_days")
    daily_credit = data.get("daily_credit")
    credits_before = data.get("total_credits")
    season = data.get("season")
    activity = data.get("activity_name")
    checkin_dates = data.get("checkin_dates") or []

    log("活动：第 %s 期「%s」| 有效期 %s ~ %s" %
        (season, activity, data.get("start_time"), data.get("end_time")))
    log("今日是否已领取：%s | 连续天数：%s | 本期累计：%s 积分 | 本期已领 %d 天" %
        ("是" if today_checked_in else "否", streak_days, credits_before, len(checkin_dates)))
    if not active:
        log("签到活动当前未开启（active=false）", "WARN")

    # ---------- 4. 领取 ----------
    action = "none"
    credits_after = credits_before
    result = "skipped"
    detail = ""

    if today_checked_in:
        action = "skip_already_signed"
        result = "skipped"
        detail = "今天已领取，跳过（幂等，不重复领取）"
        log(detail)
    elif args.check_only:
        action = "check_only"
        result = "skipped"
        detail = "--check-only 模式，未发起领取请求"
        log(detail)
    elif args.dry_run:
        action = "dry_run"
        result = "skipped"
        detail = "--dry-run 模式，未发起领取请求"
        log(detail)
    else:
        time.sleep(GAP_BETWEEN_CALLS)  # 控频：两次请求之间至少间隔 1 秒
        log("今日未领取，发起签到请求 …")
        cr = do_checkin(cfg, access)
        if is_auth_error(cr):
            log("领取返回登录态失效（HTTP %s），尝试刷新后重试一次" % cr["http"], "WARN")
            new_auth = try_refresh(cfg, refresh, access)
            refreshed_in_this_run = True
            if new_auth:
                access = new_auth.get("accessToken") or access
                refresh = new_auth.get("refreshToken") or refresh
                write_json_file(cfg.state_path, {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "domain": cfg.domain,
                    "updatedAt": now_cn().isoformat(),
                })
                time.sleep(GAP_BETWEEN_CALLS)
                cr = do_checkin(cfg, access)

        if args.verbose:
            log("checkin 原始返回：%s" % json.dumps(cr["data"], ensure_ascii=False), "DEBUG")

        ccode = cr["data"].get("code") if isinstance(cr["data"], dict) else None
        cmsg = cr["data"].get("msg") if isinstance(cr["data"], dict) else ""

        if cr["ok"] and ccode == CODE_OK:
            action = "claimed"
            result = "success"
            detail = "签到成功：%s" % (cmsg or "OK")
            log(detail)
            # 再查一次状态，获取领取后的累计积分
            time.sleep(GAP_BETWEEN_CALLS)
            after = query_status(cfg, access)
            if after["ok"]:
                adata = after["data"].get("data") or {}
                credits_after = adata.get("total_credits", credits_before)
                streak_days = adata.get("streak_days", streak_days)
        elif cr["ok"] and ccode == CODE_ALREADY_CHECKED_IN:
            action = "skip_already_signed"
            result = "skipped"
            detail = "接口返回已签到：%s" % cmsg
            log(detail)
        elif not cr["ok"] and cr["http"] in (401, 403):
            action = "claim_failed"
            result = "error"
            detail = "登录态失效，签到失败：%s %s" % (cr["error"], cmsg)
            log(detail, "ERROR")
        elif is_temp_error(cr):
            action = "claim_failed"
            result = "error"
            detail = "签到请求连续失败（已重试 %d 次）：%s" % (cr["attempts"], cr["error"])
            log(detail, "ERROR")
        else:
            action = "claim_failed"
            result = "error"
            detail = "签到返回异常：%s code=%s msg=%s" % (cr["error"] or "HTTP200", ccode, cmsg)
            log(detail, "ERROR")

    # ---------- 5. 输出与告警 ----------
    delta = None
    if isinstance(credits_before, (int, float)) and isinstance(credits_after, (int, float)):
        delta = credits_after - credits_before

    human = "%s 结果=%s 动作=%s 积分 %s → %s（%s）连续 %s 天" % (
        now_cn().strftime("%Y-%m-%d %H:%M:%S"), result, action,
        credits_before, credits_after,
        ("+%s" % delta) if delta else "无变化", streak_days)
    print(human)
    sys.stdout.flush()

    for w in warnings:
        log("提醒：%s" % w, "WARN")

    extra = {
        "action": action,
        "credits_before": credits_before,
        "credits_after": credits_after,
        "credits_delta": delta,
        "streak_days": streak_days,
        "daily_credit": daily_credit,
        "season": season,
        "activity": activity,
        "credential": cfg.describe_cred(),
        "warnings": warnings,
    }

    need_notify = (result == "error") or bool(warnings) or args.notify_on_success
    if need_notify:
        if result == "error":
            subject = "[WB签到] 失败：%s" % action
        elif warnings:
            subject = "[WB签到] 提醒：凭证需要关注"
        else:
            subject = "[WB签到] 成功"
        finish(result, detail, extra, cfg=cfg, notify=True, subject=subject)

    finish(result, detail, extra)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文兼容
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        log("已中断", "WARN")
        sys.exit(130)
    except Exception as exc:  # 兜底：任何未捕获异常也要留痕
        log("未捕获异常：%s: %s" % (type(exc).__name__, exc), "ERROR")
        sys.exit(1)
