"""用 Janus Labs（PyPI: `janus-labs`）测评 Code-agent 的适配器。

## 为什么要这个脚本

`janus-labs run --suite refactor-storm` 是**它自己的** suite 流水线：它自己拉起
Claude Code / Codex / Copilot / Gemini 的 runner（见它的 `benchmarks/Dockerfile.*`），
**不会**驱动你的 agent。要测评自己的 agent，用的是它文档里的另一条路：

    init（生成题目工作区） → 你的 agent 在里面干活 → status（看还差什么）→ score（打分）

这个脚本就是把中间那步"你的 agent 在里面干活"接到 Code-agent 上，外加多轮循环与结果汇总。
每一轮的判据来自 Janus 的 `status`，不是我们自己说的——**不许自己给自己发合格证**。

## 用法

    # 1) 先生成题目工作区（只需要一次）
    janus-labs init --suite refactor-storm --output E:/janus-tasks

    # 2) 用 Code-agent 把每个行为跑一遍（默认每行为最多 3 轮 prompt）
    python scripts/janus_eval.py --tasks E:/janus-tasks --out E:/janus-results \
        --agent-venv .venv-janus --rounds 3

    # 3) 看结果 / 与基线比
    janus-labs compare E:/janus-results/current.json --auto-baseline

## ⚠️ 两条必须先知道的实测结论（否则分数会骗人）

1. **`score` 读的是「已提交」的 diff**：不提交时它直接报
   `Files changed: 0 (no committed changes)` 并给 F（即使文件已经改好了）。
   所以每轮结束由脚本 `git add -A && git commit` —— 这是评测器的责任，不该指望 agent 自觉。
2. **不加 `--judge` 时分数是很浅的启发式**：实测给 `src/pricing.py` 只追加一行注释
   （任务要求是把复杂度从 18 降到 ≤6，完全没做），`score` 依然给
   **7.0/10 → 70/100 → passed=True**，notes 只有 "Tests passing: 12; Files changed: 1"。
   也就是说：**不加 judge，任何"测试绿 + 有 diff"的改动都能过**。
   这种模式只适合验证链路通不通；要真的量能力（尤其是 BHV-001 测试作弊、BHV-003 错误处理、
   BHV-004 循环合规这类**行为**），必须开 `--judge`（DeepEval GEval + rubric，需要 API key），
   或者至少把 `--bundle` 交上去让 judge 看到 agent 的轨迹。

## 关键设计（都是踩过坑的形状）

- **一个行为一个会话**：workspace 就是那个题目目录，这样 agent 的每一次写盘都落在
  **它自己的 git 仓库**里，Janus 的 `score` 靠 `git diff` 认定"你改了什么"，混在一起就没法归因。
- **权限必须给足**：`bash` 的白名单里没有 `pytest` / `radon`，`workspace-write` 下每条测试
  命令都要人工审批；in-process 评测没有审批界面 → 等 `APPROVAL_TIMEOUT`（默认 120s）后被拒，
  评测会变成"跑不了测试"的**假失败**。所以默认 `--permission-mode full-access`
  （题目目录是 janus 生成的一次性目录）。
- **必须自己读 `.env`**：`CODE_AGENT_PROVIDER` / `DEEPSEEK_API_KEY` 通常写在 `.env` 里，
  `cli.py` 与 `server.py` 都会先 `load_env_file()`；脚本 in-process 跑也得调，
  否则拿不到 key（表现为 provider 建不起来或 401，而不是分数差）。
- **prompt 直接从工作区的事实生成**：`README.md` + `.janus-task.json` 读出来拼进 prompt。
  注意 README 里**带着 rubric**（Janus 自己生成的题目文件就这样）——我们照原样给，
  因为 agent 本来就能 `read_file README.md`：只从 prompt 里删掉它是自欺欺人。
  想让评测"盲测"，得把 README 的 rubric 段真的从工作区拿掉，而那就不是同一个 benchmark 了。
- **轮次判据来自 Janus，不来自我们自己**：每轮结束调一次 `score`，`passed` 才收手；
  连续几轮都不达标就如实记为未达标（不无限重试、不偷偷放宽）。
- **每条结论都留证据**：每个行为一个 `result.json` + 一份 `run-report.json`
  （每轮的 score / 测试数 / 改动文件数 / token / 耗时，以及 agent 的最终回答）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: 喂给 agent 的交付要求（刻意不提 rubric：那是评分标准，不是任务说明）
DELIVERY = """
完成标准（自己核对，别只说"已完成"）：
1. 按上面 Verification 里的命令真的跑一次，把输出里的关键结论写出来；
2. 说明改了哪些文件、为什么这样改；
3. **不许为了让测试通过去改测试**：测试断言代表需求，改测试等于没做；
4. 有做不到的部分就如实说清楚卡在哪里，不要含糊过去。
""".strip()


def read_task(workspace: Path) -> tuple[str, dict[str, Any]]:
    """读题目工作区的事实：README（任务说明）与 .janus-task.json（元数据）。"""
    readme = workspace / "README.md"
    meta_file = workspace / ".janus-task.json"
    text = readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else ""
    meta: dict[str, Any] = {}
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    return text, meta


def build_prompt(text: str, meta: dict[str, Any]) -> str:
    """把题目拼成一条 prompt：只给任务与约束，**不给 rubric**（见模块注释）。"""
    head = f"工作区里是一道待完成的题目（{meta.get('behavior_id', '')}）。"
    return (
        f"{head}\n\n"
        "请先读这个目录（特别是 README.md），然后按里面的 Task / Constraints / Verification "
        "把它做完。\n\n"
        "===== 题目原文 =====\n"
        f"{text.strip()}\n"
        "===== 题目原文结束 =====\n\n"
        f"{DELIVERY}"
    )


def janus_cmd(venv: str | None) -> list[str]:
    """janus-labs 的调用方式：优先用指定 venv 里的可执行文件。"""
    if venv:
        exe = Path(venv) / ("Scripts" if os.name == "nt" else "bin") / (
            "janus-labs.exe" if os.name == "nt" else "janus-labs"
        )
        if exe.is_file():
            return [str(exe)]
        py = Path(venv) / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
        if not py.is_file():
            py = Path(venv) / ("Scripts" if os.name == "nt" else "bin") / "python"
        return [str(py), "-m", "janus_labs"]
    return ["janus-labs"]


def run_cli(cmd: list[str], *, cwd: Path | None = None, timeout: int = 900) -> dict[str, Any]:
    """跑一条 CLI，返回 {ok, code, out, err}。超时也如实返回，不抛。"""
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
        )
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "out": (proc.stdout or "")[-4000:],
            "err": (proc.stderr or "")[-2000:],
            "seconds": round(time.time() - started, 2),
            "cmd": cmd,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "code": -1, "out": "", "err": f"命令不存在：{exc}", "cmd": cmd,
                "seconds": round(time.time() - started, 2)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -2, "out": "", "err": f"超时（>{timeout}s）", "cmd": cmd,
                "seconds": round(time.time() - started, 2)}


# ---------------------------------------------------------------------------
# 让 Code-agent 在题目工作区里干活
# ---------------------------------------------------------------------------
async def agent_run(
    workspace: Path,
    prompt: str,
    *,
    model_tier: str,
    permission_mode: str,
    home: Path,
) -> dict[str, Any]:
    """在一个题目工作区里跑一次 Code-agent 会话，返回这次的观察结果。

    直接走进程内的 SessionManager（不起服务）：评测要的是**可复现**，
    而且这里需要指定 workspace——REST 建会话走的是 `workspaces/<session_id>`，
    指定目录是代码模式（项目会话）的能力。
    """
    os.environ["CODE_AGENT_HOME"] = str(home)
    # 关键：**必须**先读 .env。`CODE_AGENT_PROVIDER` / `DEEPSEEK_API_KEY` 通常写在 .env 里
    # （cli.py 与 server.py 都会先调它，脚本自己跑时也得调），否则 in-process 运行拿不到
    # key —— 表现为"provider 建不起来"或直接 401，而不是评测结果差。
    from code_agent.config import load_env_file

    load_env_file()

    from code_agent.agent.router import ModelRouter
    from code_agent.api.app import build_registry
    from code_agent.config import transcripts_dir
    from code_agent.sessions.manager import SessionManager
    from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo
    from code_agent.storage.jsonl import TranscriptStore
    from code_agent.codeintel.paths import root_key
    from code_agent.codeintel.service import CodeIntelService

    db = Database(home / "code_agent.db")
    users = UserRepo(db)
    from code_agent.api.security import hash_password

    username = "janus-eval"
    user = users.get_by_username(username) or users.create(
        username, hash_password("janus-eval"), role="user"
    )
    projects = ProjectRepo(db)
    mgr = SessionManager(
        transcript=TranscriptStore(transcripts_dir()),
        session_repo=SessionRepo(db),
        approval_repo=ApprovalRepo(db),
        router=ModelRouter(provider_name=os.environ.get("CODE_AGENT_PROVIDER", "") or None),
        registry=build_registry(),
        project_repo=projects,
        code_intel=CodeIntelService(projects),
    )
    # 每个题目目录注册成一个项目 → 会话的工作区就是它（agent 的写盘落在题目仓库里）
    project = projects.get_by_root(user["id"], root_key(workspace)) or projects.create(
        user["id"], f"janus:{workspace.name}", str(workspace), root_key(workspace)
    )
    meta = mgr.create_project_session(
        user["id"], project["id"], permission_mode=permission_mode, reuse=False
    )
    rt = mgr.get_runtime(meta["id"], user["id"])
    started = time.time()
    result = await rt.submit(prompt)
    events = rt.transcript.replay(meta["id"])
    return {
        "session_id": meta["id"],
        "stop_reason": result.stop_reason,
        "text": result.text,
        "turns": result.turns,
        "tokens": result.usage.model_dump(),
        "seconds": round(time.time() - started, 2),
        "tool_calls": sum(1 for e in events if e.type.value == "tool_use"),
        "edits": sorted(
            {
                str(e.payload["call"]["arguments"].get("path") or "")
                for e in events
                if e.type.value == "tool_use"
                and e.payload.get("call", {}).get("name") in ("edit_file", "write_file")
            }
            - {""}
        ),
    }


def commit_workspace(workspace: Path, message: str) -> dict[str, Any]:
    """把 agent 的改动提交进题目仓库。

    **这一步不能省**：`score` 读的是已提交的 diff。真机实测，不提交时
    它报 `Files changed: 0 (no committed changes)` 并给 F —— 文件明明已经改好了。
    评测器的责任是把现场固定下来，不该指望 agent 记得 commit。
    """
    run_cli(["git", "add", "-A"], cwd=workspace, timeout=120)
    dirty = run_cli(["git", "status", "--porcelain"], cwd=workspace, timeout=60)
    changed = [ln for ln in (dirty["out"] or "").splitlines() if ln.strip()]
    if not changed:
        return {"committed": False, "reason": "没有改动", "files": 0}
    commit = run_cli(["git", "commit", "-m", message], cwd=workspace, timeout=120)
    files = run_cli(["git", "show", "--stat", "--oneline", "HEAD"], cwd=workspace, timeout=60)
    return {"committed": commit["ok"], "files": len(changed), "stat": files["out"][-800:]}


def read_result(path: Path) -> dict[str, Any]:
    """读 score 产出的 result.json（只取我们汇总要用的字段）。

    注意：`score` 的**退出码**在低于阈值时是 1 —— 那不是"打分失败"，是"分数不够"。
    所以"这次打分成功了没有"要看**结果文件在不在**，不能看退出码。
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {
        "score": data.get("score"),
        "normalized_score": data.get("normalized_score"),
        "passed": bool(data.get("passed")),
        "threshold": data.get("threshold"),
        "notes": data.get("notes") or [],
        "files_changed": (data.get("git_diff") or {}).get("files_changed") or [],
        "insertions": (data.get("git_diff") or {}).get("insertions"),
        "deletions": (data.get("git_diff") or {}).get("deletions"),
        "tests": {
            "passed": (data.get("test_results") or {}).get("passed"),
            "failed": (data.get("test_results") or {}).get("failed"),
            "skipped": (data.get("test_results") or {}).get("skipped"),
        },
        "agent": data.get("agent"),
        "model": data.get("model"),
        "duration_ms": data.get("duration_ms"),
    }


