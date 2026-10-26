"""端到端冒烟脚本（M7）：一条命令验证部署是否真的可用。

覆盖链路：登录 -> 建会话 -> prompt（触发工具）-> 用量统计 -> 审计链校验 -> 自动化 CRUD。

只用标准库（urllib），不引入额外依赖，方便在服务器/容器里直接跑：

    python scripts/smoke.py --base http://127.0.0.1:8765
    python scripts/smoke.py --base http://127.0.0.1:8080 \
        --username admin --password 'your-password'

退出码：0 全通；1 有失败项（便于 CI / 上线前卡点）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: str | None = None

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"detail": raw[:300]}

    def get(self, path: str):
        return self._req("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self._req("POST", path, body or {})


class Report:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        line = f"  [{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke")
    ap.add_argument("--base", default="http://127.0.0.1:8765", help="服务地址")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--prompt", default="跑个命令", help="触发一轮 agent 的 prompt")
    ap.add_argument("--skip-automation", action="store_true", help="跳过自动化创建（部分环境禁写）")
    args = ap.parse_args()

    api = Api(args.base)
    rep = Report()
    print(f"== Code-agent 冒烟 @ {args.base} ==\n")

    # 0) 健康检查
    print("[0] 健康检查")
    status, body = api.get("/healthz")
    rep.check("GET /healthz", status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} version={(body or {}).get('version') if isinstance(body, dict) else '?'}")

    # 1) 登录
    print("\n[1] 登录")
    status, body = api.post("/api/v1/auth/login", {"username": args.username, "password": args.password})
    ok = status == 200 and isinstance(body, dict) and bool(body.get("token"))
    rep.check("POST /auth/login", ok, f"status={status}")
    if not ok:
        print("\n登录失败，后续用例无法继续。请检查用户名/密码与服务地址。")
        return 1
    api.token = body["token"]  # type: ignore[index]
    rep.check("返回角色信息", bool(body.get("user", {}).get("role")))

    # 2) 会话 + prompt
    print("\n[2] 会话与对话")
    status, meta = api.post("/api/v1/sessions", {"model_tier": "default"})
    ok = status == 200 and isinstance(meta, dict) and bool(meta.get("id"))
    rep.check("POST /sessions", ok, f"status={status}")
    if not ok:
        return 1
    sid = meta["id"]  # type: ignore[index]

    status, result = api.post(f"/api/v1/sessions/{sid}/prompt", {"text": args.prompt})
    ok = status == 200 and isinstance(result, dict) and "text" in result
    rep.check("POST /sessions/{id}/prompt", ok, f"status={status}")
    if ok:
        text = (result.get("text") or "").replace("\n", " ")[:60]
        print(f"        agent: {text}")
        rep.check("返回用量", isinstance(result.get("usage"), dict),
                  str(result.get("usage")))

    # 3) 事件轨迹
    status, events = api.get(f"/api/v1/sessions/{sid}/events")
    ok = status == 200 and isinstance(events, list) and len(events) > 0
    types = sorted({e.get("type") for e in events}) if ok else []
    rep.check("GET /sessions/{id}/events", ok, f"{len(events) if ok else 0} 条, 类型={types}")

    # 4) 用量统计
    print("\n[3] 用量统计")
    status, usage = api.get("/api/v1/usage/me")
    ok = status == 200 and isinstance(usage, dict) and usage.get("prompts", 0) >= 1
    rep.check("GET /usage/me", ok,
              f"prompts={usage.get('prompts') if isinstance(usage, dict) else '?'} "
              f"in={usage.get('input_tokens') if isinstance(usage, dict) else '?'} "
              f"out={usage.get('output_tokens') if isinstance(usage, dict) else '?'}")

    # 5) 审计链
    print("\n[4] 审计 hash chain")
    status, entries = api.get("/api/v1/admin/audit")
    ok = status == 200 and isinstance(entries, list)
    rep.check("GET /admin/audit", ok, f"{len(entries) if ok else 0} 条")
    status, verify = api.get("/api/v1/admin/audit/verify")
    ok = status == 200 and isinstance(verify, dict) and verify.get("ok") is True
    rep.check("GET /admin/audit/verify", ok, str(verify))

    # 6) 自动化
    if not args.skip_automation:
        print("\n[5] 自动化任务")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        status, auto = api.post("/api/v1/automations", {
            "name": "冒烟一次性任务",
            "prompt": "生成今日简报",
            "schedule_type": "once",
            "scheduled_at": past,
        })
        ok = status == 200 and isinstance(auto, dict) and bool(auto.get("id"))
        rep.check("POST /automations (once)", ok, f"status={status}")
        if ok:
            rep.check("next_run_at 已落库", bool(auto.get("next_run_at")), str(auto.get("next_run_at")))

        status, _ = api.post("/api/v1/automations", {
            "name": "坏规则", "prompt": "x", "schedule_type": "recurring", "rrule": "FREQ=NOPE",
        })
        rep.check("非法 rrule 被拒(422)", status == 422, f"status={status}")

        status, lst = api.get("/api/v1/automations")
        rep.check("GET /automations", status == 200 and isinstance(lst, list),
                  f"{len(lst) if isinstance(lst, list) else 0} 个")

    # 汇总
    print(f"\n== 结果：{rep.passed} passed, {rep.failed} failed ==")
    if rep.failed == 0:
        print("全部通过：部署可用。")
        return 0
    print("存在失败项：请检查上方 FAIL 行与服务日志。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
