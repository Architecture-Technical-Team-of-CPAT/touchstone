#!/usr/bin/env python3
# ============================================================================
# touchstone/ghclient.py  ——  GitHub HTTP 客户端（统一入口，消除 4 处重复 wrapper）
# ----------------------------------------------------------------------------
# requests + urllib3.Retry：连接池、指数退避、Retry-After、5xx/429 重试均由库处理。
# 【保持串行】——GitHub 二级限流惩罚并发，不做并发(这条与库无关，是 GitHub 策略)。
# 二级限流 403 带 Retry-After 时额外尊重一次；权限类 403(无 Retry-After) 由
# raise_for_status 立即抛出，不空转。
#
# 本模块是所有 GitHub REST/GraphQL 调用的唯一入口——此前 orchestrator/calibrate/
# checks/learning_loop 各自写了一个 `gh()` wrapper（拼 base_url + 传 token），
# 现在统一为 client() 工厂 + get/post/paginate/paginate_check_runs 方法。
# ============================================================================

import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GH_RETRY_MAX = int((os.environ.get("GH_RETRY_MAX") or "").strip() or "5")


def _base_url():
    return os.environ.get("GITHUB_API_URL", "https://api.github.com")


def make_session():
    retry = Retry(
        total=GH_RETRY_MAX, connect=GH_RETRY_MAX, read=GH_RETRY_MAX,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
        allowed_methods=frozenset(["GET", "POST"]),
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        _SESSION = make_session()
    return _SESSION


# ---- 统一客户端（替代各模块的 gh()/_gh_get()/_gh() wrapper）-------------------

def client(token):
    """返回一个绑定 token 的 GitHub 客户端，提供 get/post/paginate/paginate_check_runs。
    替代此前 4 个模块各自写的 `gh()` wrapper（拼 base_url + 传 token 的重复代码）。"""
    base = _base_url()

    def _req(method, path, data=None, accept="application/vnd.github+json", timeout=60):
        url = base + path if path.startswith("/") else path
        sess = _session()
        headers = {"Authorization": "Bearer " + token, "Accept": accept,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "touchstone"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        r = None
        for attempt in range(2):
            r = sess.request(method, url, headers=headers,
                             json=data if data is not None else None, timeout=timeout)
            if r.status_code == 403 and r.headers.get("Retry-After") and attempt == 0:
                time.sleep(float(r.headers["Retry-After"]))
                continue
            break
        r.raise_for_status()
        if accept.endswith("diff"):
            return r.text
        return r.json() if r.text else {}

    def get(path, accept="application/vnd.github+json"):
        return _req("GET", path, accept=accept)

    def post(path, data):
        return _req("POST", path, data=data)

    def patch(path, data):
        # 对称于 get/post：编辑既有资源（如 PATCH /repos/.../issues/{n} 改 body）。
        # make_session 的 allowed_methods 只含 GET/POST——PATCH 非幂等、不自动重试（issue
        # 编辑语义本就不该重放），但 sess.request 仍正常发送 PATCH（allowed_methods 仅约束
        # 【重试】，不约束可发 method）；403+Retry-After 的二级限流兜底在 _req 内仍生效。
        return _req("PATCH", path, data=data)

    def paginate(path, per_page=100, max_pages=20):
        sep = "&" if "?" in path else "?"
        out = []
        for page in range(1, max_pages + 1):
            data = _req("GET", f"{path}{sep}page={page}&per_page={per_page}")
            if not isinstance(data, list):
                break
            out.extend(data)
            if len(data) < per_page:
                break
            sep = "&"
        return out

    def paginate_check_runs(path, per_page=100, max_pages=20):
        sep = "&" if "?" in path else "?"
        all_runs = []
        for page in range(1, max_pages + 1):
            data = _req("GET", f"{path}{sep}page={page}&per_page={per_page}")
            runs = (data or {}).get("check_runs") or []
            all_runs.extend(runs)
            if len(runs) < per_page:
                break
            sep = "&"
        return {"check_runs": all_runs, "total_count": len(all_runs)}

    return type("GHClient", (), {
        "get": staticmethod(get), "post": staticmethod(post), "patch": staticmethod(patch),
        "paginate": staticmethod(paginate), "paginate_check_runs": staticmethod(paginate_check_runs),
        "_req": staticmethod(_req),
        "base_url": base, "token": token,
    })()


# ---- 旧接口（向后兼容，逐步迁移到 client()）---------------------------------

def request(method, url, token, data=None, accept="application/vnd.github+json",
            session=None, timeout=60):
    """串行请求 GitHub REST/GraphQL。accept 以 'diff' 结尾返回文本，否则返回 JSON。"""
    sess = session or _session()
    headers = {"Authorization": "Bearer " + token, "Accept": accept,
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "touchstone"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = None
    for attempt in range(2):
        r = sess.request(method, url, headers=headers,
                         json=data if data is not None else None, timeout=timeout)
        if r.status_code == 403 and r.headers.get("Retry-After") and attempt == 0:
            time.sleep(float(r.headers["Retry-After"]))
            continue
        break
    r.raise_for_status()
    if accept.endswith("diff"):
        return r.text
    return r.json() if r.text else {}


def paginate(url, token, *, per_page=100, max_pages=20, accept="application/vnd.github+json"):
    """GitHub 列表翻页（旧接口，逐步迁移到 client(token).paginate）。"""
    sep = "&" if "?" in url else "?"
    out = []
    for page in range(1, max_pages + 1):
        data = request("GET", f"{url}{sep}page={page}&per_page={per_page}", token, accept=accept)
        if not isinstance(data, list):
            break
        out.extend(data)
        if len(data) < per_page:
            break
        sep = "&"
    return out


def paginate_check_runs(url, token, *, per_page=100, max_pages=20):
    """check-runs 专用翻页（旧接口，逐步迁移到 client(token).paginate_check_runs）。"""
    sep = "&" if "?" in url else "?"
    all_runs = []
    for page in range(1, max_pages + 1):
        data = request("GET", f"{url}{sep}page={page}&per_page={per_page}", token)
        runs = (data or {}).get("check_runs") or []
        all_runs.extend(runs)
        if len(runs) < per_page:
            break
        sep = "&"
    return {"check_runs": all_runs, "total_count": len(all_runs)}
