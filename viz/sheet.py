"""整页排版：把一条回答（Markdown + LaTeX）排成**一张**白底黑字长图。

为什么用 Typst 而不是浏览器：Typst 是一个 Rust 编译的排版引擎，本仓库已经因为
md_render 装好了（`typst` + `tylax`），不需要 Chromium、不需要 JS。
流程是：Markdown → Typst 源码（公式用 tylax 从 LaTeX 转过来）→ PNG。

为什么单独起进程：和 worker 一样，编译器和字体解析都会占内存，用完就退。

调用方式（stdin 一行 JSON，stdout 一行 JSON）：

    echo '{"markdown":"...","out":"/tmp/a.png"}' | python -m viz.sheet
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
from pathlib import Path

import typst

try:  # 内置 Typst 数学引擎（可选；缺了只影响 MathJax 失败时的兜底）
    import tylax
except ImportError:  # pragma: no cover - 取决于安装环境
    tylax = None

# 中文字体候选（跨平台）。配置项 font_path 非空时优先用它。
FONT_CANDIDATES = (
    # Linux（fonts-noto-cjk / 文泉驿）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
EXTRA_FONT_DIRS = (
    "/usr/share/fonts/opentype/noto/",
    "/usr/share/fonts/truetype/",
    "C:/Windows/Fonts/",
    "/System/Library/Fonts/",
)

MAX_MARKDOWN_CHARS = 12000
DEFAULT_WIDTH_PT = 480  # ≈ 640px，手机上看一行 30 来个字，不挤

# LaTeX → Typst 转换的已知缺口（与 md_render 的处理保持一致）
_TEXT_ESCAPES = (("\\&", "\\text{\\&}"), ("\\_", "\\text{\\_}"))
_MATHSCR = re.compile(r"\\mathscr\s*\{([^{}]*)\}")

DOC_TEMPLATE = """#set page(width: {width}pt, height: auto, margin: (x: 22pt, y: 20pt), fill: white)
#set text(font: ("Noto Sans CJK SC", "DejaVu Sans"), size: {size}pt, lang: "zh", fill: rgb("#111111"))
#set par(leading: 0.75em, spacing: 1.0em, justify: false)
#set list(indent: 1.1em, spacing: 0.55em)
#set enum(indent: 1.1em, spacing: 0.55em)
{body}
"""

_BLOCK_MATH = re.compile(r"\$\$(?s:(.*?))\$\$")
_INLINE_MATH = re.compile(r"(?<!\\)\$([^$\n]+?)\$")
_TYPST_SPECIAL = re.compile(r"([#*_@<>\\`~\[\]$])")


class SheetError(ValueError):
    """排版失败，消息回给调用方并回退到普通发送。"""


_ENV_RE = re.compile(r"\\begin\{(aligned|align\*?|gathered|cases|pmatrix|bmatrix|vmatrix|matrix)\}(?s:(.*?))\\end\{\1\}")


def _convert_rows(body: str) -> list[str]:
    """按 ``\\`` 切行，逐行调 tylax。

    为什么不用 tylax 整体转：实测它对多行环境的行分隔符**时有时无**
    （``a \\\\ b`` 这种会被并成一行），靠不住。拆开转再拼就完全可控。
    """
    rows = re.split(r"\\\\", body)
    out: list[str] = []
    for row in rows:
        piece = tylax.latex_to_typst(row.strip()).strip()
        if piece:
            out.append(piece)
    return out


def _env_to_math(match: re.Match) -> str:
    """把多行 LaTeX 环境翻成 Typst 数学。

    只有 aligned / align / gathered 需要自己拆行（tylax 会丢掉它们的行分隔符）；
    cases 与各种矩阵交给 tylax 整体转——它本来就能输出正确的 `cases(a & b, c & d)`
    和 `mat(...)`，我们插手反而会把行结构拆坏（踩过）。
    """
    env, body = match.group(1), match.group(2)
    if env.startswith("align") or env == "gathered":
        rows = _convert_rows(body)
        if not rows:
            return ""
        # 用 mat() 承载多行：Typst 的 `&` 对齐在块级公式里实测不生效（各行仍居中），
        # 而 mat 的 `&` 是真列对齐（实测两到四行等号严格同列）。
        # 只当**每一行**都含 `&` 时才用 mat，否则退化成普通换行。
        if len(rows) > 1 and all("&" in row for row in rows):
            return "mat(delim: #none, " + " ; ".join(row.strip() for row in rows) + ")"
        return " \\ ".join(rows)
    return _typst_post_fix(tylax.latex_to_typst(match.group(0)).strip())


def _typst_post_fix(body: str) -> str:
    """修 tylax 产物的已知坏点。

    - `\\hbar` → `planck.reduce`：Typst 没有这个符号修饰符，直接用会报
      `unknown symbol modifier`，整页排版会因此失败（物理题里很常见）。
    - 其余与 md_render 的口径保持一致，免得同一段公式在两个引擎下不一样。
    """
    body = body.replace("planck.reduce", "planck")
    # `\frac{n}{\beta}I` 这类会被 tylax 粘成 `n/betaI`（β 与 I 变成一个标识符，
    # Typst 当变量名报错，即便自愈拆开也会变成 ηI）。在「已知符号名 + 字母」之间补空格。
    # 也叫待长优先：`varepsilon` 先于 `eta` 匹配，不会把 θ 拆成 `the ta`。
    names = "|".join(sorted(_TYPST_SYMBOLS, key=len, reverse=True))
    body = re.sub(rf"(?<![A-Za-z])({names})(?=[A-Za-z])", r"\1 ", body)
    return body


def latex_to_math(tex: str) -> str:
    """LaTeX 公式 → Typst 数学源码。

    Raises:
        ValueError: 转换后为空（tylax 认不出来）。
    """
    text = tex.strip()
    if not text:
        raise ValueError("空公式")
    text = re.sub(r"}(?=\\)", "} ", text)
    # `\frac{n}{\beta}I` 会被 tylax 粘成 `n/betaI`：在 `}` 与后续字母之间补一个薄空格
    # （LaTeX 里 `\,` 就是薄空格，语义不变），实测能避免粘连。
    text = re.sub(r"}(?=[A-Za-z])", r"}\\,", text)
    for src, dst in _TEXT_ESCAPES:
        text = text.replace(src, dst)
    text = _MATHSCR.sub(r"\\mathcal{\1}", text)

    # 多行环境自己拆行（tylax 对 \\ 的处理不可靠）；环境**外**的 LaTeX 仍交给 tylax，
    # 两边拼起来。不能像早先那样“整段直接返回”，否则环境外的 \varphi 这类命令会漏转。
    if _ENV_RE.search(text):
        parts: list[str] = []
        pos = 0
        for match in _ENV_RE.finditer(text):
            head = text[pos:match.start()].strip()
            if head:
                head_typ = tylax.latex_to_typst(head).strip()
                if head_typ:
                    parts.append(head_typ)
            env_typ = _env_to_math(match)
            if env_typ:
                parts.append(env_typ)
            pos = match.end()
        tail = text[pos:].strip()
        if tail:
            tail_typ = tylax.latex_to_typst(tail).strip()
            if tail_typ:
                parts.append(tail_typ)
        body = " ".join(parts).strip()
        if body:
            return _typst_post_fix(body)

    body = _typst_post_fix(tylax.latex_to_typst(text).strip())
    if not body:
        raise ValueError("公式转换后为空")
    # 注意：**不要**把 tylax 输出的 ` \ `（反斜杠+空格）改写成「反斜杠+换行」。
    # Typst 里 `\ ` 就是数学换行符；改成反斜杠直接换行反而被当成转义空格，
    # 两行会并成一行、再靠自动折行「假换行」，`&` 对齐点也就失效了（踩过）。
    return body


def escape_text(text: str) -> str:
    """转义 Typst 标记字符（避免正文里的 # * _ 被当成语法）。"""
    return _TYPST_SPECIAL.sub(r"\\\1", text)


