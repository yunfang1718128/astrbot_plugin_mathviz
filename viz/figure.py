"""matplotlib 统一配置：中文字体、配色、坐标轴样式、落盘。

只在这个子进程里用，AstrBot 主进程不 import matplotlib，避免常驻内存。
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")  # 必须在 import pyplot 之前

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

# 中文字体候选（跨平台）。注意：**.ttc 的第 0 个 face 常是日文版（JP）**，
# matplotlib 的 addfont 只认 face 0，所以优先用抽取出来的简中 face（SC）。
_FONT_CANDIDATES = (
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)

INK = "#1f2933"
GRID = "#cbd2d9"
CURVE_COLORS = ("#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2")
MAX_PIXELS = 4200  # 单边像素上限，防止 3D/超大 DPI 撑爆上传


def setup(font_path: str = "") -> str:
    """注册中文字体并设好全局 rcParams。

    Args:
        font_path: 额外指定的字体文件；为空则按内置候选顺序找。

    Returns:
        实际使用的字体族名（找不到中文时为空串，此时中文会显示成方框）。
    """
    candidates = ([font_path] if font_path else []) + list(_FONT_CANDIDATES)
    family = ""
    for path in candidates:
        if path and os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                family = font_manager.FontProperties(fname=path).get_name()
                break
            except Exception:
                continue

    plt.rcParams.update(
        {
            "font.sans-serif": [family, "DejaVu Sans"] if family else ["DejaVu Sans"],
            "font.family": "sans-serif",
            "axes.unicode_minus": False,  # 负号用 ASCII，否则中文字体里可能缺字
            "axes.edgecolor": INK,
            "axes.labelcolor": INK,
            "text.color": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
        }
    )
    return family


def new_figure(width: float = 7.0, height: float = 4.6, dpi: int = 140) -> tuple[Figure, plt.Axes]:
    """建图并返回 (fig, ax)。"""
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    return fig, ax


def style_axes(ax: plt.Axes, show_grid: bool = True, centered: bool = True) -> None:
    """坐标轴穿过原点并加网格（function/parametric/polar 用）。"""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if centered:
        ax.spines["left"].set_position(("data", 0))
        ax.spines["bottom"].set_position(("data", 0))
        # 箭头状轴端点，方便看正方向
        ax.plot(1, 0, ">k", transform=ax.get_yaxis_transform(), clip_on=False, markersize=5)
        ax.plot(0, 1, "^k", transform=ax.get_xaxis_transform(), clip_on=False, markersize=5)
    ax.grid(show_grid)
    ax.set_axisbelow(True)


def save(fig: Figure, out_path: str, dpi: int = 140) -> tuple[int, int]:
    """保存为 PNG，返回像素尺寸。超过上限时缩小 DPI 重存一次。"""
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", pad_inches=0.16)
    plt.close(fig)

    from PIL import Image

    with Image.open(out_path) as image:
        width, height = image.size
    if max(width, height) > MAX_PIXELS:
        scale = MAX_PIXELS / max(width, height)
        # 直接重画成本高，这里用高质量重采样兜底
        with Image.open(out_path) as image:
            resized = image.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.LANCZOS,
            )
            resized.save(out_path, "PNG", optimize=True)
        width, height = resized.size
    return width, height
