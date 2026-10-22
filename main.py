"""数学绘图插件（本地、受限）。

设计要点（为什么不用现成的「数学函数绘图器」）：

1. **不执行任意代码**。那个插件用 ``sympify`` 解析表达式，而 ``sympify`` 内部是
   ``eval``——群里任何人说一句「画 __import__('os').popen(...) 的图像」就能在服务器上
   执行命令（已实测）。这里所有表达式都走 ``viz.safeexpr`` 的 AST 白名单。
2. **渲染在子进程里**。matplotlib 一 import 就要几十 MB，用完就退出，不往 AstrBot
   常驻内存里塞。
3. **输入全是结构化参数**。不给模型「写 Python 代码」的口子，只给「表达式 + 区间 +
   几何图元」，越界的写法直接被白名单拒掉并把原因回给用户。

工具：plot_function / plot_implicit / plot_parametric / plot_polar / draw_figure
指令：/画图 <expr>、/几何 <JSON>
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_DIR = Path(__file__).resolve().parent
MAX_ELEMENTS_JSON = 8000

# 什么样的回答值得整页排版：含公式，且够长（短问答做成大图反而别扭）
_MATH_HINT = re.compile(r"\$\$|(?<!\\)\$[^$\n]+\$|\\begin\{|\\frac|\\lim|\\int")


def _split_range(text: str, default: tuple[float, float]) -> list[float]:
    """把 "min,max" 解析成 [min, max]；空串用默认值。"""
    raw = (text or "").strip().replace("，", ",")
    if not raw:
        return [default[0], default[1]]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"区间需要写成 \"min,max\"，收到 {text!r}")
    return [float(parts[0]), float(parts[1])]


@register("astrbot_plugin_mathviz", "local", "数学绘图（本地受限）", "v1.0.0")
class MathVizStar(Star):
    """把数学表达式和几何示意图渲染成图片发送。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg: dict[str, Any] = dict(config or {})
        base = Path(StarTools.get_data_dir("astrbot_plugin_mathviz"))
        self.data_dir = base
        self.out_dir = base / "out"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mathjax_dir = Path(str(self.cfg.get("mathjax_dir") or (base / "mathjax")))
        self._mathjax_checked = False
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        logger.info("[mathviz] 就绪：输出目录 %s", self.out_dir)

    # ────────────────────────── 基础设施 ──────────────────────────

    def _cleanup(self) -> None:
        """按 TTL 清理旧图，顺手兜住磁盘。"""
        ttl = float(self.cfg.get("temp_ttl_seconds", 900))
        if ttl <= 0:
            return
        deadline = time.time() - ttl
        for path in self.out_dir.glob("viz_*.png"):
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                continue

    def _allow(self, user_id: str) -> bool:
        """滑动窗口限流，防止刷图撞 QQ 频控。"""
        limit = int(self.cfg.get("rate_limit_per_minute", 6))
        if limit <= 0:
            return True
        window = self._recent[user_id]
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    async def _render(self, spec: dict[str, Any]) -> tuple[str | None, str | None]:
        """在子进程里渲染，返回 (图片路径, 错误信息)。"""
        self._cleanup()
        stamp = f"{int(time.time() * 1000)}_{os.getpid()}"
        out_path = self.out_dir / f"viz_{stamp}.png"

        payload = dict(spec)
        payload.update(
            {
                "out": str(out_path),
                "dpi": int(self.cfg.get("dpi", 140)),
                "font_path": str(self.cfg.get("font_path") or ""),
                "line_width": float(self.cfg.get("line_width", 2.0)),
                "show_grid": bool(self.cfg.get("show_grid", True)),
            }
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "viz.worker",
                cwd=str(PLUGIN_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return None, f"渲染进程启动失败：{exc}"

        timeout = float(self.cfg.get("timeout_seconds", 25))
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload, ensure_ascii=False).encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, f"绘图超时（超过 {timeout:.0f} 秒）"

        text = (stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        if not text:
            tail = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
            return None, "渲染无输出" + (f"：{' / '.join(tail)}" if tail else "")

        try:
            result = json.loads(text[-1])
        except json.JSONDecodeError:
            return None, f"渲染输出无法解析：{text[-1][:120]}"

        if not result.get("ok"):
            return None, str(result.get("error") or "未知渲染错误")
        if not out_path.exists():
            return None, "渲染完成但文件不存在"

        logger.info(
            "[mathviz] %s %dx%d %sms",
            spec.get("kind"),
            result.get("w", 0),
            result.get("h", 0),
            result.get("ms", 0),
        )
        return str(out_path), None

    async def _deliver(self, event: AstrMessageEvent, spec: dict[str, Any], caption: str) -> str:
        """渲染并发送；失败把原因回给调用方（模型或用户）。"""
        user_id = str(event.get_sender_id() or "unknown")
        if not self._allow(user_id):
            return "绘图太频繁了，歇一分钟再画。"

        path, error = await self._render(spec)
        if error:
            return f"画不出来：{error}"
        await event.send(MessageChain(chain=[Plain(caption), Image.fromFileSystem(path)]))
        return "图已发出。"

    # ────────────────────────── 整页排版 ──────────────────────────

    async def _ensure_mathjax(self) -> None:
        """首次使用前准备 MathJax（可选依赖）。

        - 有 node + mathjax-full → 直接用；
        - 没有但允许自动安装且能找到 npm → 现场装一次（装在插件数据目录，不污染系统）；
        - 都不行 → 静默退回内置 Typst 数学引擎（渲染质量差一些，但不会拖垮整页）。
        """
        if self._mathjax_checked:
            return
        self._mathjax_checked = True

        from viz import mathjax as mj

        mj.configure(self.mathjax_dir)
        if mj.available():
            logger.info("[mathviz] MathJax 就绪：%s", self.mathjax_dir)
            return
        if not mj.node_path():
            logger.warning("[mathviz] 未找到 node，公式将使用内置 Typst 引擎（覆盖率较低）")
            return

        npm = shutil.which("npm")
        if not (npm and self.cfg.get("mathjax_auto_install", True)):
            logger.warning(
                "[mathviz] MathJax 未安装，公式使用内置 Typst 引擎。手动安装：\n"
                "  mkdir -p %s && cd %s && npm i mathjax-full",
                self.mathjax_dir,
                self.mathjax_dir,
            )
            return

        try:
            self.mathjax_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[mathviz] 正在安装 MathJax（首次，约 30~60 秒）…")
            proc = await asyncio.create_subprocess_exec(
                npm, "install", "mathjax-full", "--no-audit", "--no-fund",
                cwd=str(self.mathjax_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
            if proc.returncode == 0 and mj.available():
                logger.info("[mathviz] MathJax 安装完成")
            else:
                tail = (out or b"").decode("utf-8", "ignore").strip().splitlines()[-2:]
                logger.warning("[mathviz] MathJax 安装失败，退回内置引擎：%s", " / ".join(tail))
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("[mathviz] MathJax 安装异常，退回内置引擎：%s", exc)

    async def _render_sheet(self, markdown: str) -> tuple[str | None, str | None]:
        """把整段回答排成一页 PNG（Typst），返回 (路径, 错误)。"""
        await self._ensure_mathjax()
        self._cleanup()
        stamp = f"{int(time.time() * 1000)}_{os.getpid()}"
        out_path = self.out_dir / f"sheet_{stamp}.png"
        payload = {
            "markdown": markdown,
            "out": str(out_path),
            "width_pt": float(self.cfg.get("sheet_width_pt", 480)),
            "font_pt": float(self.cfg.get("sheet_font_pt", 10.5)),
            "ppi": int(self.cfg.get("sheet_ppi", 200)),
            "max_px": int(self.cfg.get("sheet_max_px", 5000)),
            "font_path": str(self.cfg.get("font_path") or ""),
            "mathjax_dir": str(self.mathjax_dir),
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "viz.sheet",
                cwd=str(PLUGIN_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return None, f"排版进程启动失败：{exc}"

        timeout = float(self.cfg.get("timeout_seconds", 25))
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload, ensure_ascii=False).encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, f"排版超时（>{timeout:.0f}s）"

        lines = (stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        if not lines:
            tail = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-2:]
            return None, "排版无输出" + (f"：{' / '.join(tail)}" if tail else "")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError:
            return None, "排版输出无法解析"
        if not result.get("ok"):
            return None, str(result.get("error") or "未知排版错误")
        if not out_path.exists():
            return None, "排版完成但文件不存在"
        if result.get("font_missing") and not getattr(self, "_font_warned", False):
            self._font_warned = True
            logger.warning(
                "[mathviz] 未找到中文字体，图里的中文会显示成方框。\n"
                "  Linux: sudo apt install fonts-noto-cjk\n"
                "  或在插件配置里把 font_path 指向任意中文字体文件（.ttf/.otf/.ttc）"
            )
        logger.info("[mathviz] sheet %sx%s %sms", result.get("w"), result.get("h"), result.get("ms"))
        return str(out_path), None

    @filter.on_decorating_result(priority=2000)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """整页排版：含公式的长回答 → 白底黑字一张大图。

        优先级 2000 高于 md_render（1000）：先整页出图，md_render 拿到只剩图片的
        消息链就什么都不做；splitter 的「含图不分段」补丁保证它也不再拆。
        """
        mode = str(self.cfg.get("sheet_mode", "auto")).lower()
        if mode == "off":
            return

        result = event.get_result()
        if result is None:
            return
        # 流式阶段已经逐字发过了，不要再整页重发（与 md_render 同一处理）
        if str(getattr(result, "result_content_type", "")).endswith("STREAMING_FINISH"):
            return

        chain = list(getattr(result, "chain", []) or [])
        if any(not isinstance(c, Plain) for c in chain):
            return  # 链里本来就有图片/文件，别动
        text = "".join(c.text for c in chain if isinstance(c, Plain))
        if not text.strip() or not _MATH_HINT.search(text):
            return
        min_chars = int(self.cfg.get("sheet_min_chars", 120))
        if mode != "always" and len(text) < min_chars:
            return

        path, error = await self._render_sheet(text)
        if error or not path:
            logger.warning("[mathviz] 整页排版失败，回退普通发送：%s", error)
            return

        image = Image.fromFileSystem(path)
        # 自己发，而不是只替换 chain：QQ 上传图片会偶发 `rich media transfer failed`
        # （retcode 1200），如果交给 RespondStage 发，失败了整条消息就没了。
        # 这里发两次，都失败就保留原文字链，让正常流程把文字发出去。
        for attempt in (1, 2):
            try:
                await event.send(MessageChain(chain=[image]))
            except Exception as exc:  # noqa: BLE001 - 发送失败要降级，不能让异常冒泡
                logger.warning("[mathviz] 整页图发送失败（第 %d/2 次）：%s", attempt, exc)
                if attempt == 2:
                    return
                await asyncio.sleep(1.5)
                continue
            result.chain = []  # 已经发出去了，别再发一遍（空链 RespondStage 会跳过）
            return

    # ────────────────────────── LLM 工具 ──────────────────────────

    @filter.llm_tool(name="plot_function")
    async def tool_plot_function(
        self,
        event: AstrMessageEvent,
        expressions: str,
        x_range: str = "-10,10",
        y_range: str = "",
        xlabel: str = "x",
        ylabel: str = "y",
        title: str = "",
    ) -> str:
        """绘制一元函数 y=f(x) 的图像。适合画曲线、比较多个函数、看单调性与极值。

        Args:
            expressions(string): 一个或多个函数表达式，多个用逗号分隔。支持 sin/cos/tan/exp/log/sqrt/abs/pi/e，幂用 ** 或 ^，例如 "sin(x),x**2/2"
            x_range(string): x 轴范围 "min,max"，例如 "-6,6"
            y_range(string): y 轴范围 "min,max"，留空则自动
            xlabel(string): x 轴标签，可写中文
            ylabel(string): y 轴标签，可写中文
            title(string): 图片标题，留空自动
        """
        try:
            exprs = [e.strip() for e in (expressions or "").replace("，", ",").split(",") if e.strip()]
            limit = int(self.cfg.get("max_expressions", 4))
            if len(exprs) > limit:
                return f"最多同时画 {limit} 条曲线。"
            spec: dict[str, Any] = {
                "kind": "function",
                "expressions": exprs,
                "x": _split_range(x_range, (-10.0, 10.0)),
                "xlabel": xlabel,
                "ylabel": ylabel,
                "title": title,
            }
            if (y_range or "").strip():
                spec["y"] = _split_range(y_range, (-10.0, 10.0))
        except ValueError as exc:
            return f"参数有误：{exc}"
        return await self._deliver(event, spec, f"y = {expressions}")

    @filter.llm_tool(name="plot_implicit")
    async def tool_plot_implicit(
        self,
        event: AstrMessageEvent,
        equation: str,
        x_range: str = "-5,5",
        y_range: str = "-5,5",
        title: str = "",
    ) -> str:
        """绘制隐式方程 F(x,y)=0 的图像，例如圆、椭圆、双曲线、一般曲线。

        Args:
            equation(string): 方程，可带等号。例如 "x**2+y**2=1"、"y**2-x**2/4=1"、"x**2+y**2-4"，幂用 ** 或 ^
            x_range(string): x 轴范围 "min,max"
            y_range(string): y 轴范围 "min,max"
            title(string): 图片标题，留空用方程本身
        """
        try:
            spec = {
                "kind": "implicit",
                "equation": equation,
                "x": _split_range(x_range, (-5.0, 5.0)),
                "y": _split_range(y_range, (-5.0, 5.0)),
                "title": title,
            }
        except ValueError as exc:
            return f"参数有误：{exc}"
        return await self._deliver(event, spec, f"方程 {equation}")

    @filter.llm_tool(name="plot_parametric")
    async def tool_plot_parametric(
        self,
        event: AstrMessageEvent,
        x_expr: str,
        y_expr: str,
        t_range: str = "0,6.2832",
        title: str = "",
    ) -> str:
        """绘制参数方程 x=x(t), y=y(t) 的轨迹，适合圆、摆线、心形线、抛体轨迹。

        Args:
            x_expr(string): x 关于 t 的表达式，例如 "cos(t)"
            y_expr(string): y 关于 t 的表达式，例如 "sin(t)"
            t_range(string): 参数范围 "min,max"，例如 "0,6.2832"
            title(string): 图片标题
        """
        try:
            spec = {
                "kind": "parametric",
                "x_expr": x_expr,
                "y_expr": y_expr,
                "t": _split_range(t_range, (0.0, 6.2832)),
                "title": title,
            }
        except ValueError as exc:
            return f"参数有误：{exc}"
        return await self._deliver(event, spec, f"参数方程 x={x_expr}, y={y_expr}")

    @filter.llm_tool(name="plot_polar")
    async def tool_plot_polar(
        self,
        event: AstrMessageEvent,
        r_expr: str,
        theta_range: str = "0,6.2832",
        title: str = "",
    ) -> str:
        """绘制极坐标曲线 r=f(θ)，适合玫瑰线、螺线、心形线。

        Args:
            r_expr(string): 半径关于 theta 的表达式，例如 "sin(3*theta)"
            theta_range(string): 角度范围（弧度）"min,max"
            title(string): 图片标题
        """
        try:
            spec = {
                "kind": "polar",
                "r_expr": r_expr,
                "theta": _split_range(theta_range, (0.0, 6.2832)),
                "title": title,
            }
        except ValueError as exc:
            return f"参数有误：{exc}"
        return await self._deliver(event, spec, f"极坐标 r={r_expr}")

    @filter.llm_tool(name="draw_figure")
    async def tool_draw_figure(
        self,
        event: AstrMessageEvent,
        elements_json: str,
        title: str = "",
    ) -> str:
        """绘制几何/物理示意图（点、线段、圆、圆弧、箭头、角度标记、多边形、文字）。

        用来画受力图、几何题配图、光路示意、矢量分解等。所有坐标用同一套平面直角坐标系。

        Args:
            elements_json(string): 元素数组的 JSON 字符串。每个元素必须带 type：
                {"type":"point","xy":[x,y],"label":"O"}                       点（可带标注）
                {"type":"segment","from":[x1,y1],"to":[x2,y2],"label":"R","dashed":false}  线段
                {"type":"arrow","from":[x1,y1],"to":[x2,y2],"label":"F"}      带箭头（力、速度）
                {"type":"circle","center":[cx,cy],"r":1,"label":""}          圆（r>0）
                {"type":"arc","center":[cx,cy],"r":1,"from_deg":0,"to_deg":90,"label":"θ"}  圆弧
                {"type":"angle","vertex":[x,y],"a":[x1,y1],"b":[x2,y2],"radius":0.4,"label":"θ","right_angle":false} 角度标记，a/b 是两条边上的任一点
                {"type":"polygon","points":[[x,y],...],"fill":false}          多边形（≥3 点）
                {"type":"text","xy":[x,y],"text":"中文标注"}                  纯文字
            title(string): 图片标题
        """
        raw = (elements_json or "").strip()
        if not raw:
            return "elements_json 不能为空。"
        if len(raw) > MAX_ELEMENTS_JSON:
            return f"图形描述过长（上限 {MAX_ELEMENTS_JSON} 字符），请简化。"
        try:
            elements = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"elements_json 不是合法 JSON：{exc.msg}"
        if not isinstance(elements, list):
            return "elements_json 需要是数组，例如 [{\"type\":\"point\",...}]"
        spec = {"kind": "geometry", "elements": elements, "title": title}
        return await self._deliver(event, spec, title or "示意图")

    # ────────────────────────── 指令 ──────────────────────────

    @filter.command("画图")
    async def cmd_plot(self, event: AstrMessageEvent, expression: str = ""):
        """手动画一元函数图像：/画图 sin(x)"""
        text = (expression or "").strip()
        if not text:
            yield event.plain_result("用法：/画图 sin(x)   或   /画图 sin(x),cos(x)")
            return
        spec = {
            "kind": "function",
            "expressions": [e.strip() for e in text.replace("，", ",").split(",") if e.strip()],
            "x": _split_range(str(self.cfg.get("default_x_range", "-10,10")), (-10.0, 10.0)),
        }
        user_id = str(event.get_sender_id() or "unknown")
        if not self._allow(user_id):
            yield event.plain_result("绘图太频繁了，歇一分钟再画。")
            return
        path, error = await self._render(spec)
        if error:
            yield event.plain_result(f"画不出来：{error}")
            return
        yield event.image_result(path)

    @filter.command("几何")
    async def cmd_geometry(self, event: AstrMessageEvent, elements_json: str = ""):
        """手动画示意图：/几何 [{"type":"circle","center":[0,0],"r":1}]"""
        raw = (elements_json or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：/几何 [{\"type\":\"circle\",\"center\":[0,0],\"r\":1}]\n"
                "元素类型：point / segment / arrow / circle / arc / angle / polygon / text"
            )
            return
        user_id = str(event.get_sender_id() or "unknown")
        if not self._allow(user_id):
            yield event.plain_result("绘图太频繁了，歇一分钟再画。")
            return
        try:
            elements = json.loads(raw)
        except json.JSONDecodeError as exc:
            yield event.plain_result(f"JSON 不合法：{exc.msg}")
            return
        path, error = await self._render({"kind": "geometry", "elements": elements})
        if error:
            yield event.plain_result(f"画不出来：{error}")
            return
        yield event.image_result(path)

    async def terminate(self) -> None:
        """插件卸载时清一遍临时图。"""
        for path in self.out_dir.glob("viz_*.png"):
            try:
                path.unlink()
            except OSError:
                pass