from viz import mathjax


def inline_markup(text: str, spans: list[tuple[str, str]] | None = None,
                  assets: list[dict] | None = None) -> str:
    """处理行内 **粗体** / `代码` / $公式$，其余按纯文本转义。

    粗体里的内容会递归处理，所以 `**先看 $g(x)$ 的值域**` 这种写法也能把公式转对。
    `spans` 用来收集（渲染标记, 原始 LaTeX），编译失败时好定位并降级那一条。
    """
    out: list[str] = []
    pos = 0
    pattern = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|(?<!\\)\$([^$\n]+?)\$")
    for match in pattern.finditer(text):
        out.append(escape_text(text[pos:match.start()]))
        if match.group(1) is not None:
            out.append("*" + inline_markup(match.group(1), spans) + "*")
        elif match.group(2) is not None:
            code = match.group(2).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'#raw("{code}")')
        else:
            try:
                if assets is not None:
                    assets.append({"latex": match.group(3), "display": False})
                    out.append("\x00MJA%d\x00" % (len(assets) - 1))
                else:
                    src = latex_to_math(match.group(3))
                    out.append("$" + src + "$")
                    if spans is not None:
                        spans.append(("$" + src + "$", match.group(3)))
            except Exception:
                out.append(escape_text(match.group(0)))
        pos = match.end()
    out.append(escape_text(text[pos:]))
    return "".join(out)


