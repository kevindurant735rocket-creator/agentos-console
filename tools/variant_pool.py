"""D72：变异体自证门的并发执行器（neg_a11y / neg_a11y_human / view_dod_selftest 共用）。

为什么需要它（2026-10-04 实测，不是推演）：
  给 15 道门挂墙钟后逐门读数，741s 里 492s 压在三道「变异体自证」门上 ——
  neg_a11y 199s / view_dod_selftest 149s / neg_a11y_human 144s。
  这三道的**单个变异体**都要起一次真 Chrome（真键盘事件 / 真 AX 树 / 真视口截图），
  本身不可再切；但**变异体之间**互不相干：各自持有隔离副本 HTML、各自 --out 报告、
  零写活的 index.html。串行只是没人把它们放一起。

为什么敢放一起（并发安全的真实依据，不是「以前并发 flake 过所以算了」）：
  - D39 事故的根因是 neg_a11y **就地改活的 index.html** 注入缺陷，别的门在同一时刻
    读到了缺陷版。自 v122 起三个门都只改 tempdir 里的副本，这条根因已经没有了。
  - 每个变异体的产物路径互不相同（iso-<tag>.html / neg-report-<tag>-<try>.json /
    shots-<tag> / out-<tag>.json），没有共享写目标。
  - 10 核 / 16GB 上实测 4 路并发 a11y_walk：4 份全绿，墙钟 27.3s（串行 4×23=92s）。
    即并发不引入 flake，且把吞吐提到 3.4 倍。

回退：AGENTOS_VARIANT_WORKERS=1（或 0/非法）立刻退回原串行逐个跑，行为与改前一致。
调大上限封在 8 —— 再多就是自己跟自己抢 Chrome，墙钟反而回升。

输出纪律：worker 线程**不许 print**（会互相插行，破坏「一变异体一块」的读法）。
每个 case 返回自己的行块，本模块按 cases 的**原始顺序**展平后再由主线程打印，
所以两次运行的输出逐字节可比。

默认并发度为什么是 6（2026-10-04 实测，4 组配对 + 独立内存实测，非推演）：
  neg_a11y.py（9 变异体）配对跑，墙钟 = 4:40/42/41/41/46s（中位 41）
                              6:31/32/33/34/38/47s（中位 33）-> -20%
                              8:28/29/31/33s + 一次 65s 离群（中位 31）-> 再省约 6%，不稳
  内存（进程内实测：同开 N 个 chromium，扫 ms-playwright 的 chrome-headless-shell RSS 峰值）
    N=4 924MB / N=6 2359MB / N=8 3147MB。
  取 6：-20% 墙钟换 2.4GB，8 只多换 6% 却多 0.8GB 且已出现 65s 离群，不划算。
  W=6 共 8 次独立跑全部 RC=0，零 flake（其中一次 cmp 证明输出逐字节可比）。

为什么 act_gate 的 26 道子门**不**进这个池（2026-10-04 实测判定，放弃约 40s 收益）：
  那 26 道子门共享**同一个 serve 进程 / 同一个端口 / 同一批可变状态**
  （promote 晋级、feedback 禁用态等）。并发化会让一道子门看到另一道还没落地的状态，
  正是「同一设计面并行写」禁止的那一类 —— 并行会毁掉门的语义，而不只是引入 flake。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor

_HARD_MAX = 8
_DEFAULT = 6


def workers():
    """本次并发度。非法值/非正数 -> 1（等价于关掉并发，退回串行）。"""
    raw = os.environ.get("AGENTOS_VARIANT_WORKERS", str(_DEFAULT))
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(_HARD_MAX, n))


def map_cases(cases, fn, header=None):
    """并发跑 fn(i, case) -> list[str]，返回按 cases 顺序展平的输出行。

    header: 可选，(i, case) -> 可读短名，用于给每块末尾的时间行做标签。
    多数门直接用它输出序号（中文 case 名按空格切出来的片段没区分度）。
    每块以 'TIME: variant <tag> <n>s' 收尾；这个形状刻意不与
    '== 名 (py=' / 'RC=' 撞车（引擎 D50 靠那两种形状认门和裁决）。
    """
    items = list(enumerate(cases))
    n = min(workers(), len(items)) or 1
    blocks = [None] * len(items)
    t_all = time.time()
    if n == 1:
        for i, case in items:
            blocks[i] = _run_one(i, case, fn, header)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = {ex.submit(_run_one, i, c, fn, header): i for i, c in items}
            for fut, i in futs.items():
                blocks[i] = fut.result()
    lines = []
    for b in blocks:
        lines.extend(b)
    lines.append("TIME: variants %d 个 / 并发度 %d / 墙钟 %.1fs"
                 % (len(items), n, time.time() - t_all))
    return lines


def _run_one(i, case, fn, header):
    t0 = time.time()
    out = list(fn(i, case))
    tag = header(i, case) if header else "case%d" % i
    out.append("TIME: variant %s %.1fs" % (tag, time.time() - t0))
    return out


if __name__ == "__main__":
    import sys

    os.environ["AGENTOS_VARIANT_WORKERS"] = "1"
    assert workers() == 1, workers()
    os.environ["AGENTOS_VARIANT_WORKERS"] = "999"
    assert workers() == _HARD_MAX, workers()
    os.environ["AGENTOS_VARIANT_WORKERS"] = "abc"
    assert workers() == 1, workers()
    os.environ.pop("AGENTOS_VARIANT_WORKERS")
    assert workers() == _DEFAULT, workers()
    got = map_cases(["a", "b", "c", "d"],
                    lambda i, c: ["blk-%s" % c], header=lambda i, c: "c%d" % i)
    assert [l for l in got if l.startswith("blk-")] == \
        ["blk-a", "blk-b", "blk-c", "blk-d"], got
    assert sum(1 for l in got if l.startswith("TIME: variant ")) == 4, got
    print("VARIANT_POOL-OK 5 assertions / 4 路并发实测全绿 / 回退 workers=1")
    sys.exit(0)
