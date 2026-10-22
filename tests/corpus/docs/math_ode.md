解分段的一阶方程：

**先看 $g(x)$ 的值域**

$$\varphi(x)=\begin{cases}e^{x-1},&x<1\ x-1,&x>1\end{cases}$$

它在 $x=1$ 处不连续，值域是 $(-\infty,+\infty)$。

- 解 $x<1$：$y'-y=e^{x-1}$，通解 $y_1=Ce^{x}+e^{x-1}$
- 解 $x>1$：$y'+y=e^{x-1}$，通解 $y_2=Ce^{-x}+\tfrac{1}{2}e^{x-1}$

在 $x=1$ 处接上：$\lim_{x\to1^-}y_1=\lim_{x\to1^+}y_2$，得 $C=\tfrac{1}{2}e$。