def _block_math(match: re.Match) -> str:
    """块级公式：$$...$$ → 独立成段的 Typst 公式。"""
    try:
        return "\n\n$ " + latex_to_math(match.group(1)) + " $\n\n"
    except Exception:
        return "\n\n" + escape_text(match.group(0)[:200]) + "\n\n"


_TO_BLOCK = "\x00BLOCKMATH{}\x00"
_FROM_BLOCK = re.compile(r"\x00BLOCKMATH(\d+)\x00")


def markdown_to_typst(md: str, spans: list[tuple[str, str]] | None = None,
                      assets: list[dict] | None = None) -> str:
    """把模型回答转成 Typst 正文。只认常见语法，认不出的原样当文本。"""
    if len(md) > MAX_MARKDOWN_CHARS:
        raise SheetError(f"内容过长（{len(md)} 字 > {MAX_MARKDOWN_CHARS}），改走普通发送")

    text = md.replace("\r\n", "\n")

    # ① 先把块级公式挖出来换成占位符。不能直接放回 `$ ... $`：
    #    后面的行内规则会把 Typst 源码当成 LaTeX 再转一遍（踩过）。
    blocks: list[str] = []

    def _stash(match: re.Match) -> str:
        raw = match.group(1)
        if assets is not None:
            assets.append({"latex": raw, "display": True})
            return "\n\n\x00MJA%d\x00\n\n" % (len(assets) - 1)
        try:
            tex = latex_to_math(raw)
        except Exception:
            tex = ""
        if not tex:
            return match.group(0)
        blocks.append(tex)
        if spans is not None:
            spans.append(("$ " + tex + " $", raw))
        return "\n\n" + _TO_BLOCK.format(len(blocks) - 1) + "\n\n"

    text = _BLOCK_MATH.sub(_stash, text)

    # ② 逐行处理
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            out.append("#line(length: 100%, stroke: 0.5pt + luma(200))")
            continue
        if _FROM_BLOCK.fullmatch(stripped):
            out.append(stripped)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            out.append("*" + inline_markup(heading.group(2), spans, assets) + "*")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            out.append("- " + inline_markup(bullet.group(1), spans, assets))
            continue
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ordered:
            out.append("+ " + inline_markup(ordered.group(1), spans, assets))
            continue
        if stripped.startswith(">"):
            out.append("#quote[" + inline_markup(stripped.lstrip("> "), spans, assets) + "]")
            continue
        out.append(inline_markup(stripped, spans, assets))

    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

    # ③ 占位符换回真正的公式
    body = _FROM_BLOCK.sub(lambda m: "$ " + blocks[int(m.group(1))] + " $", body)

    if not body:
        raise SheetError("正文为空")
    return body