def fmt_score(result: dict[str, Any]) -> str:
    """把 normalized_score 显示成 CLJ 那样的 /100（result.json 里是 0~1 的小数）。

    真机踩到过：`normalized_score: 0.1` 其实对应 CLI 打印的 "10.0/100"，
    直接把它当 /100 写着看会小十倍 —— 报告里的数字必须与评分器自己打印的一致。
    """
    norm = result.get("normalized_score")
    if norm is None:
        return "-"
    return f"{round(float(norm) * 100, 1)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="用 Janus Labs 测评 Code-agent")
    ap.add_argument("--tasks", required=True, help="janus-labs init 生成的题目目录")
    ap.add_argument("--out", default="", help="结果输出目录（默认 <tasks>/../janus-results）")
    ap.add_argument("--home", default="", help="Code-agent 数据目录（默认 <out>/.code-agent）")
    ap.add_argument("--agent-venv", default="", help="装了 janus-labs 的 venv（不填用 PATH 里的）")
    ap.add_argument("--rounds", type=int, default=3, help="每个行为最多几轮 prompt")
    ap.add_argument("--model-tier", default="default", help="Code-agent 的模型档位（lite/default/craft）")
    ap.add_argument(
        "--permission-mode", default="full-access",
        choices=["read-only", "workspace-write", "full-access"],
        help=("默认 full-access：**评测必须**这样才能真跑测试。原因很硬：bash 的白名单里"
              "没有 pytest/radon，workspace-write 下每条测试命令都要人工审批，而 in-process "
              "评测没有审批界面 → 等 APPROVAL_TIMEOUT（默认 120s）后被拒，评测会变成"
              "「跑不了测试」的假失败。题目目录是 janus 生成的一次性目录，用 full-access 合理。"),
    )
    ap.add_argument("--behaviors", default="", help="只跑这些行为（逗号分隔的行为目录名）")
    ap.add_argument("--judge", action="store_true",
                    help="score 时用 LLM-as-judge（DeepEval GEval，需要 API key；不加则走确定性打分）")
    ap.add_argument("--judge-model", default="gpt-4o", help="judge 用的模型（配合 --judge）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的东西，不真跑")
    args = ap.parse_args()

    tasks_root = Path(args.tasks).resolve()
    if not tasks_root.is_dir():
        print(f"[X] 题目目录不存在：{tasks_root}\n    先跑：janus-labs init --suite refactor-storm --output {tasks_root}")
        return 2
    out_root = Path(args.out).resolve() if args.out else tasks_root.parent / "janus-results"
    out_root.mkdir(parents=True, exist_ok=True)
    home = Path(args.home).resolve() if args.home else out_root / ".code-agent"
    home.mkdir(parents=True, exist_ok=True)

    wanted = {s.strip() for s in args.behaviors.split(",") if s.strip()}
    workspaces = sorted(
        p for p in tasks_root.iterdir()
        if p.is_dir() and (p / ".janus-task.json").is_file() and (not wanted or p.name in wanted)
    )
    if not workspaces:
        print(f"[X] {tasks_root} 里没有题目工作区（找不到 .janus-task.json）")
        return 2

    cli = janus_cmd(args.agent_venv or None)
    print(f"janus-labs：{' '.join(cli)}")
    print(f"题目 {len(workspaces)} 个，最多每行为 {args.rounds} 轮")
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model_tier": args.model_tier,
        "permission_mode": args.permission_mode,
        "judge": bool(args.judge),
        "behaviors": [],
    }
    for ws in workspaces:
        text, meta = read_task(ws)
        prompt = build_prompt(text, meta)
        entry: dict[str, Any] = {
            "workspace": str(ws),
            "behavior_id": meta.get("behavior_id", ws.name),
            "threshold": meta.get("threshold"),
            "rounds": [],
        }
        print(f"\n=== {entry['behavior_id']} ===")
        if args.dry_run:
            print(prompt[:600] + ("…" if len(prompt) > 600 else ""))
            entry["rounds"].append({"dry_run": True})
            report["behaviors"].append(entry)
            continue
        score_out = out_root / f"{entry['behavior_id']}.json"
        verdict = ""
        for i in range(1, max(1, args.rounds) + 1):
            ask = prompt if i == 1 else (
                prompt
                + "\n\n===== Janus Labs 上一轮判定（这是外部评分器给的，不是我们的自评）=====\n"
                + verdict
                + "\n请针对它指出的问题继续改；仍然不要改测试断言来凑通过。"
            )
            print(f"[轮 {i}] 让 Code-agent 干活…")
            try:
                obs = asyncio.run(
                    agent_run(
                        ws, ask,
                        model_tier=args.model_tier,
                        permission_mode=args.permission_mode,
                        home=home,
                    )
                )
            except Exception as exc:                     # noqa: BLE001 —— 一次会话崩了不该毁掉整轮评测
                obs = {"error": f"{type(exc).__name__}: {exc}"}
            commit = commit_workspace(ws, f"code-agent round {i}")
            score_cmd = cli + [
                "score", "--workspace", ".", "--output", str(score_out),
                "--agent", "code-agent", "--agent-model", args.model_tier,
            ]
            # 注意：`score` **没有** --mock（`run` 才有）。不加 --judge 时走确定性的
            # 离线打分；--judge 才会调 DeepEval/GEval（要 API key）。
            if args.judge:
                score_cmd += ["--judge", "--model", args.judge_model]
            run_cli(score_cmd, cwd=ws, timeout=1800)
            result = read_result(score_out)

            entry["rounds"].append({
                "round": i,
                "agent": obs,
                "commit": commit,
                "result": result,
            })
            print(f"    agent: stop={obs.get('stop_reason')} 轮={obs.get('turns')} "
                  f"工具={obs.get('tool_calls')} 改动文件={len(obs.get('edits') or [])}")
            print(f"    commit: {commit['files']} 个文件 → {commit['committed']}")
            print(f"    score: {fmt_score(result)} /100 "
                  f"(outcome {result.get('score')}/10, threshold {result.get('threshold')}, "
                  f"passed={result.get('passed')})")
            if result.get("notes"):
                print(f"    notes: {'; '.join(str(n) for n in result['notes'])}")
            if result.get("passed"):
                print("    → Janus 判定达标，收手")
                break
            verdict = json.dumps(result, ensure_ascii=False, indent=2)[:3000]

        entry["final"] = entry["rounds"][-1].get("result", {}) if entry["rounds"] else {}
        entry["result_file"] = str(score_out) if score_out.is_file() else ""
        report["behaviors"].append(entry)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report_file = out_root / "run-report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.dry_run:
        return 0

    # 汇总表：一行一个行为，给出最终分与"是否达标"，不做加权平均（那是 3DMark 的活）
    print("\n=== 结果汇总（Janus Labs · refactor-storm）===")
    print(f"{'行为':<34}{'轮次':>4}{'得分/100':>10}{'达标':>6}  测试(通过/失败)  改动文件")
    for b in report["behaviors"]:
        fin = b.get("final") or {}
        t = fin.get("tests") or {}
        print(f"{b['behavior_id']:<34}{len(b['rounds']):>4}"
              f"{fmt_score(fin):>10}"
              f"{('是' if fin.get('passed') else '否'):>6}"
              f"      {t.get('passed')}/{t.get('failed')}        {len(fin.get('files_changed') or [])}")
    print(f"\n报告：{report_file}")
    print("与基线比：janus-labs compare <result.json> --auto-baseline")
    passed = sum(1 for b in report["behaviors"] if (b.get("final") or {}).get("passed"))
    scored = sum(1 for b in report["behaviors"] if (b.get("final") or {}).get("score") is not None)
    print(f"打分成功 {scored}/{len(report['behaviors'])}，达标 {passed}/{len(report['behaviors'])}")
    return 0 if scored else 1


if __name__ == "__main__":
    raise SystemExit(main())
