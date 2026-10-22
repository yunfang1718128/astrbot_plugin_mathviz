# 跑分语料

```bash
python tests/run.py --report                                  # 用默认 MathJax 目录
python tests/run.py --mathjax-dir /path/to/mathjax --report   # 指定目录
```

## 判据

- **公式**：`expect: ok` 必须排版成功；`expect: merror` 表示"已知不支持"，
  必须渲染成 MathJax 的红字错误（用 `fill="red"` 识别）。
  如果某条从 `merror` 变成 `ok`，说明兼容面变了，值得看一眼（通常是好消息）。
- **整页**：每个 `.md` 必须出图成功，并统计 `degraded`（降级成原文的公式数）。
- 退出码 0 = 全部符合预期。

## 目录

```
corpus/
├── formulas.jsonl   单公式，含历史缺陷回归（hinge：粘连 / \hbar / 中文 / aligned / cases）
└── docs/*.md         整页排版用例
```

## 当前基线（2026-10-23）

```
formulas  25/25 passed
pages     2/2 ok    avg 235ms    max 247ms    degraded 0
```

已知不支持：`\si{}{}`（siunitx）。化学式 `\ce{}` 可用（MathJax 的 AllPackages 内含 mhchem）。
