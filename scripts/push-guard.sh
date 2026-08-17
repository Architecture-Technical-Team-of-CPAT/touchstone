#!/usr/bin/env bash
# ============================================================================
# push-guard.sh —— git pre-push 钩子：拦「推送到已关闭/已合并 PR 的分支」
# ----------------------------------------------------------------------------
# 动机（2026-08-17 实付学费）：PR 合并后其分支的推送 GitHub 静默接受——push 报
# 成功、提交却不评审、不进 main，纯作废。写进 skill §7 的坑被 skill 作者本人
# 在规则合入 5 分钟内踩中，证明纪律靠自觉不可靠，要靠机器闸。
#
# 拦截逻辑：对每个待推送分支——
#   1. 远端同名分支有【开着的】 PR（state=open；draft PR 同样放行——草稿态也要推提交）→ 放行
#   2. 远端同名分支的 PR 已 merged/closed，且本次推送含新提交 → 拒绝并提示
#      「开新分支 cherry-pick」
#   3. 远端无 PR（未开过 PR 的分支，如个人开发分支）→ 放行
#
# 安装（一次性）：
#   cp scripts/push-guard.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
#   （或 git config core.hooksPath .githooks 后放入该目录——随仓版本化时用）
#
# 依赖：curl；GITHUB_TOKEN（PAT）或 GH_TOKEN。无 token 时降级为提示（不拦截），
#       避免离线/未配环境被卡。--no-verify 可绕过（明知故犯时）。
# ============================================================================
set -euo pipefail

# 退出时统一善后：排空 stdin（防 git 在钩子提前退出时报 SIGPIPE/读错误——覆盖
# set -e 意外死、所有 exit 路径）+ 清理含凭据的临时头文件。
HDR_FILE=""
drain() { cat > /dev/null 2>/dev/null || true; }
cleanup() { drain; [ -n "$HDR_FILE" ] && rm -f "$HDR_FILE" 2>/dev/null || true; }
trap cleanup EXIT

GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
# 兜底：本机惯例 ~/github.token（仅本地开发便利；CI/他人环境用 env）
# 写成 if 而非 && 链：&& 链首失败在 set -e 下虽被豁免（bash 规则：非链尾失败不
# 触发 -e），但整链返回码非 0——若哪天这条挪到脚本末行,会被 git 当钩子失败。
if [ -z "$GITHUB_TOKEN" ] && [ -f "$HOME/github.token" ]; then
  GITHUB_TOKEN="$(tr -d '\n' < "$HOME/github.token")"
fi

# 含凭据头文件（token 不进 argv）：建一次循环复用,EXIT trap 统一清理。无 token 时
# 空文件——循环内无 token 分支先行 continue,不会用到。
HDR_FILE="$(mktemp)"
printf 'Authorization: Bearer %s\n' "$GITHUB_TOKEN" > "$HDR_FILE"

