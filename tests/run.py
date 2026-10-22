#!/usr/bin/env python3
"""跑分：单公式 + 整页排版。

用法（在插件目录下）：
    python tests/run.py --report
    python tests/run.py --mathjax-dir /path/to/mathjax --report

判定口径：
- 公式：`expect` 为 ok 的必须 ok；为 merror 的必须渲染成 MathJax 错误标记
  （即"已知不支持"，不允许悄无声息地变成 ok —— 那说明兼容面变了，值得看一眼）；
- 整页：必须 ok，且统计 degraded / repaired / 耗时。

退出码：0 = 全部符合预期；1 = 有不符合项。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"

# MathJax 渲染失败时会把出错的记号标成红字（实测：<g data-mml-node="mtext" fill="red" ...>），
# 用它区分"排版成功"和"排出来了但是错的"
_ERROR_MARKERS = ('fill="red"', "data-mjx-error")


def classify(result: dict) -> str:
    """把 MathJax 单条结果归类为 ok / merror / fail。"""
    if not result.get("ok"):
        return "fail"
    svg = result.get("svg", "")
    if any(marker in svg for marker in _ERROR_MARKERS):
        return "merror"
    return "ok"


def run_formulas(plugin: Path) -> tuple[int, int, list[str]]:
    sys.path.insert(0, str(plugin))
    from viz import mathjax as mj

    entries = []
    for line in (CORPUS / "formulas.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(json.loads(line))

    started = time.time()
    results = mj.render_batch([{"latex": e["tex"], "display": e.get("display", True)} for e in entries])
    cost = time.time() - started

    passed = failed = 0
    problems: list[str] = []
    for entry, result in zip(entries, results):
        got = classify(result)
        want = entry.get("expect", "ok")
        if got == want:
            passed += 1
        else:
            failed += 1
            note = entry.get("note", "")
            problems.append(f"{want}→{got}: {entry['tex'][:60]}" + (f"  ({note})" if note else ""))
    print(f"formulas  {passed}/{len(entries)} passed   ({cost:.2f}s, {len(entries)/max(cost,1e-6):.0f}/s)")
    for item in problems:
        print(f"  ✗ {item}")
    return passed, failed, problems


def run_pages(plugin: Path, mathjax_dir: str) -> tuple[int, int, list[str]]:
    docs = sorted((CORPUS / "docs").glob("*.md"))
    ok = bad = 0
    problems: list[str] = []
    times: list[int] = []
    degraded_total = repaired_total = 0

    for doc in docs:
        out = Path("/tmp") / f"corpus_{doc.stem}.png"
        payload = {
            "markdown": doc.read_text(encoding="utf-8"),
            "out": str(out),
            "width_pt": 500,
            "ppi": 180,
            "mathjax_dir": mathjax_dir,
            "font_path": "",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "viz.sheet"],
            cwd=str(plugin),
            input=json.dumps(payload, ensure_ascii=False).encode(),
            capture_output=True,
            timeout=300,
        )
        lines = (proc.stdout or b"").decode().strip().splitlines()
        data = json.loads(lines[-1]) if lines else {"ok": False, "error": (proc.stderr or b"").decode()[-200:]}
        if data.get("ok"):
            ok += 1
            times.append(int(data.get("ms", 0)))
            degraded_total += int(data.get("degraded", 0))
            repaired_total += int(data.get("repaired", 0))
            print(
                f"  ✓ {doc.name:20} {data.get('w')}x{data.get('h')} "
                f"mj={data.get('mathjax_ok')} fail={data.get('mathjax_fail')} "
                f"degraded={data.get('degraded')} {data.get('ms')}ms"
            )
        else:
            bad += 1
            problems.append(f"{doc.name}: {data.get('error')}")
            print(f"  ✗ {doc.name:20} {data.get('error')}")

    print(
        f"pages     {ok}/{len(docs)} ok   "
        f"avg {sum(times)//max(len(times),1)}ms  max {max(times) if times else 0}ms  "
        f"degraded {degraded_total}  repaired {repaired_total}"
    )
    return ok, bad, problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", default=str(HERE.parent), help="插件根目录")
    parser.add_argument("--mathjax-dir", default="", help="MathJax 目录（含 node_modules）")
    parser.add_argument("--report", action="store_true", help="打印汇总")
    args = parser.parse_args()

    plugin = Path(args.plugin).resolve()
    sys.path.insert(0, str(plugin))

    from viz import mathjax as mj

    mj.configure(args.mathjax_dir or None)
    if not mj.available():
        print("✗ MathJax 不可用：请用 --mathjax-dir 指定，或先 npm i mathjax-full")
        return 1

    print(f"plugin   {plugin}")
    print(f"mathjax  {args.mathjax_dir or mj.mathjax_dir()}")
    print()
    p_ok, p_bad, p_prob = run_formulas(plugin)
    d_ok, d_bad, d_prob = run_pages(plugin, args.mathjax_dir)

    problems = p_prob + d_prob
    print()
    if problems:
        print(f"不符合预期 {len(problems)} 项")
        return 1
    print("全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
