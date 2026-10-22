// MathJax v3 → 自包含 SVG（字体路径内嵌在 <defs>，不依赖系统字体）
//
// 用法：node tex2svg.js <mathjax目录>  < formulas.json  > svgs.json
//   <mathjax目录> 里面应有 node_modules/mathjax-full（由插件自动或手动安装）
//
// 注意：**由 Node 侧直接取 <svg> 元素本身**并返回它的宽高，
// 不要让调用方用正则去抠——正则会在某些结构上截断标签，
// Typst 会报 "root node was opened but never closed"。
const fs = require('fs');
const path = require('path');

const base = process.argv[2] || __dirname;
const MJ = path.join(base, 'node_modules/mathjax-full/js/');

const { mathjax } = require(MJ + 'mathjax.js');
const { TeX } = require(MJ + 'input/tex.js');
const { SVG } = require(MJ + 'output/svg.js');
const { liteAdaptor } = require(MJ + 'adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require(MJ + 'handlers/html.js');
const { AllPackages } = require(MJ + 'input/tex/AllPackages.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);

const tex = new TeX({ packages: AllPackages });
const svg = new SVG({ fontCache: 'local' });
const doc = mathjax.document('', { InputJax: tex, OutputJax: svg });

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const started = Date.now();
const results = input.map((item) => {
  const source = typeof item === 'string' ? item : item.tex;
  const display = typeof item === 'string' ? true : item.display !== false;
  try {
    const node = doc.convert(source, { display, em: 16, ex: 8 });
    const kids = adaptor.childNodes(node);
    const svgEl = kids.find((k) => adaptor.kind(k) === 'svg');
    if (!svgEl) return { ok: false, error: 'no <svg> element' };
    return {
      ok: true,
      svg: adaptor.outerHTML(svgEl),
      width: adaptor.getAttribute(svgEl, 'width') || '',
      height: adaptor.getAttribute(svgEl, 'height') || '',
      style: adaptor.getAttribute(svgEl, 'style') || '',
    };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err).slice(0, 200) };
  }
});
process.stdout.write(JSON.stringify({ results, ms: Date.now() - started }));
