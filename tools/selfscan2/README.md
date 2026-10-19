# selfscan2 — 多候选隔离试跑 + 可复算打分 + fail-closed 择优

> derived from [vstorm-co/pydantic-deepagents](https://github.com/vstorm-co/pydantic-deepagents)（★1103, MIT, Python）
> 借鉴其 **Live Run Forking**：分支隔离（copy-on-write 覆盖层 + 独立 budget）、可插拔接受模式
> （`manual` / `auto` / `auto_with_fallback` / `vote`）、以及可复算的加权 confidence
> （`quality_spread·0.4 + test_pass_ratio·0.4 + internal_consistency·0.2`，测试项只认真实 exit code）。

本工具把这三样搬成一个纯 stdlib 的小量具：**给多个候选目录各跑一遍同一组验证命令，
按真实 rc 打分，并按可插拔的接受模式决定采纳哪一个；拿不准就明说 NO-WINNER。**

## 为什么需要它

本仓做「多方案试错」时，直接在原目录跑验证命令有三个坑：

1. **候选互相污染** —— A 候选写出的文件被 B 候选的验证命令读到，结论就不可比；
2. **试跑无预算** —— 一条卡死的命令能把整轮评估挂住；
3. **择优靠感觉** —— 没有可复算的判据，容易「看起来差不多就挑一个」，
   这与本仓「不装成功」的铁律相冲突。

## 三道诚实地线

| 诱因 | 本工具的做法 |
| --- | --- |
| 隔离失效 | 候选先复制到临时副本再跑，原树只读；跑完用 sha256 对账，变了直接判红（rc=2） |
| 试跑挂死 | 每条命令独立超时，超时记 `rc=124` 并在明细里写明 timeout，**不算通过** |
| 拿不准硬选 | 默认档 `auto_with_fallback` 要求「分数过线 + 领先次名够多」，否则 NO-WINNER（rc=2）；`manual` 永不自选（rc=4，winner 为空） |

## 打分公式

```
score = 0.4·pass_ratio + 0.4·lead + 0.2·agreement

pass_ratio = 通过命令数 / 命令总数                       （只看真实 rc）
lead       = clamp01(0.5 + (本候选 pass_ratio − 其余候选均值))
agreement  = 同一命令上与多数派 rc 一致的比例（单候选时 = 1.0）
```

权重是模块级常量 `W_PASS / W_LEAD / W_AGREE`，没有任何一项来自模型自述；
`--json` 会落盘每个候选、每条命令的原始 rc 与输出尾行，便于逐条复算。

## 用法

```bash
# 交互式：三个候选目录，跑同一组验证命令
python3 selfscan2.py \
  --cand ../cand-a ../cand-b ../cand-c \
  --cmd "python3 -m py_compile x.py" \
  --cmd "python3 -m pytest -q" \
  --accept auto_with_fallback --json out.json

# 只给打分，人来拍板
python3 selfscan2.py --cand ../a ../b --cmd "make check" --accept manual

# 变异体验牙（红→绿）
python3 selfscan2.py --selftest
```

## 裁决 rc

| rc | 含义 |
| --- | --- |
| 0 | 采纳了某个候选（winner 非空，明细里看得到领先幅度） |
| 2 | NO-WINNER：并列、分数不够、或隔离失效 —— 不装成功 |
| 3 | 用法/输入错误（候选缺失、命令为空） |
| 4 | `manual` 档：打分已给出，等人工指定 winner |

## 自检输出（本仓库实测）

```
$ python3 selfscan2.py --selftest
PASS 隔离：原树 probe.py sha256 未变（写只落副本）  -- sha 前后一致
PASS 隔离：副本里确实落了 out.txt（命令真跑了）  -- good 副本有 out.txt
PASS 打分：good 排第一且 > half > bad  -- good=0.9000 half=0.6000 bad=0.1000
PASS 默认档：分差够大时采纳 good  -- winner=good why=auto_with_fallback：good=0.9，领先次名 0.3000
PASS manual 档：永不自选（winner 为空，交给 rc=4）
PASS 默认档：同分 NO-WINNER（不赌）
PASS auto 档：同分也给出一个（并明说可能只是随手挑）
PASS 默认档：全挂时 NO-WINNER（不拿 0 分当赢家）
PASS 预算：超时记 rc=124 且不当作通过
PASS vote 档：一致度并列 -> NO-WINNER
PASS 未知接受模式 -> None（fail-closed，不猜）
SELFSCAN2 OK 11/11
```

## 许可

本工具为独立实现，MIT。设计灵感与打分权重口径来自
[vstorm-co/pydantic-deepagents](https://github.com/vstorm-co/pydantic-deepagents)（MIT）。
