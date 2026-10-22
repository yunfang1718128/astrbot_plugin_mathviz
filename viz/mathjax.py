"""LaTeX → SVG（MathJax v3，Node 子进程），供 Typst 用 #image() 嵌入。

为什么用 MathJax 而不是自己写转换器：Typst 不认 LaTeX，而"LaTeX→Typst"的转换器
（如 tylax）不可能覆盖全部写法——`\\beta I` 会被粘成 `betaI`、`\\underbrace`/`\\si`
不支持，结果就是不停地打补丁。MathJax 是真正的 TeX 引擎，直接产出**自包含 SVG**
（字体路径内嵌在 `<defs>`，不依赖系统字体），Typst 原生支持嵌入 SVG。

MathJax 是**可选依赖**：没装 Node 或没装 `mathjax-full` 时，调用方会退回到内置的
Typst 数学引擎（覆盖率低一些，但只要系统有 Typst 就能跑）。

安装（插件首次运行会提示）：
    mkdir -p <插件数据目录>/mathjax && cd $_ && npm i mathjax-full
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

# 1ex 对应多少 pt：MathJax 里 em:16 / ex:8，即 1ex = 0.5em；正文 10.5pt → 5.25pt
EX_TO_PT = 5.25

TIMEOUT_SECONDS = 60

_state: dict[str, Path | None] = {"dir": None}

_SIZE_RE = re.compile(r"^\s*([\d.]+)ex\s*$")
_VALIGN_RE = re.compile(r"vertical-align:\s*(-?[\d.]+)ex")

_NODE = shutil.which("node") or shutil.which("node.exe")


class MathJaxError(RuntimeError):
    """MathJax 整体失败（node 起不来、包缺失等），调用方应回退内置引擎。"""


def configure(directory: str | Path | None) -> None:
    """指定 MathJax 安装目录（内含 node_modules/mathjax-full）。"""
    _state["dir"] = Path(directory) if directory else None


def mathjax_dir() -> Path | None:
    """当前生效的 MathJax 目录。"""
    return _state["dir"]


def node_path() -> str | None:
    """系统里的 node 可执行文件。"""
    return _NODE


def script_path() -> Path:
    """渲染脚本随插件走（不依赖安装目录）。"""
    return Path(__file__).resolve().parent / "tex2svg.js"


def available() -> bool:
    """MathJax 是否就绪（node + mathjax-full 包）。"""
    directory = _state["dir"]
    if not _NODE or directory is None:
        return False
    return (directory / "node_modules" / "mathjax-full").exists()


def _ex(value: str) -> float:
    """把 MathJax 的 `12.34ex` 转成浮点数。"""
    match = _SIZE_RE.match(value or "")
    return float(match.group(1)) if match else 0.0


def render_batch(items: list[dict]) -> list[dict]:
    """批量把 LaTeX 渲染成 SVG。

    Args:
        items: 每项 ``{"latex": str, "display": bool}``。

    Returns:
        与输入等长的列表，每项 ``{"ok": bool, "svg": str, "width_pt": float,
        "height_pt": float, "baseline_pt": float, "error": str}``。

    Raises:
        MathJaxError: node 进程整体失败（调用方应回退内置引擎）。
    """
    if not items:
        return []
    if not available():
        raise MathJaxError("MathJax 未就绪（缺 node 或 mathjax-full）")

    directory = _state["dir"]
    assert directory is not None
    payload = json.dumps(
        [{"tex": item.get("latex", ""), "display": bool(item.get("display", True))} for item in items],
        ensure_ascii=False,
    )
    try:
        proc = subprocess.run(
            [_NODE or "node", str(script_path()), str(directory)],
            input=payload.encode(),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MathJaxError(f"node 调用失败：{exc}") from exc

    if proc.returncode != 0:
        raise MathJaxError(f"node 退出码 {proc.returncode}：{proc.stderr.decode()[:300]}")

    try:
        data = json.loads(proc.stdout.decode())
    except json.JSONDecodeError as exc:
        raise MathJaxError(f"MathJax 输出无法解析：{exc}") from exc

    results: list[dict] = []
    for raw in data.get("results", []):
        if not raw.get("ok"):
            results.append({"ok": False, "error": raw.get("error", "未知错误")})
            continue
        svg = raw.get("svg") or ""
        if not svg.startswith("<svg"):
            results.append({"ok": False, "error": "返回里没有 <svg>"})
            continue
        # 尺寸由 Node 侧直接给出（不在 Python 里用正则抠，正则会在某些结构上截断标签）
        width_ex = _ex(str(raw.get("width") or ""))
        height_ex = _ex(str(raw.get("height") or ""))
        valign = _VALIGN_RE.search(str(raw.get("style") or ""))
        baseline_ex = float(valign.group(1)) if valign else 0.0
        results.append(
            {
                "ok": True,
                "svg": svg,
                "width_pt": round(width_ex * EX_TO_PT, 2),
                "height_pt": round(height_ex * EX_TO_PT, 2),
                # MathJax 的 vertical-align 负值表示下沉；Typst 的 baseline 正数下沉
                "baseline_pt": round(-baseline_ex * EX_TO_PT, 2),
            }
        )
    while len(results) < len(items):
        results.append({"ok": False, "error": "MathJax 返回条数不足"})
    return results