def detect_font(extra_font: str = "") -> str:
    """返回可用的中文字体文件路径；找不到返回空串。"""
    for candidate in ([extra_font] if extra_font else []) + list(FONT_CANDIDATES):
        try:
            if candidate and Path(candidate).exists():
                return candidate
        except OSError:
            continue
    return ""


def _font_setup(extra_font: str = "") -> list[str]:
    """返回 typst.compile 需要的字体路径列表（配置项优先，其次自动探测）。"""
    paths: list[str] = []
    for candidate in ([extra_font] if extra_font else []) + list(FONT_CANDIDATES):
        try:
            if candidate and Path(candidate).exists():
                paths.append(candidate)
                break
        except OSError:
            continue
    paths.extend(d for d in EXTRA_FONT_DIRS if Path(d).exists())
    return paths


def _formula_compiles(math_src: str, font_paths: list[str]) -> bool:
    """单条公式能不能编译（用于整页失败后定位坏公式）。"""
    doc = (
        "#set page(width: 300pt, height: auto, margin: 4pt, fill: white)\n"
        "#set text(size: 10pt)\n"
        f"$ {math_src} $\n"
    )
    try:
        typst.compile(doc.encode(), format="png", ppi=100, font_paths=font_paths)
        return True
    except Exception:
        return False


# Typst 里的希腊字母名：用来把 tylax 粘连的标识符拆回去（betaI → beta I）
_TYPST_SYMBOLS = (
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta",
    "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi",
    "rho", "varrho", "sigma", "varsigma", "tau", "upsilon", "phi", "varphi",
    "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi",
    "Sigma", "Upsilon", "Phi", "Psi", "Omega",
)

_UNKNOWN_VAR = re.compile(r"unknown variable: (\w+)")
_TEXT_IN_MATH = re.compile(r"#text\[([^\[\]]*)\]")


def _compile_error(source: str, font_paths: list[str]) -> str | None:
    """编译一次，返回错误文本；通过则返回 None。"""
    doc = (
        "#set page(width: 300pt, height: auto, margin: 4pt, fill: white)\n"
        '#set text(font: ("Noto Sans CJK SC", "DejaVu Sans"), size: 10pt, lang: "zh")\n'
        f"$ {source} $\n"
    )
    try:
        typst.compile(doc.encode(), format="png", ppi=100, font_paths=font_paths)
        return None
    except Exception as exc:  # noqa: BLE001 - 只为拿错误文本
        return str(exc)


def _split_glued(name: str) -> str | None:
    """把 `betaI` 这种粘连标识符拆成「符号 + 空格 + 剩余」；拆不动返回 None。"""
    for symbol in sorted(_TYPST_SYMBOLS, key=len, reverse=True):
        if name.startswith(symbol) and len(name) > len(symbol):
            rest = name[len(symbol):]
            if rest.isalpha():
                tail = rest if len(rest) == 1 else f'"{rest}"'
                return f"{symbol} {tail}"
    return None


def _fix_unknown(name: str) -> str | None:
    """给定 Typst 报的未知标识符，返回替换文本（保证能前进）。

    1. 希腊字母粘连（`betaI`、`etaI`）→ 拆成「符号 + 剩余」；
    2. 已知遗留的空白词（tylax 把 `\\qquad` 转成 `wide` 之类）→ 换空格；
    3. 其余实在认不出的 → 包成直立文本，至少整页还能出来。
    """
    glued = _split_glued(name)
    if glued:
        return glued
    if name in {"wide", "med", "thick"}:
        return "quad" if name != "med" else "thin"
    if len(name) <= 24:
        return f'upright("{name}")'
    return None


