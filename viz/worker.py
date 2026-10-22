"""渲染子进程入口：从 stdin 读 JSON 规格，画图，写 PNG，向 stdout 打一行 JSON 结果。

单独进程的原因：matplotlib 一 import 就要几十 MB，「用完退出」比常驻在
AstrBot 进程里便宜得多。主进程只负责组规格 + 收图。
"""

from __future__ import annotations

import json
import math
import sys
import time
import traceback

import numpy as np

from viz import figure
from viz.safeexpr import ExprError, compile_expression

MAX_POINTS_2D = 2000
MAX_GRID_IMPLICIT = 420
LIMIT_ABS = 1e6
MIN_SPAN = 1e-6
MAX_ELEMENTS = 80
MAX_TEXT_LEN = 60


class SpecError(ValueError):
    """规格不合法，消息直接回给用户。"""


def _num(value: object, name: str) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{name} 不是数字：{value!r}") from exc
    if not math.isfinite(result):
        raise SpecError(f"{name} 不是有限数")
    if abs(result) > LIMIT_ABS:
        raise SpecError(f"{name} 超出范围（|{name}| ≤ {LIMIT_ABS:g}）")
    return result


def _span(spec: dict, key: str, default: tuple[float, float]) -> tuple[float, float]:
    """读 [min, max] 形式的区间。"""
    raw = spec.get(key)
    if raw is None:
        return default
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise SpecError(f"{key} 需要 [min, max] 两个数")
    low, high = _num(raw[0], f"{key}[0]"), _num(raw[1], f"{key}[1]")
    if high <= low:
        raise SpecError(f"{key} 的 max 必须大于 min")
    if high - low < MIN_SPAN:
        raise SpecError(f"{key} 区间太小")
    return low, high


def _label(spec: dict, key: str, default: str = "") -> str:
    text = str(spec.get(key) or default)
    return text[:MAX_TEXT_LEN]


def _finish(fig, out: str, dpi: int, extra: dict | None = None) -> dict:
    width, height = figure.save(fig, out, dpi=dpi)
    result = {"ok": True, "out": out, "w": width, "h": height}
    if extra:
        result.update(extra)
    return result


# ────────────────────────────── 各类图 ──────────────────────────────


def render_function(spec: dict, out: str, dpi: int) -> dict:
    exprs = spec.get("expressions") or []
    if not isinstance(exprs, list) or not exprs:
        raise SpecError("expressions 需要至少一个表达式")
    x_min, x_max = _span(spec, "x", (-10.0, 10.0))
    xs = np.linspace(x_min, x_max, MAX_POINTS_2D)

    fig, ax = figure.new_figure(dpi=dpi)
    plotted = 0
    for index, raw in enumerate(exprs):
        func = compile_expression(str(raw), ("x",))
        ys = func(x=xs)
        if np.shape(ys) != np.shape(xs):
            raise SpecError(f"表达式不是一元函数：{raw}")
        ys = np.where(np.isfinite(ys), ys, np.nan)
        if np.all(np.isnan(ys)):
            continue
        color = figure.CURVE_COLORS[index % len(figure.CURVE_COLORS)]
        ax.plot(xs, ys, color=color, linewidth=float(spec.get("line_width") or 2.0), label=str(raw)[:MAX_TEXT_LEN])
        plotted += 1
    if plotted == 0:
        raise SpecError("表达式在给定区间内没有有效值（可能处处无定义）")

    ax.set_xlim(x_min, x_max)
    if spec.get("y"):
        ax.set_ylim(*_span(spec, "y", (-1.0, 1.0)))
    else:
        finite = [np.nanmin(line.get_ydata()) for line in ax.lines if np.any(np.isfinite(line.get_ydata()))]
        finite_max = [np.nanmax(line.get_ydata()) for line in ax.lines if np.any(np.isfinite(line.get_ydata()))]
        if finite and finite_max:
            low, high = min(finite), max(finite_max)
            pad = (high - low) * 0.12 or 1.0
            ax.set_ylim(low - pad, high + pad)

    figure.style_axes(ax, show_grid=bool(spec.get("show_grid", True)))
    ax.set_xlabel(_label(spec, "xlabel", "x"))
    ax.set_ylabel(_label(spec, "ylabel", "y"))
    if plotted > 1:
        ax.legend(loc="best", framealpha=0.9)
    if spec.get("title"):
        ax.set_title(_label(spec, "title"))
    return _finish(fig, out, dpi, {"curves": plotted})


