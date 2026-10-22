"""表达式白名单求值。

为什么不用 sympy 的 ``sympify``：它内部走 ``eval``，实测

    sympify("__import__('os').popen('echo x').read()")   # 返回 'x\\n'

可以在服务器上执行任意命令。本插件对群里所有人开放，所以必须自己做一层。

做法：``ast.parse`` 解析 → 只放行算术/幂/取模 + 白名单函数 + 变量名与常量 →
把数字常量强制转成 float（避免 ``2**10**9`` 这种整数炸弹）→ ``compile`` 后
用 ``{"__builtins__": {}}`` 求值。全程不允许属性访问（``a.b``）、下标、lambda、
关键字参数，因此拿不到任何对象，也就拿不到 ``__class__`` 之类的逃逸入口。
"""

from __future__ import annotations

import ast
import math
from typing import Callable

import numpy as np

# 输入上限：够写完整的物理表达式，又不给解析器太多活儿
MAX_SOURCE_LEN = 300
MAX_NODES = 300

# 允许的函数：全部是 numpy 的向量化实现，不碰文件/网络/进程
ALLOWED_FUNCS: dict[str, Callable] = {
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": np.arcsin,
    "acos": np.arccos,
    "atan": np.arctan,
    "atan2": np.arctan2,
    "sinh": np.sinh,
    "cosh": np.cosh,
    "tanh": np.tanh,
    "exp": np.exp,
    "log": np.log,
    "log10": np.log10,
    "log2": np.log2,
    "sqrt": np.sqrt,
    "abs": np.abs,
    "floor": np.floor,
    "ceil": np.ceil,
    "sign": np.sign,
    "min": np.minimum,
    "max": np.maximum,
    "clip": np.clip,
}

ALLOWED_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}

# 单个函数最多接受的参数个数（clip 需要 3 个）
_MAX_ARGS = 3

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


class ExprError(ValueError):
    """表达式不符合白名单时抛出，消息直接回给用户。"""


def _validate(node: ast.AST, variables: set[str], counter: list[int]) -> None:
    """递归校验语法树，遇到白名单外的节点立即抛错。"""
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise ExprError("表达式过于复杂")

    if not isinstance(node, _ALLOWED_NODES):
        raise ExprError(f"不支持的语法：{type(node).__name__}")

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExprError("只允许数字常量")
        return

    if isinstance(node, ast.Name):
        if node.id not in variables and node.id not in ALLOWED_FUNCS and node.id not in ALLOWED_CONSTS:
            raise ExprError(f"未知符号：{node.id}")
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExprError("只允许直接调用白名单函数")
        if node.func.id not in ALLOWED_FUNCS:
            raise ExprError(f"不允许的函数：{node.func.id}")
        if node.keywords:
            raise ExprError("不允许关键字参数")
        if len(node.args) > _MAX_ARGS:
            raise ExprError("参数过多")
        for arg in node.args:
            _validate(arg, variables, counter)
        return

    for child in ast.iter_child_nodes(node):
        _validate(child, variables, counter)


def _to_float_constants(tree: ast.Expression) -> ast.Expression:
    """把数字常量统一成 float，堵掉整数幂炸弹（2**10**9 会卡死进程）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            node.value = float(node.value)
    return tree


def compile_expression(source: str, variables: tuple[str, ...] = ("x",)) -> Callable[..., np.ndarray]:
    """把用户/AI 给的表达式编译成可直接用 numpy 调用的函数。

    Args:
        source: 表达式文本，例如 ``sin(x) + x**2/2``。
        variables: 允许出现的变量名，例如 ``("x",)`` 或 ``("x", "y")``。

    Returns:
        接受 numpy 数组并返回 numpy 数组的函数。

    Raises:
        ExprError: 表达式为空、过长、语法不合法或含白名单外的结构。
    """
    if not source or not source.strip():
        raise ExprError("表达式为空")
    if len(source) > MAX_SOURCE_LEN:
        raise ExprError(f"表达式过长（上限 {MAX_SOURCE_LEN} 字符）")

    # 常见的手写习惯：^ 当幂、隐式乘号、中文全角括号
    text = source.strip().replace("^", "**").replace("（", "(").replace("）", ")")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"语法错误：{exc.msg}") from exc

    _validate(tree, set(variables), [0])
    _to_float_constants(tree)
    code = compile(tree, "<mathviz>", "eval")

    namespace: dict[str, object] = {"__builtins__": {}}
    namespace.update(ALLOWED_CONSTS)
    namespace.update(ALLOWED_FUNCS)

    def _run(**arrays: np.ndarray) -> np.ndarray:
        local = dict(namespace)
        local.update(arrays)
        with np.errstate(all="ignore"):
            value = eval(code, {"__builtins__": {}}, local)  # noqa: S307 - 已做白名单
        return np.asarray(value, dtype=float)

    return _run