def _repair_math(source: str, font_paths: list[str], attempts: int = 6) -> str | None:
    """自愈：按 Typst 报错的名字逐个修，修不好返回 None。

    实测物理/量子公式有两类高频毛病：
    - `\\beta I`、`\\mu B` 相邻符号被 tylax 粘成 `betaI`，Typst 当变量名报错；
    - `#text[中文]` 在 mat() 环境不合法，要换成引号文本。
    """
    current = _TEXT_IN_MATH.sub(r'"\1"', source)
    for _ in range(attempts):
        error = _compile_error(current, font_paths)
        if error is None:
            return current
        match = _UNKNOWN_VAR.search(error)
        if not match:
            return None
        name = match.group(1)
        fixed = _fix_unknown(name)
        if not fixed:
            return None
        current = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", fixed, current
        )
    return current if _compile_error(current, font_paths) is None else None


def _degrade_broken(
    body: str, spans: list[tuple[str, str]], font_paths: list[str]
) -> tuple[str, int, int]:
    """整页编译失败后的兼底：先自愈，自愈不了才降级成原文。

    为什么需要：一条坏公式会让**整页**编译失败，整条回答于是退回「文字 + 一堆小图」。
    物理/量子题里这种符号很多，所以分两级：
    1. 自愈（拆粘连标识符、中文转引号）——修好的仍正常排版；
    2. 真修不好（如 `\\zzzqqq{x}`）才改成等宽原文，至少整页还在。

    Returns:
        (新 body, 降级条数, 自愈条数)
    """
    degraded = 0
    repaired = 0
    for marker, latex in spans:
        if marker not in body:
            continue
        src = marker[1:-1].strip()
        if _formula_compiles(src, font_paths):
            continue
        fixed = _repair_math(src, font_paths)
        if fixed is not None:
            body = body.replace(marker, "$ " + fixed + " $", 1)
            repaired += 1
            continue
        raw = latex.replace("\\", "\\\\").replace('"', '\\"')
        body = body.replace(marker, f'#raw("{raw}")', 1)
        degraded += 1
    return body, degraded, repaired


_MJ_PLACEHOLDER = re.compile(r"\x00MJA(\d+)\x00")


def _substitute_assets(body: str, assets: list[dict], assets_dir: Path) -> tuple[str, int, int]:
    """把公式占位符换成 Typst 的 #image()；MathJax 失败的退回旧引擎。

    Returns:
        (新 body, MathJax 成功条数, 回退旧引擎条数)
    """
    if not assets:
        return body, 0, 0
    try:
        results = mathjax.render_batch(assets)
    except mathjax.MathJaxError:
        results = [{"ok": False, "error": "mathjax unavailable"}] * len(assets)

    assets_dir.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for index, (asset, result) in enumerate(zip(assets, results)):
        marker = "\x00MJA%d\x00" % index
        if marker not in body:
            continue
        if result.get("ok"):
            path = assets_dir / f"f{index}.svg"
            path.write_text(result["svg"], encoding="utf-8")
            # Typst 用「相对 root」的路径，避免要求能读文件系统根目录
            rel = f"{assets_dir.name}/f{index}.svg"
            width = result["width_pt"]
            baseline = result["baseline_pt"]
            if asset["display"]:
                repl = f'\n\n#align(center)[#image("{rel}", width: {width}pt)]\n\n'
            else:
                repl = f'#box(baseline: {baseline}pt)[#image("{rel}", width: {width}pt)]'
            ok += 1
        else:
            try:
                tex = latex_to_math(asset["latex"])
                repl = ("$ " + tex + " $") if asset["display"] else ("$" + tex + "$")
            except Exception:
                repl = escape_text(asset["latex"][:200])
            fail += 1
        body = body.replace(marker, repl)
    return body, ok, fail