def render_implicit(spec: dict, out: str, dpi: int) -> dict:
    equation = str(spec.get("equation") or "").strip()
    if not equation:
        raise SpecError("equation 不能为空")
    if equation.count("=") > 1:
        raise SpecError("只支持一个等号，例如 x**2+y**2=1")
    if "=" in equation:
        left, right = equation.split("=", 1)
        func = compile_expression(f"({left})-({right})", ("x", "y"))
        shown = equation
    else:
        func = compile_expression(equation, ("x", "y"))
        shown = f"{equation} = 0"

    x_min, x_max = _span(spec, "x", (-5.0, 5.0))
    y_min, y_max = _span(spec, "y", (-5.0, 5.0))
    gx, gy = np.meshgrid(
        np.linspace(x_min, x_max, MAX_GRID_IMPLICIT),
        np.linspace(y_min, y_max, MAX_GRID_IMPLICIT),
    )
    values = func(x=gx, y=gy)
    values = np.where(np.isfinite(values), values, np.nan)

    fig, ax = figure.new_figure(dpi=dpi)
    if not np.any(np.isfinite(values)):
        raise SpecError("该方程在给定区间内无有效取值")
    ax.contour(gx, gy, values, levels=[0.0], colors=[figure.CURVE_COLORS[0]], linewidths=2.0)
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    figure.style_axes(ax, show_grid=bool(spec.get("show_grid", True)), centered=False)
    ax.set_xlabel(_label(spec, "xlabel", "x"))
    ax.set_ylabel(_label(spec, "ylabel", "y"))
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(_label(spec, "title") or shown)
    return _finish(fig, out, dpi)


def render_parametric(spec: dict, out: str, dpi: int) -> dict:
    x_func = compile_expression(str(spec.get("x_expr") or ""), ("t",))
    y_func = compile_expression(str(spec.get("y_expr") or ""), ("t",))
    t_min, t_max = _span(spec, "t", (0.0, 2 * math.pi))
    ts = np.linspace(t_min, t_max, MAX_POINTS_2D)
    xs = np.where(np.isfinite(x_func(t=ts)), x_func(t=ts), np.nan)
    ys = np.where(np.isfinite(y_func(t=ts)), y_func(t=ts), np.nan)

    fig, ax = figure.new_figure(dpi=dpi)
    ax.plot(xs, ys, color=figure.CURVE_COLORS[0], linewidth=float(spec.get("line_width") or 2.0))
    figure.style_axes(ax, show_grid=bool(spec.get("show_grid", True)))
    ax.set_xlabel(_label(spec, "xlabel", "x"))
    ax.set_ylabel(_label(spec, "ylabel", "y"))
    ax.set_aspect("equal", adjustable="datalim")
    title = _label(spec, "title") or f"x={spec.get('x_expr')}, y={spec.get('y_expr')}"
    ax.set_title(title)
    return _finish(fig, out, dpi)


def render_polar(spec: dict, out: str, dpi: int) -> dict:
    r_func = compile_expression(str(spec.get("r_expr") or ""), ("theta",))
    t_min, t_max = _span(spec, "theta", (0.0, 2 * math.pi))
    thetas = np.linspace(t_min, t_max, MAX_POINTS_2D)
    rs = np.where(np.isfinite(r_func(theta=thetas)), r_func(theta=thetas), np.nan)

    fig, ax = figure.new_figure(dpi=dpi)
    ax.plot(rs * np.cos(thetas), rs * np.sin(thetas), color=figure.CURVE_COLORS[0],
            linewidth=float(spec.get("line_width") or 2.0))
    figure.style_axes(ax, show_grid=bool(spec.get("show_grid", True)))
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_title(_label(spec, "title") or f"r = {spec.get('r_expr')}")
    return _finish(fig, out, dpi)


# ────────────────────────────── 几何示意图 ──────────────────────────────


def _label_box() -> dict:
    """标签白底：示意图里标注常常压线，垫一层半透明白底才看得清。"""
    return {"boxstyle": "round,pad=0.18", "fc": "white", "ec": "none", "alpha": 0.78}