# pre-push 钩子从 stdin 读：每行 "<local ref> <local sha> <remote ref> <remote sha>"
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue  # 删分支，不拦
  case "$remote_ref" in refs/heads/*) ;; *) continue ;; esac                 # 只管分支
  branch="${remote_ref#refs/heads/}"

# 从仓库 remote 推断 owner/repo（取 origin 的 URL）
  url="$(git remote get-url origin 2>/dev/null || true)"
  slug=""
  if echo "$url" | grep -qE 'github\.com[:/].+/.+'; then
    slug="$(echo "$url" | sed -E 's#.*github\.com[:/]##; s#\.git$##; s#^x-access-token:[^@]*@##; s#https?://##')"
  fi
  [ -n "$slug" ] || { echo "[push-guard] 非 GitHub remote 或无法解析仓坐标，跳过检查。"; continue; }

  if [ -z "$GITHUB_TOKEN" ]; then
    echo "[push-guard] ⚠️ 未设 GITHUB_TOKEN/GH_TOKEN，无法查 PR state（降级提示，不拦截）。"
    continue
  fi

  # 查该分支的 PR（含关闭的）。列表端点无 merged 字段：先列表取 state+number，
  # closed 时再查单 PR 端点取 merged（区分「已合并」与「关闭未合并」）。
  # 网络/服务失败 → 标记 "net-fail"，区别于「该分支无 PR」：设计选择是不拦截
  # （离线/CI 故障不该卡住开发者推送），但显式提示降级,不静默。
  # token 不进 argv（ps 可见）：经含凭据的头文件 -H @file 传（循环外建一次复用，
  # EXIT trap 统一清理）。分支名全量 URL 编码：? # % 中文等都会破 query。
  # 一次请求同时拿 body 与 HTTP 状态（-w 附加状态码行）——不二次请求（省限流，
  # 无 TOCTOU）；非 2xx（401 坏token/403 限流/404 错slug）与「无PR」区分开。
  enc_branch="$(printf '%s' "$branch" | python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')"
  list_url="https://api.github.com/repos/$slug/pulls?head=${slug%%/*}:$enc_branch&state=all"
  raw="$(curl -sS -H @"$HDR_FILE" -w '\n__HTTP__%{http_code}' \
        "$list_url" 2>/dev/null || true)"
  http_code="${raw##*__HTTP__}"
  body="${raw%$'\n'__HTTP__*}"
  if [ -z "$http_code" ] || [ "$http_code" = "000" ]; then state="net-fail"
  elif [ "$http_code" != "200" ]; then state="api-error"
  else
    # 输出收敛为单一结果行；.get() 防御畸形条目；异常时不回退 echo 追加（避免
    # 「部分输出+parse-fail」拼接污染 grep 判定）——统一落 else 分支提示。
    state="$(printf '%s' "$body" | python3 -c '
import sys, json
def out(tag, extra=""):
    print((tag + " " + extra).strip()); sys.exit()
try:
    ps = json.load(sys.stdin)
except Exception:
    out("parse-fail")
if not isinstance(ps, list):
    out("api-error")
if not ps:
    out("none")
# 任一 open 优先（同 head 分支存在 open PR 即放行）；否则取 number 最大（最新）
# 的那条走 merged/closed 判定——比固定 ps[0] 稳：旧记录排序不依赖 API 返回序。
opens = [p for p in ps if p.get("state") == "open"]
if opens:
    out("open")
p = max(ps, key=lambda x: x.get("number") or 0)
st = p.get("state")
if st != "closed":
    out(st or "unknown")
out("closed", str(p.get("number") or 0))' || echo parse-fail)"
  fi

  if printf '%s' "$state" | grep -q '^open'; then
    echo "[push-guard] ✓ $branch 的 PR 仍 open，放行。"
    continue
  elif printf '%s' "$state" | grep -q '^closed [0-9]'; then
    pr_num="$(printf '%s' "$state" | awk '{print $2}')"
    one="$(curl -sS -H @"$HDR_FILE" \
         "https://api.github.com/repos/$slug/pulls/$pr_num" 2>/dev/null || echo 'ONE_FAIL')"
    merged="$(printf '%s' "$one" | python3 -c '
import sys, json
try: print("merged" if json.load(sys.stdin).get("merged") else "not-merged")
except Exception: print("unknown")' 2>/dev/null || echo unknown)"
    if [ "$merged" = "unknown" ]; then
      echo "[push-guard] ⚠️ PR #$pr_num 状态查询失败——不基于空响应硬拦，降级放行。请自行确认 PR 状态。" >&2
      continue
    fi
    if [ "$merged" = "merged" ]; then
      echo "[push-guard] ❌ $branch 的 PR #$pr_num 已【合并】，推送不会进任何 PR/main——纯作废！" >&2
      echo "            正确做法：从最新 main 切新分支 cherry-pick，重开 PR。" >&2
      echo "            （明知故犯绕过：git push --no-verify）" >&2
      exit 1
    fi
    echo "[push-guard] ❌ $branch 的 PR #$pr_num 已【关闭未合并】，推送不评审不进 main——作废！" >&2
    echo "            若要继续此工作：重开 PR，或从最新 main 切新分支。" >&2
    echo "            （明知故犯绕过：git push --no-verify）" >&2
    exit 1
  elif [ "$state" = "api-error" ]; then
    echo "[push-guard] ⚠️ GitHub API 返回错误（HTTP $http_code：token 失效/限流/slug 错？）——无法查 $branch 的 PR state，降级放行。请检查凭据。" >&2
    continue
  elif [ "$state" = "net-fail" ]; then
    echo "[push-guard] ⚠️ GitHub API 不可达（网络/限流）——无法查 $branch 的 PR state，降级放行。请自行确认 PR 状态。" >&2
    continue
  elif [ "$state" = "none" ]; then
    echo "[push-guard] ✓ $branch 从未开过 PR（个人分支），放行。"
    continue
  else
    echo "[push-guard] ⚠️ PR state 查询异常（$state），不拦截仅提示——请人工确认 $branch 的 PR 状态。"
    continue
  fi
done

exit 0