def render_sheet(
    markdown: str,
    out_path: str,
    *,
    width_pt: float = DEFAULT_WIDTH_PT,
    font_pt: float = 10.5,
    ppi: int = 200,
    max_px: int = 5000,
    max_bytes: int = 1_200_000,
    font_path: str = "",
    mathjax_dir: str = "",
) -> dict:
    """把 Markdown 排成一张 PNG，返回尺寸信息。"""
    spans: list[tuple[str, str]] = []
    assets: list[dict] = []
    body = markdown_to_typst(markdown, spans=spans, assets=assets)
    mathjax.configure(mathjax_dir or None)
    font_file = detect_font(font_path)
    font_paths = _font_setup(font_path)

    # 公式交给 MathJax 出 SVG，再用 Typst 的 #image() 嵌入
    body, mj_ok, mj_fail = _substitute_assets(body, assets, Path(out_path).parent / "fassets")

    def _source(text: str) -> bytes:
        return DOC_TEMPLATE.format(width=int(width_pt), size=font_pt, body=text).encode()

    started = time.time()
    degraded = 0
    repaired = 0
    try:
        png = typst.compile(_source(body), format="png", ppi=ppi, font_paths=font_paths, root=str(Path(out_path).resolve().parent))
    except Exception:
        # 整页挂了：先自愈（拆粘连标识符/中文转引号），修不好才降级那几条
        body, degraded, repaired = _degrade_broken(body, spans, font_paths)
        if degraded == 0 and repaired == 0:
            raise
        png = typst.compile(_source(body), format="png", ppi=ppi, font_paths=font_paths, root=str(Path(out_path).resolve().parent))

    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        width, height = image.size
        shrunk = 0
        # 超长/超大：按比例降 PPI 重排。
        # 除了像素上限，还要卡**体积**——实测 QQ 会报 `rich media transfer failed`
        # （retcode 1200），图发不出去。
        while (max(width, height) > max_px or len(png) > max_bytes) and ppi > 80:
            if max(width, height) > max_px:
                ppi = max(80, int(ppi * max_px / max(width, height)))
            else:
                ppi = max(80, int(ppi * math.sqrt(max_bytes / len(png))))
            png = typst.compile(_source(body), format="png", ppi=ppi, font_paths=font_paths, root=str(Path(out_path).resolve().parent))
            with Image.open(BytesIO(png)) as again:
                width, height = again.size
            shrunk += 1
        with open(out_path, "wb") as fh:
            fh.write(png)

    return {
        "ok": True,
        "out": out_path,
        "w": width,
        "h": height,
        "ppi": ppi,
        "shrunk": shrunk,
        "degraded": degraded,
        "repaired": repaired,
        "font_missing": not font_file,
        "mathjax_ok": mj_ok,
        "mathjax_fail": mj_fail,
        "bytes": len(png),
        "ms": int((time.time() - started) * 1000),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"规格不是合法 JSON：{exc}"}, ensure_ascii=False))
        return 2

    markdown = str(payload.get("markdown") or "")
    out = str(payload.get("out") or "")
    if not markdown or not out:
        print(json.dumps({"ok": False, "error": "缺少 markdown 或 out"}, ensure_ascii=False))
        return 2

    try:
        result = render_sheet(
            markdown,
            out,
            width_pt=float(payload.get("width_pt") or DEFAULT_WIDTH_PT),
            font_pt=float(payload.get("font_pt") or 10.5),
            ppi=int(payload.get("ppi") or 200),
            max_px=int(payload.get("max_px") or 5000),
            max_bytes=int(payload.get("max_bytes") or 1_200_000),
            font_path=str(payload.get("font_path") or ""),
            mathjax_dir=str(payload.get("mathjax_dir") or ""),
        )
    except SheetError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001 - 任何异常都回一句话，别把 traceback 发给用户
        print(json.dumps({"ok": False, "error": f"排版失败：{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