def _pt(value: object, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SpecError(f"{name} 需要 [x, y]")
    return _num(value[0], f"{name}[0]"), _num(value[1], f"{name}[1]")


def _short(text: object, field: str) -> str:
    value = str(text or "")
    if len(value) > MAX_TEXT_LEN:
        raise SpecError(f"{field} 过长（上限 {MAX_TEXT_LEN} 字）")
    return value


def render_geometry(spec: dict, out: str, dpi: int) -> dict:
    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        raise SpecError("elements 需要至少一个元素")
    if len(elements) > MAX_ELEMENTS:
        raise SpecError(f"元素过多（上限 {MAX_ELEMENTS} 个）")

    fig, ax = figure.new_figure(dpi=dpi)
    from matplotlib.patches import Arc, Circle, Polygon

    xs_all: list[float] = []
    ys_all: list[float] = []

    def keep(x: float, y: float) -> None:
        xs_all.append(x)
        ys_all.append(y)

    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            raise SpecError(f"第 {index + 1} 个元素不是对象")
        kind = str(element.get("type") or "").strip()
        color = str(element.get("color") or figure.INK)
        dashed = bool(element.get("dashed"))
        line_style = "--" if dashed else "-"

        if kind == "point":
            x, y = _pt(element.get("xy"), f"elements[{index}].xy")
            ax.plot([x], [y], "o", color=color, markersize=float(element.get("size") or 5))
            keep(x, y)
            label = _short(element.get("label"), "label")
            if label:
                offset = element.get("offset") or [0, 0]
                dx = _num(offset[0], "offset[0]") if len(offset) > 0 else 0.0
                dy = _num(offset[1], "offset[1]") if len(offset) > 1 else 0.0
                ax.annotate(label, (x, y), textcoords="offset points",
                            xytext=(dx if dx else 6, dy if dy else 6),
                            fontsize=float(spec.get("font_size") or 12), bbox=_label_box())

        elif kind in ("segment", "arrow"):
            x1, y1 = _pt(element.get("from"), f"elements[{index}].from")
            x2, y2 = _pt(element.get("to"), f"elements[{index}].to")
            keep(x1, y1)
            keep(x2, y2)
            if kind == "arrow":
                ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                            arrowprops={"arrowstyle": "-|>", "color": color,
                                        "linewidth": float(element.get("width") or 2.0),
                                        "linestyle": line_style,
                                        "shrinkA": 0, "shrinkB": 6 if element.get("label") else 0})
            else:
                ax.plot([x1, x2], [y1, y2], color=color, linestyle=line_style,
                        linewidth=float(element.get("width") or 1.8))
            label = _short(element.get("label"), "label")
            if label:
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                ax.annotate(label, (mx, my), textcoords="offset points", xytext=(6, 4),
                            fontsize=float(spec.get("font_size") or 12), color=color, bbox=_label_box())

        elif kind == "circle":
            cx, cy = _pt(element.get("center"), f"elements[{index}].center")
            r = _num(element.get("r"), "r")
            if r <= 0:
                raise SpecError("半径必须为正")
            ax.add_patch(Circle((cx, cy), r, fill=bool(element.get("fill")),
                                color=color, linestyle=line_style,
                                linewidth=float(element.get("width") or 1.8), alpha=0.85))
            keep(cx - r, cy - r)
            keep(cx + r, cy + r)
            label = _short(element.get("label"), "label")
            if label:
                ax.annotate(label, (cx, cy), textcoords="offset points", xytext=(4, 4),
                            fontsize=float(spec.get("font_size") or 12), color=color, bbox=_label_box())

        elif kind == "arc":
            cx, cy = _pt(element.get("center"), f"elements[{index}].center")
            r = _num(element.get("r"), "r")
            start = _num(element.get("from_deg") or 0.0, "from_deg")
            end = _num(element.get("to_deg") or 90.0, "to_deg")
            ax.add_patch(Arc((cx, cy), 2 * r, 2 * r, theta1=start, theta2=end, color=color,
                             linestyle=line_style, linewidth=float(element.get("width") or 1.8)))
            mid = math.radians((start + end) / 2)
            keep(cx + r * math.cos(mid), cy + r * math.sin(mid))
            keep(cx - r, cy - r)
            keep(cx + r, cy + r)
            label = _short(element.get("label"), "label")
            if label:
                ax.annotate(label, (cx + r * 1.15 * math.cos(mid), cy + r * 1.15 * math.sin(mid)),
                            fontsize=float(spec.get("font_size") or 12), color=color, bbox=_label_box())

        elif kind == "angle":
            vertex = _pt(element.get("vertex"), f"elements[{index}].vertex")
            ax_pt = _pt(element.get("a"), f"elements[{index}].a")
            bx_pt = _pt(element.get("b"), f"elements[{index}].b")
            radius = _num(element.get("radius") or 0.4, "radius")
            ang_a = math.atan2(ax_pt[1] - vertex[1], ax_pt[0] - vertex[0])
            ang_b = math.atan2(bx_pt[1] - vertex[1], bx_pt[0] - vertex[0])
            deg_a, deg_b = math.degrees(ang_a), math.degrees(ang_b)
            if abs(deg_b - deg_a) > 180:  # 取小于平角的那个扇形
                deg_a, deg_b = deg_b, deg_a
            if deg_a > deg_b:  # matplotlib 要求 theta1 <= theta2，否则会绕远路画 270°
                deg_a, deg_b = deg_b, deg_a
            if element.get("right_angle"):
                d1 = (math.cos(ang_a), math.sin(ang_a))
                d2 = (math.cos(ang_b), math.sin(ang_b))
                s = radius * 0.65
                pts = [
                    (vertex[0] + d1[0] * s, vertex[1] + d1[1] * s),
                    (vertex[0] + (d1[0] + d2[0]) * s, vertex[1] + (d1[1] + d2[1]) * s),
                    (vertex[0] + d2[0] * s, vertex[1] + d2[1] * s),
                ]
                ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color,
                        linewidth=float(element.get("width") or 1.4))
            else:
                ax.add_patch(Arc(vertex, 2 * radius, 2 * radius, theta1=deg_a, theta2=deg_b,
                                 color=color, linewidth=float(element.get("width") or 1.4)))
            mid = math.radians((deg_a + deg_b) / 2)
            label = _short(element.get("label"), "label")
            if label:
                ax.annotate(label,
                            (vertex[0] + radius * 1.45 * math.cos(mid),
                             vertex[1] + radius * 1.45 * math.sin(mid)),
                            fontsize=float(spec.get("font_size") or 12), color=color,
                            ha="center", va="center", bbox=_label_box())
            keep(vertex[0], vertex[1])

        elif kind == "polygon":
            points = element.get("points") or []
            if not isinstance(points, list) or len(points) < 3:
                raise SpecError("多边形至少需要 3 个点")
            parsed = [_pt(p, f"elements[{index}].points[{i}]") for i, p in enumerate(points)]
            ax.add_patch(Polygon(parsed, closed=True, fill=bool(element.get("fill")),
                                 color=color, linestyle=line_style, alpha=0.85,
                                 linewidth=float(element.get("width") or 1.8)))
            for px, py in parsed:
                keep(px, py)
            label = _short(element.get("label"), "label")
            if label:
                cx = sum(p[0] for p in parsed) / len(parsed)
                cy = sum(p[1] for p in parsed) / len(parsed)
                ax.annotate(label, (cx, cy), fontsize=float(spec.get("font_size") or 12),
                            color=color, ha="center", va="center", bbox=_label_box())

        elif kind == "text":
            x, y = _pt(element.get("xy"), f"elements[{index}].xy")
            text = _short(element.get("text"), "text")
            if not text:
                raise SpecError("text 元素缺少 text")
            ax.annotate(text, (x, y), fontsize=float(element.get("font_size") or spec.get("font_size") or 12),
                        color=color, ha=str(element.get("ha") or "center"),
                        va=str(element.get("va") or "center"))
            keep(x, y)

        else:
            raise SpecError(f"不认识的元素类型：{kind or '(空)'}")

    # 视窗：按元素包围盒留白；用户显式给的 xlim/ylim 优先
    if xs_all and ys_all:
        low_x, high_x = min(xs_all), max(xs_all)
        low_y, high_y = min(ys_all), max(ys_all)
        span_x = (high_x - low_x) or 1.0
        span_y = (high_y - low_y) or 1.0
        pad = max(span_x, span_y) * 0.14
        x_range = _span(spec, "x", (low_x - pad, high_x + pad))
        y_range = _span(spec, "y", (low_y - pad, high_y + pad))
        ax.set_xlim(*x_range)
        ax.set_ylim(*y_range)
    elif spec.get("x") and spec.get("y"):
        ax.set_xlim(*_span(spec, "x", (-1, 1)))
        ax.set_ylim(*_span(spec, "y", (-1, 1)))

    if spec.get("equal_aspect", True):
        ax.set_aspect("equal", adjustable="box")
    ax.axis("off")  # 示意图不要坐标轴与刻度  # 示意图不要坐标轴与刻度
    if spec.get("title"):
        ax.set_title(_label(spec, "title"), fontsize=float(spec.get("font_size") or 13))
    return _finish(fig, out, dpi, {"elements": len(elements)})


HANDLERS = {
    "function": render_function,
    "implicit": render_implicit,
    "parametric": render_parametric,
    "polar": render_polar,
    "geometry": render_geometry,
}


def main() -> int:
    started = time.time()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"规格不是合法 JSON：{exc}"}, ensure_ascii=False))
        return 2

    kind = str(payload.get("kind") or "")
    handler = HANDLERS.get(kind)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"不支持的 kind：{kind}"}, ensure_ascii=False))
        return 2

    out = str(payload.get("out") or "")
    if not out:
        print(json.dumps({"ok": False, "error": "缺少 out 路径"}, ensure_ascii=False))
        return 2

    dpi = int(payload.get("dpi") or 140)
    family = figure.setup(str(payload.get("font_path") or ""))

    try:
        result = handler(payload, out, dpi)
    except (SpecError, ExprError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:  # 渲染期的意外一律回一句话，不把 traceback 丢给用户
        print(json.dumps({"ok": False, "error": f"渲染失败：{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        traceback.print_exc(file=sys.stderr)
        return 1

    result["font"] = family
    result["ms"] = int((time.time() - started) * 1000)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
