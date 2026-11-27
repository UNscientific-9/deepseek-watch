#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docwatch.wake —— 唤醒服务器上的 pi agent，让它调查 + 解说一次文档变化。

**分工（CONTRACT §3.3 铁律，2026-11-04 用户拍板）**：
  - `level` / 发不发 → 规则（`docwatch/rules.py`，由 watch_docs.py 传进来）
  - 模型 → 只负责「调查 + 把变化讲成人话」，**无权压级、无权拦信**
  所以本模块解析模型输出后会**丢掉模型给的 level**，最终 Verdict 的 `level` 用
  `--rule-level`（或 `<job>/rule.json` / manifest 里的 `rule_level`），并写 `level_source: "rule"`。

用法（服务器上一般由 watch_docs.py 调，也可手跑）：

    # 只打印「将发送给 pi 的提示词」，不调模型（本地也能跑）
    python -m docwatch.wake --job /tmp/jobs/7 --rule-level 3 --dry

    # 真正唤醒 pi 调查 + 解说（规则判为 3 级 → 会发信）
    python -m docwatch.wake --job /tmp/jobs/7 --rule-level 3
    python -m docwatch.wake --job 7 --data-dir /opt/docwatch/data     # 按 id 找 <data>/jobs/7

    # 解说 + 只读复核（规则 level >= 2 时触发第二遍，只查瞎编，不改 level）
    python -m docwatch.wake --job /tmp/jobs/7 --rule-level 2 --review

    # 调试：把一份已有的 --mode json 输出重新解析成 Verdict
    python -m docwatch.wake --job /tmp/jobs/7 --parse-file /tmp/jobs/7/pi-raw.ndjson

给 watch_docs.py 的调用方式（编排方只需要这两件事）：

    cmd = [sys.executable, "/opt/docwatch/wake.py",
           "--job", str(job_dir),
           "--rule-level", str(level),          # rules.py 定的级别，必传
           "--data-dir", "/opt/docwatch/data",
           "--timeout", "300", "--budget", "720", # 由调用方按外层超时留余量
           "--json", "--cleanup"]
    if level >= 2:                              # CONTRACT §7.5：只有 2 级及以上才复核
        cmd.append("--review")
    subprocess.run(cmd, timeout=780, check=False)
    verdict = json.loads(open(job_dir / "verdict.json"))   # 读文件也行，stdout 也行

    - 复核（`--review`）不改 level，只查解读有没有瞎编。
    - wake.py **永远**返回 0（只要 verdict.json 写出来了），pi 挂了/输出乱码都会产出
      `degraded=true` 的兜底结论，不会把异常抛给发信那一段。
    - 磁盘：默认 `--raw-keep summary`（约 5KB/次）+ 不存 pi 会话；开 `full` 一次 0.5~1.5MB。

设计要点：
1. 提示词只说「你是谁、材料在哪、结论要什么形状」；工具手册放在作业目录的 `AGENTS.md`
   （由 `docwatch/AGENT.md` 填变量生成），pi 会自己把它读进 system prompt。
2. 一次性工作区：`<job>/.pi-agent`（models/mcp/auth 的副本，不写 pi 用户自己的目录）
   + `<job>/.home`（HOME 也落在作业目录，作业目录一删就干净）。
3. 围栏：优先 `systemd-run`（MemoryHigh/MemoryMax、CPUQuota、RuntimeMaxSec、
   ProtectSystem=strict + 只把作业目录 bind 成可写）→ 被唤醒的 agent 改不了 /opt/docwatch。
   没有 systemd 时退化成普通子进程（可选 RLIMIT_AS）。
4. 解析：`--mode json` 是 NDJSON 事件流；取 `agent_end` 最后一条 assistant 消息的 text 块
   → 抽 JSON（容忍围栏、前后废话、尾逗号、中文引号）→ 校验成 Verdict 的**解说字段**。
5. 兜底：解析失败重试一次（更严厉的提示词）；再失败也**必须**产出一个 Verdict
   （level 仍是规则给的级别，`headline="模型未能给出解读"`），原始输出留档，不抛异常。
6. 用量留档：`verdict.json` / `pi-raw.ndjson` / `pi-prompt.txt` / `wake.json`。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DATA_DIR = os.environ.get("DOCWATCH_DATA_DIR", "/opt/docwatch/data")
DEFAULT_PI = os.environ.get("DOCWATCH_PI", "/opt/agent/bin/agent")
# systemd 围栏里的 pi 不再跟主服务一起跑 root；默认用现有的 pi 系统用户。
DEFAULT_PI_USER = os.environ.get("DOCWATCH_PI_USER", "pi")
DEFAULT_PI_GROUP = os.environ.get("DOCWATCH_PI_GROUP", "pi")
DEFAULT_PROVIDER = "llm"
DEFAULT_MODEL = "example-route-v2"
DEFAULT_FALLBACK_MODEL = ""   # 路由组内已按 order 逐个兜底，外层不再重复切模型
DEFAULT_THINKING = "high"
# 服务器实测：/opt/agent-run.sh 里用的就是这个路径
DEFAULT_AGENT_SRC = "/home/user/.pi/agent"
MAX_STEPS = 12

LEVEL_LABELS = {0: "噪音", 1: "轻微", 2: "值得看", 3: "重要"}
LEVEL_ALIASES = {
    "重要": 3, "值得看": 2, "轻微": 1, "噪音": 0, "噪声": 0,
    "critical": 3, "important": 3, "major": 3, "high": 3,
    "medium": 2, "moderate": 2, "normal": 2, "info": 2,
    "low": 1, "minor": 1, "trivial": 1,
    "noise": 0, "none": 0, "ignore": 0,
}

# 模型的输出字段（无 level：级别由规则决定）
# tldr_points / overview / sections 是详情页两版解读的结构化内容；tldr / what_changed /
# why_matters / action 继续供邮件、RSS、列表摘要和旧版渲染回退使用。
MODEL_FIELDS = ("tldr", "tldr_points", "overview", "sections", "headline",
                "what_changed", "why_matters", "action", "evidence", "model_note")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_write_text(path: Path, text: str, append: bool = False) -> None:
    """root 往作业目录写文本时统一走这里：拒绝跟随符号链接。

    作业目录在 2026-11 的安全修复后归 root 所有，正常情况 pi 无法在其中
    创建链接；这里仍保留 O_NOFOLLOW 作为兜底，避免旧作业目录残留的链接
    把 root 写入引到 /etc 等位置。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    if os.name != "posix":
        with open(path, "a" if append else "w", encoding="utf-8") as fh:
            fh.write(text)
        return
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _safe_open_binary(path: Path, append: bool = False):
    """root 写 pi 原始输出/错误日志用；返回可写二进制文件对象。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    if os.name != "posix":
        return open(path, "ab" if append else "wb")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o640), "wb")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """在目标目录写临时文件再 os.replace，替换 symlink 本身而不是它的目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if os.name != "posix":
        with open(tmp, "xb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    """创建目录；遇到符号链接直接拒绝，避免 out/ 被换成指向别处的链接。"""
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"拒绝符号链接目录：{path}")
    path.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise OSError(f"不是目录：{path}")
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows 上 POSIX 权限位不完整支持；权限收紧失败不阻断流程。
        pass


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    _safe_write_text(tmp, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def truncate(text, limit: int) -> str:
    text = "" if text is None else str(text)
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def has_budget(started_at: float, budget: float) -> bool:
    """整次唤醒还剩不剩预算（严格重试 / 复核 / 重写前必须检查）。"""
    try:
        return (time.monotonic() - float(started_at)) < float(budget)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 作业目录 / manifest / 规则级别
# --------------------------------------------------------------------------- #

def resolve_job_dir(args) -> Path:
    raw = args.job
    if raw is None:
        cwd = Path.cwd()
        if (cwd / "manifest.json").exists() or (cwd / "BRIEF.md").exists():
            return cwd
        raise SystemExit("wake: 没有指定 --job，当前目录也不是作业目录（缺 manifest.json/BRIEF.md）")
    p = Path(raw).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = Path(args.data_dir).expanduser() / "jobs" / str(raw)
    if cand.is_dir():
        return cand.resolve()
    raise SystemExit(f"wake: 作业目录不存在：{raw}（也试过 {cand}）")


def load_manifest(job_dir: Path) -> dict:
    m = read_json(job_dir / "manifest.json", None)
    if isinstance(m, dict):
        m.setdefault("signals", [])
        if not isinstance(m.get("signals"), list):
            m["signals"] = []
        return m
    brief = ""
    try:
        brief = (job_dir / "BRIEF.md").read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = max(0, len(re.findall(r"^##\s*信号|^##\s*Signal", brief, re.M)))
    return {"id": job_dir.name, "created_at": now_iso(), "mode": "normal", "signals": [],
            "pages_changed": n, "note": "manifest.json 缺失，按 BRIEF.md 推断"}


def resolve_rule_level(job_dir: Path, manifest: dict, args) -> tuple:
    """级别只认规则给的。返回 (level, source_detail, warnings)。

    查找顺序：--rule-level → --rule-file → <job>/rule.json → manifest.rule_level/level → 默认 2。
    默认 2 是「保守地照发」：宁可多打扰一次，也不让模型或缺失字段把邮件吞掉。
    """
    warnings = []
    if args.rule_level is not None:
        return clamp_level(args.rule_level), "--rule-level", warnings
    candidates = []
    if args.rule_file:
        candidates.append((Path(args.rule_file), "--rule-file"))
    candidates.extend([(job_dir / "rule.json", "<job>/rule.json")])
    for path, label in candidates:
        data = read_json(path, None)
        if isinstance(data, dict):
            for key in ("level", "rule_level"):
                if key in data:
                    lv = clamp_level(data[key], default=None)
                    if lv is not None:
                        if data.get("noise") is not None:
                            args._rule_noise = bool(data["noise"])
                        return lv, f"{label}:{key}", warnings
    for key in ("rule_level", "level"):
        if key in manifest:
            lv = clamp_level(manifest[key], default=None)
            if lv is not None:
                return lv, f"manifest.json:{key}", warnings
    warnings.append("调用方没有给规则级别（--rule-level / rule.json / manifest.rule_level），"
                    "按默认 2 级处理：会发信，但级别可能不准")
    return 2, "default(2)", warnings


def clamp_level(value, default: int = 1):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return max(0, min(3, int(round(float(value)))))
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in LEVEL_ALIASES:
            return LEVEL_ALIASES[s]
        m = re.search(r"[0-3]", s)
        if m:
            return int(m.group(0))
    return default


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

JUDGE_PROMPT = """\
你是 docwatch 的「文档变化调查员 + 解说员」。你现在在一台 Linux 服务器上，工作目录就是
本次变化的证据作业目录：

  {job_dir}

【分工，先说清楚（很重要）】
DeepSeek 官方站点（api-docs.deepseek.com 中文站 57 页 + www.deepseek.com 中文首页，共 58 页）
刚发生变化。**「要不要通知用户、算几级」已经由上游规则判定了**，不需要你决定，你也改不了。
你那部分工作只有两件：
  1) 调查：这次到底改了什么？是真实内容变化，还是重新构建/样式/时间戳这类噪音？
  2) 解说：用用户能看懂的人话把它讲清楚 —— 改了什么、对他有什么影响、要不要动手。

所以：
- 不要写「可以忽略」「不用看」这种替用户做决定的结论性话术；
- 如果你觉得规则判重了，把你的看法写进 `model_note`（只作参考，不影响发信）；
- 规则这次判为 **level {rule_level}（{rule_label}）**，会照发，你就按这个前提好好解释。

【本次作业】
  作业号：{job_id}        触发时间：{created_at}        档位：{mode}（high=任何变化都报）
  变化页：{pages_changed} / {pages_total} 页    信号：{n_signals} 条
  信号概览：
{signal_lines}

【第一步：先读简报（必做）】
  1) 读 ./BRIEF.md —— 人读简报：按页面分组的信号 + 正文 diff + 新增/消失的 JS 字符串。
  2) 读 ./manifest.json —— 机器可读明细（每个信号的 kind/page/asset/before/after）。
  证据文件：
     pages/<slug>.before.txt / .after.txt   变化页正文快照
     assets/<slug>.before.txt / .after.txt  变化资源的新旧文本
     diff/<slug>.diff                       统一 diff

【第二步：只在必要时深挖】
如果 BRIEF.md + manifest.json 已经能讲清楚，**不要**多查，直接写结论（省时间省钱）。
判读阶段只给你 read 工具（无 bash / 无联网），以本目录的 BRIEF.md、manifest.json、pages/、
assets/、diff/ 为准；如果运行环境额外提供了只读工具箱（命令前缀 {toolbox}），才可以用：

  {toolbox} brief {job_id}                    # 再看一遍简报
  {toolbox} signals {job_id} --json           # 本轮信号明细（kind/detail/before/after）
  {toolbox} text <URL>                        # 抓线上页面并转纯文本（HTML 和 Next.js 都行）
  {toolbox} strings <文件> --grep 价格         # 在压缩 JS 里搜关键词
  {toolbox} diff <页面URL|资源URL> --n 3       # 和上一版、上上版对比
  {toolbox} search deepseek-v4                # 在全部历史快照里搜
  {toolbox} history --days 7                  # 最近几次触发与判定

完整用法见本目录的 AGENTS.md（你已经在读了，里面还有判定标准、噪音清单和真实案例）。
调查最多 {max_steps} 步（一次命令 / 一次文件读取算一步），到第 {max_steps} 步必须收尾。

【最后一步：只输出一个 JSON】
最后一条消息必须是**且只是**一个 JSON 对象，不要 Markdown 代码围栏，不要解释，
不要「结论如下：」这种前缀。字段（缺哪个就留空字符串/空数组，不要编造）：

{{
  "headline": "一句话说清这次改了什么，带具体数字/名称；如果免费/仍收费并存要写出反差",   // ≤ 40 字
  "tldr": "给列表/RSS/邮件用的一句话省流，60-120 字，说清改了什么 + 对用户有什么影响",
  "tldr_points": ["详情页省流要点：只挑最重要的，每条一句完整的话", "..."],   // 1-7 条
  "overview": "详情页总述：本轮整体改了什么、主线是什么；不要提内部定级",   // 1 段
  "sections": [
    {{
      "title": "主题名，如：计费口径与价格表 / 联网工具 / 接入 Claude Code",
      "paragraphs": ["把这一主题下的变化写清楚：改前是什么、改后是什么、对谁有影响", "..."]
    }}
  ],
  "what_changed": "具体到哪一页、哪一段、改前是什么、改后是什么",   // 2-4 句，供邮件/旧版回退
  "why_matters": "对他有什么影响（钱/能力/兼容性/要改代码吗）",      // 1-3 句，没影响就说没影响
  "action": "要不要动手、动什么；不用动就写「无需操作」",
  "evidence": ["实际看过的证据，例：diff/pricing.diff 第 3 行：13.50 → 15.00", "..."],
  "model_note": "你自己的看法（可选）：比如「看起来只是措辞微调」「JS 重建但正文没变」"
}}

详情字段要求（这是详情页「省流 + 详细分析」的固定骨架，必须遵守）：
- `tldr_points`：1-7 条，只挑最重要的；每条一行、一个完整句子，带具体对象或数字；不要写「详见下文」这类空话，不要把资源文件名/技术细节塞进省流。不要单列「价格与接口未变 / 其他不受影响」这类纯否定句；这类信息只在影响段作为对照出现。
- `sections`：按主题分组，组数随内容多少而定（1-20 组）。事实材料里**每一条有变化的内容都必须覆盖**；每组至少 1 段，每段约 80-160 字、最多不超过 180 字或 3 句，宁可拆成两小段，不要堆成长墙。
- `why_matters` 写「对谁有影响、影响是什么」；如果确实没有直接影响，就直接写「对普通用户没有影响，只影响……」，不要为了配合级别硬写重要性。
- 每个 `paragraphs` 是成段文字，不是要点罗列；要写「改前 → 改后 → 对谁有影响」。详情页渲染时每段前会加一个黑色圆点作视觉锚点（省流圆点是蓝色），正文仍要完整成句。
- `overview` 是一段连贯总述，不要提「规则定级 / X 级 / 值得看 / 重要」这类内部信息。
- **权限错误、作业目录、EACCES、BRIEF.md、manifest.json、pages/diff、无法逐条核对**这类内部诊断，不许出现在 headline/tldr/tldr_points/overview/sections/what_changed/why_matters/action/model_note 这些叙述字段里。`evidence` 是唯一例外：可以写「diff/pricing.diff 第 3 行」这种证据文件名和行号，方便读者核对。详细分析里可以写接口名、字段名、页面路径、数字、时间。
- 纯重建/噪音也要写清楚「凭什么判断正文没变、对比了哪些文件、哪些东西明确没有变化」，不能只写一句「无影响」。

写法要求（用户不懂技术，但很在意钱和能不能用）：
- 价格/计费类变化必须写出「改前 → 改后」的具体数字和单位；
- 接口变化必须写出接口名 + 参数名 + 是不是破坏性（老代码会不会挂）；
- 纯前端重建就说「只是重新构建，正文没变」，并说明你凭什么这么判断（对比了哪几个文件）；
- evidence 只写你**实际看过**的文件或命令结果，不要编。

【红线】
- 只读调查：除 ./out/ 外不要写、不要删任何文件；不要 sudo；不要发邮件/发消息；
- 不要碰 /opt/docwatch、/opt/llm-gateway、other-services 等与本任务无关的服务和配置；
- 不确定就少说，绝对不要编造证据或编造线上页面的内容。
"""

REVIEW_PROMPT = """\
你是 docwatch 的「解读复核员」。上游规则已经决定了级别和是否发信（level {rule_level}），
你**不参与**那个决定，也不许改级别。你只核对一件事：下面这份「解读」是不是被证据支持。

作业目录：{job_dir}
本轮信号数：{n_signals}，变化页：{pages_changed} / {pages_total}

待复核的解读 JSON：
{verdict_json}

你可以只读查看证据：BRIEF.md、manifest.json、pages/、assets/、diff/
（注意：这一遍你**只有 read 工具**，没有 bash、没有联网，所以没法跑工具箱、也没法抓线上页面；
核对不到的内容就按「无法核实」处理，不要推测）。

逐条核对（只有这四类问题算不通过）：
1. **瞎编**：解读里的事实（改了哪一页、改前改后是什么、新增了哪些字符串/数字/参数名）
   在证据里找不到，或者证据里明显不是这个意思。
2. **抄错**：金额、单价、模型名、接口名、参数名、日期与证据不一致（差一个数字都算）。
3. **漏关键变化**：信号里明明有价格/计费/限流/新接口/新页面/下线这类变化，
   解读却只字未提，或者把它说成「无关紧要的调整」。
4. **答非所问**：headline 说的和 what_changed 说的不是一回事，或通篇空话没有具体内容。

注意：语气、详略、措辞风格不算问题；`model_note` 里模型自己的看法也不算问题。
证据里**根本没有**这条信息、你也无法核实（比如它抓了线上页面）时，只要它没说出与证据相反的话，
就按「无法核实」放过，不要因此判不通过 —— 判不通过要有实锤的冲突或编造。

最后一条消息必须是**且只是**一个 JSON 对象，不要围栏、不要解释：
{{"pass": true 或 false, "reason": "一句话理由（引具体证据；通过就写核对了什么）"}}
"""

STRICT_RETRY_NOTE = """
【上一次输出无法解析，重发要求】
你上一条回复不是合法的 JSON 对象（可能带了代码围栏、解释或多余的括号）。
现在**只**输出那一个 JSON 对象：第一个字符是 `{{`，最后一个字符是 `}}`，
不要 ```json 围栏，不要任何前后文字。字段：{fields}。

原任务回顾：
{task}
"""


def fmt_signal_lines(signals: list, limit: int = 12) -> str:
    if not signals:
        return "    （manifest 里没有信号明细，直接看 BRIEF.md）"
    lines = []
    for i, s in enumerate(signals[:limit], 1):
        if not isinstance(s, dict):
            lines.append(f"    {i}. {truncate(s, 160)}")
            continue
        kind = s.get("kind", "?")
        where = s.get("page") or s.get("asset") or ""
        detail = s.get("detail") or ""
        extra = ""
        before, after = s.get("before"), s.get("after")
        if before or after:
            extra = f"  [{truncate(before, 60)} → {truncate(after, 60)}]"
        lines.append(f"    {i}. ({kind}) {truncate(where, 90)}\n       {truncate(detail, 160)}{extra}")
    if len(signals) > limit:
        lines.append(f"    …另有 {len(signals) - limit} 条，见 manifest.json")
    return "\n".join(lines)


def _systemd_available() -> bool:
    return bool(shutil.which("systemd-run")) and hasattr(os, "geteuid") and os.geteuid() == 0


def toolbox_prefix(args) -> str:
    p = Path(args.toolbox) if args.toolbox else Path(__file__).with_name("toolbox.py")
    if p.exists():
        return f"python3 {p}"
    return "python3 -m docwatch.toolbox"


def pick_thinking(args) -> str:
    """默认 high（用户 2026-11-05 要求）；显式 --thinking 可覆盖。"""
    return args.thinking or DEFAULT_THINKING


def build_judge_prompt(job_dir: Path, manifest: dict, args, rule_level: int) -> str:
    signals = manifest.get("signals") or []
    return JUDGE_PROMPT.format(
        job_dir=str(job_dir),
        job_id=manifest.get("id", job_dir.name),
        created_at=manifest.get("created_at", "?"),
        mode=manifest.get("mode", args.mode or "normal"),
        pages_changed=manifest.get("pages_changed", "?"),
        pages_total=manifest.get("pages_total", "?"),
        n_signals=len(signals),
        signal_lines=fmt_signal_lines(signals),
        toolbox=toolbox_prefix(args),
        max_steps=args.max_steps,
        rule_level=rule_level,
        rule_label=LEVEL_LABELS.get(rule_level, "?"),
    )


def build_review_prompt(job_dir: Path, manifest: dict, verdict: dict, args,
                        rule_level: int) -> str:
    shown = {k: verdict.get(k) for k in MODEL_FIELDS}
    return REVIEW_PROMPT.format(
        job_dir=str(job_dir),
        n_signals=len(manifest.get("signals") or []),
        pages_changed=manifest.get("pages_changed", "?"),
        pages_total=manifest.get("pages_total", "?"),
        verdict_json=json.dumps(shown, ensure_ascii=False, indent=2),
        toolbox=toolbox_prefix(args),
        job_id=manifest.get("id", job_dir.name),
        rule_level=rule_level,
    )


# --------------------------------------------------------------------------- #
# 一次性工作区 + AGENTS.md
# --------------------------------------------------------------------------- #

def render_agent_md(job_dir: Path, manifest: dict, args, work: dict) -> str:
    src = Path(args.agent_md) if args.agent_md else Path(__file__).with_name("AGENT.md")
    try:
        text = src.read_text(encoding="utf-8")
    except Exception:
        text = None
    if text is None:
        text = ("# 你是文档变化调查员 + 解说员\n\n"
                "工作目录是本次作业的证据目录，先读 BRIEF.md，再决定要不要深挖。\n"
                "工具箱：{{TOOLBOX}}\n级别由规则决定，你只负责讲清楚改了什么、有什么影响。\n"
                "最后只输出一个 JSON（headline/tldr/tldr_points/overview/sections/"
                "what_changed/why_matters/action/evidence/model_note）。\n")
    subs = {
        "{{JOB_DIR}}": str(job_dir),
        "{{JOB_ID}}": str(manifest.get("id", job_dir.name)),
        "{{MODE}}": str(manifest.get("mode", "normal")),
        "{{MAX_STEPS}}": str(args.max_steps),
        "{{TOOLBOX}}": toolbox_prefix(args),
        "{{DATA_DIR}}": str(Path(args.data_dir).expanduser()),
        "{{PI_MODEL}}": args.model,
        "{{PI_PROVIDER}}": args.provider,
        "{{PI}}": args.pi,
        "{{HOME}}": str(work.get("home", "")),
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def resolve_uid_gid(user: str, group: str = "") -> tuple[int, int] | None:
    """用户名/组名 → uid/gid；找不到返回 None（只在 Linux 上有意义）。"""
    try:
        import grp
        import pwd
        pw = pwd.getpwnam(user)
        gid = grp.getgrnam(group).gr_gid if group else pw.pw_gid
        return pw.pw_uid, gid
    except (ImportError, KeyError) as exc:
        print(f"wake: 警告：找不到运行 pi 的用户/组 {user}:{group or user}（{exc}）",
              file=sys.stderr)
        return None


def chown_job(path: Path, user: str, group: str = "") -> bool:
    """把指定目录树交给 pi 用户；只用于 job 下的 out/，绝不传整个作业目录。"""
    ids = resolve_uid_gid(user, group)
    if not ids:
        return False
    uid, gid = ids
    ok = True
    paths = [path]
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            paths.append(Path(current) / name)
    for item in paths:
        try:
            os.chown(item, uid, gid, follow_symlinks=False)
        except OSError as exc:
            print(f"wake: 警告：chown {item} 失败：{exc}", file=sys.stderr)
            ok = False
    return ok


def _harden_job_dir(job_dir: Path) -> None:
    """作业目录必须归 root 所有；pi 只拿 out/，否则 root 写日志会被链接写穿。"""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    st = os.lstat(job_dir)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"作业目录不是普通目录：{job_dir}")
    out_dir = job_dir / "out"
    # 把旧作业里 pi 拥有过的文件收回 root；out/ 留给 pi，不在这里处理。
    for current, dirs, files in os.walk(job_dir, followlinks=False):
        cur = Path(current)
        if cur == out_dir:
            dirs[:] = []
            continue
        for name in dirs + files:
            item = cur / name
            if item == out_dir:
                continue
            try:
                os.chown(item, 0, 0, follow_symlinks=False)
            except OSError:
                pass
    os.chown(job_dir, 0, 0)
    os.chmod(job_dir, 0o755)


def _read_regular_bytes(path: Path, label: str, expected_uid: int | None = None) -> bytes | None:
    """读普通文件，不跟随符号链接，并校验属主；失败返回 None。"""
    path = Path(path)
    if os.name != "posix":
        try:
            if path.is_symlink():
                print(f"wake: 警告：拒绝读取 {label}：符号链接（{path}）", file=sys.stderr)
                return None
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            print(f"wake: 警告：拒绝读取 {label}（{path}）：{exc}", file=sys.stderr)
            return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        print(f"wake: 警告：拒绝读取 {label}（{path}）：{exc}", file=sys.stderr)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            print(f"wake: 警告：拒绝读取 {label}：不是普通文件（{path}）", file=sys.stderr)
            return None
        if expected_uid is not None and st.st_uid not in (expected_uid, os.geteuid()):
            print(f"wake: 警告：拒绝读取 {label}：属主不是预期用户（{path}）", file=sys.stderr)
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = None
            return fh.read()
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


_SECRET_KEYS = ("apikey", "api_key", "token", "secret", "authorization", "password", "passwd")


def _collect_secret_values(obj, key: str = "") -> list[str]:
    """从配置对象中取出需要精确脱敏的值（只收集敏感键下面的字符串）。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lowered = str(k).lower()
            if any(marker in lowered for marker in _SECRET_KEYS):
                found.extend(_walk_strings(v))
            else:
                found.extend(_collect_secret_values(v, str(k)))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_secret_values(item, key))
    return found


def _walk_strings(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if len(text) >= 8 and not text.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_walk_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_walk_strings(item))
        return out
    return []


def _looks_like_secret(value: str) -> bool:
    return bool(re.search(r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|oc_[A-Za-z0-9_-]{16,}|bearer\s+\S{16,})", value)) or len(value) >= 40


def _sanitize_models_json(data, allowed_provider: str) -> dict:
    """只保留当前 provider 的启动配置，并去掉真实密钥。

    docwatch 默认 --provider llm，走本机 LLM gateway 网关（apiKey 是短占位符）；
    other-provider 等第三方 provider 的 51 字符密钥不复制给模型可读目录。
    """
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    if "$schema" in data:
        out["$schema"] = data["$schema"]
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return {k: v for k, v in data.items() if k != "providers"}
    kept: dict = {}
    chosen = providers.get(allowed_provider)
    if isinstance(chosen, dict):
        kept[allowed_provider] = _sanitize_provider_config(chosen)
    else:
        for name, cfg in providers.items():
            if isinstance(cfg, dict):
                kept[name] = _sanitize_provider_config(cfg)
    out["providers"] = kept
    return out


def _sanitize_provider_config(cfg: dict) -> dict:
    clean: dict = {}
    for key, value in cfg.items():
        lowered = str(key).lower()
        if "apikey" in lowered or lowered == "api_key":
            if isinstance(value, str) and not _looks_like_secret(value) and len(value) <= 16:
                clean[key] = value
            else:
                clean[key] = "redacted"
        elif lowered in ("authorization", "headers", "token", "secret", "password", "passwd"):
            continue
        elif isinstance(value, dict):
            clean[key] = _sanitize_provider_config(value)
        elif isinstance(value, list):
            clean[key] = [_sanitize_provider_config(item) if isinstance(item, dict) else item
                          for item in value]
        else:
            clean[key] = value
    return clean


def _source_expected_uid(args) -> int | None:
    """凭据/配置源目录的预期属主：生产是 pi，本地是当前用户；解析不到就不强制。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if getattr(args, "pi_user", ""):
            ids = resolve_uid_gid(args.pi_user, getattr(args, "pi_group", ""))
            return ids[0] if ids else None
        return 0
    if hasattr(os, "geteuid"):
        return os.geteuid()
    return None


def _prepare_auth_copy(src: Path, job_dir: Path, args) -> dict:
    """把 auth.json 放到作业目录之外，并作为 pi 的 agent 配置目录。

    作业目录整体交给 pi 用户且保留 120 份；凭据副本绝不能留在那里。
    生产 root 放在 systemd RuntimeDirectory（/run/docwatch-pi-auth/）下的随机子目录，
    本地/非 root 放在 <data-dir>/pi-auth/；root 拥有、pi 组可读（0640），
    跑完在 finally/信号里删除。
    目录本身必须给 pi 组写权限（0770）：pi 1.5.2 读取凭据前会在 auth.json 同目录
    创建 <auth>.lock 目录（proper-lockfile），0750 会让它在启动阶段就 EACCES 退出。
    """
    out = {"auth_path": None, "auth_root": None, "auth_link": None, "isolated": False}
    auth_src = src / "auth.json"
    if not auth_src.is_file():
        return out
    # 源文件必须按普通文件读取，不能跟随 pi 可写目录里的符号链接；否则 root 会把
    # /opt/docwatch-v2/env、/etc/shadow 等任意可读文件复制到 pi 组可读目录。
    expected_uid = _source_expected_uid(args)
    auth_bytes = _read_regular_bytes(auth_src, "auth.json", expected_uid)
    if auth_bytes is None:
        return out
    data_dir = Path(args.data_dir).expanduser()
    use_sandbox = (getattr(args, "sandbox", "none") in ("auto", "systemd")
                   and _systemd_available())
    # 生产 root 进程把凭据副本放到 systemd RuntimeDirectory 下：既不在作业目录里被保留，
    # 也不在模型能猜到的 /opt/docwatch-v2/data/pi-auth 固定路径上。主 unit 的
    # ProtectSystem=strict 让 /run 整体只读，只有 RuntimeDirectory 这一个子目录可写。
    auth_root: Path | None = None
    auth_parent: Path | None = None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raw_runtime = os.environ.get("RUNTIME_DIRECTORY") or ""
        # systemd 用冒号分隔多个 RuntimeDirectory；Windows 盘符也有冒号，不能盲拆。
        runtime_env = raw_runtime.split(":")[0] if os.name == "posix" else raw_runtime
        auth_parent = Path(runtime_env) if runtime_env else Path("/run/docwatch-pi-auth")
        try:
            auth_parent.mkdir(parents=True, exist_ok=True)
            # 不用 tempfile.mkdtemp：Windows 沙箱下 0700 mkdtemp 目录会拒绝后续写入；
            # 自己生成同强度随机名，再按目标权限创建。
            auth_root = auth_parent / f"job-{os.getpid()}-{time.time_ns()}-{secrets.token_hex(4)}"
            _ensure_dir(auth_root, 0o700)
        except OSError as exc:
            print(f"wake: 警告：无法在 {auth_parent} 创建凭据目录：{exc}", file=sys.stderr)
            auth_root = None
    if auth_root is None:
        # 使用 systemd 沙箱时绝不回退到 data/pi-auth：那个目录被沙箱
        # InaccessiblePaths 封禁，回退过去只会让 pi 启动失败，再触发无围栏降级。
        if use_sandbox:
            print("wake: 错误：无法创建沙箱可读的凭据目录，拒绝回退到 data/pi-auth",
                  file=sys.stderr)
            return out
        auth_root = data_dir / "pi-auth"
    out["auth_root"] = auth_root
    # 只有 root + RuntimeDirectory 的随机子目录才让 pi 直接把它当 agent 配置目录；
    # 非 root 本地调试仍退回共享 data/pi-auth，不能把整个目录当一次性配置删掉。
    out["isolated"] = auth_parent is not None
    target = auth_root / f"{job_dir.name}-{os.getpid()}-{time.time_ns()}.json"
    try:
        _ensure_dir(auth_root, 0o700)
        _write_bytes_atomic(target, auth_bytes)
        os.chmod(target, 0o600)
    except OSError as exc:
        print(f"wake: 警告：准备 auth.json 失败：{exc}", file=sys.stderr)
        return out
    out["auth_path"] = target

    if args.pi_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        ids = resolve_uid_gid(args.pi_user, args.pi_group)
        if ids:
            _uid, gid = ids
            try:
                # 父目录是 systemd RuntimeDirectory（root:root 0770），pi 组需要能穿过它
                # 才能进到随机子目录；只改权限，不删除（由 systemd 负责清理）。
                if auth_parent is not None and auth_parent != auth_root:
                    os.chown(auth_parent, 0, gid)
                    os.chmod(auth_parent, 0o770)
                os.chown(auth_root, 0, gid)
                os.chmod(auth_root, 0o770)   # 只给组写权限，供 pi 建 <auth>.lock 目录
                os.chown(target, 0, gid)
                os.chmod(target, 0o640)   # pi 组只读，pi 用户不能改
                out["gid"] = gid
            except OSError as exc:
                print(f"wake: 警告：auth.json 权限收紧失败：{exc}", file=sys.stderr)

    if out["isolated"]:
        # 生产路径：pi 的 agent 配置目录就是私有 auth_root，作业目录里不再出现
        # auth.json 链接，模型即使被注入也无法通过 .pi-agent/auth.json 读到凭据。
        return out
    # 本地非 root 调试：没有独立 RuntimeDirectory，只能放回作业目录旁边；
    # 生产 unit 永远走上面的 isolated 分支。
    agent_dir = job_dir / "out" / ".pi-agent"
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    link = agent_dir / "auth.json"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target, link)
        out["auth_link"] = link
        print("wake: 警告：非 root 调试环境，auth.json 仍链接在作业目录；生产沙箱不会这样",
              file=sys.stderr)
    except OSError as exc:
        try:
            shutil.copyfile(target, link)
            os.chmod(link, 0o600)
            out["auth_link"] = link
            print(f"wake: 警告：无法创建 auth.json 符号链接（{exc}），已复制到作业目录",
                  file=sys.stderr)
        except OSError as copy_exc:
            print(f"wake: 警告：auth.json 链接/复制失败：{copy_exc}", file=sys.stderr)
    return out


def cleanup_workspace(job_dir: Path, work: dict, remove_config: bool = True) -> None:
    """删除凭据副本与一次性配置（finally / 信号处理共用；可重复调用）。"""
    auth_link = work.get("auth_link")
    if auth_link:
        try:
            Path(auth_link).unlink(missing_ok=True)
        except OSError:
            pass
    auth_path = work.get("auth_path")
    if auth_path:
        try:
            Path(auth_path).unlink(missing_ok=True)
        except OSError:
            pass
    auth_root = work.get("auth_root")
    if auth_root:
        try:
            Path(auth_root).rmdir()      # 只删空目录，不碰别人的文件
        except OSError:
            pass
    if remove_config:
        for key in ("agent_dir", "home"):
            path = work.get(key)
            if path:
                shutil.rmtree(Path(path), ignore_errors=True)


_ACTIVE_WORKSPACES: list[tuple[Path, dict, bool]] = []


def _cleanup_all() -> None:
    """兜底清理：无论 main 正常返回还是抛异常，都清掉本次注册的工作区。"""
    while _ACTIVE_WORKSPACES:
        job_dir, work, remove_config = _ACTIVE_WORKSPACES.pop()
        cleanup_workspace(job_dir, work, remove_config=remove_config)


def _install_cleanup(job_dir: Path, work: dict, args) -> None:
    """正常退出、异常、SIGTERM/SIGINT 都清掉 .home/.pi-agent 与凭据副本。"""
    remove_config = bool(getattr(args, "cleanup", False))

    def _cleanup() -> None:
        cleanup_workspace(job_dir, work, remove_config=remove_config)

    atexit.register(_cleanup)
    _ACTIVE_WORKSPACES.append((job_dir, dict(work), remove_config))

    def _handler(signum, _frame) -> None:
        _kill_active_child()
        cleanup_workspace(job_dir, work, remove_config=True)
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass


def prepare_workspace(job_dir: Path, manifest: dict, args) -> dict:
    """作业目录里准备 pi 的 HOME / agent 配置目录 / AGENTS.md（不碰 pi 用户自己的目录）。"""
    job_dir = Path(job_dir)
    # 关键安全边界：job_dir 归 root，pi 只拿 out/。否则 pi 可以在 job 里预置
    # wake.log / verdict.json 等符号链接，让 root 的日志/结果写入穿透到 /etc。
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _harden_job_dir(job_dir)
    out_dir = job_dir / "out"
    # 沙箱把作业目录挂成只读、只留 out/ 可写；所有需要写的位置都放在 out/ 下。
    home = out_dir / "home"
    _ensure_dir(out_dir, 0o700)
    _ensure_dir(home, 0o700)

    src = Path(args.agent_src).expanduser()
    auth_work = _prepare_auth_copy(src, job_dir, args) if src.is_dir() else {}
    # 生产把私有 auth_root 直接当 pi 的 agent 配置目录；本地调试才用 out/.pi-agent。
    agent_dir = (
        Path(auth_work["auth_root"])
        if auth_work.get("isolated") and auth_work.get("auth_root")
        else out_dir / ".pi-agent"
    )
    if auth_work.get("isolated"):
        _ensure_dir(agent_dir, 0o770)
    else:
        _ensure_dir(agent_dir, 0o700)

    copied: list[str] = []
    extra_secrets: list[str] = []
    if src.is_dir():
        # auth.json 单独处理：绝不复制进可写作业目录。models.json 只保留当前
        # provider 的启动配置并剔除真实 apiKey；mcp.json/models-store.json 不复制
        # （pi 始终 --no-mcp，模型不需要也不应该看到第三方 MCP 凭据）。
        models_src = src / "models.json"
        if models_src.is_file():
            raw = _read_regular_bytes(models_src, "models.json", _source_expected_uid(args))
            if raw is not None:
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    extra_secrets.extend(_collect_secret_values(data))
                    clean = _sanitize_models_json(data, getattr(args, "provider", ""))
                    _write_bytes_atomic(agent_dir / "models.json",
                                        json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8"))
                    try:
                        if auth_work.get("gid") is not None:
                            os.chown(agent_dir / "models.json", 0, int(auth_work["gid"]))
                        os.chmod(agent_dir / "models.json", 0o640)
                    except OSError:
                        pass
                    copied.append("models.json")
                except (OSError, ValueError) as exc:
                    print(f"wake: 警告：准备 models.json 失败：{exc}", file=sys.stderr)
    if "models.json" not in copied:
        print(f"wake: 警告：{src} 下没有可用的 models.json，pi 可能找不到 provider {args.provider}；"
              f"用 --agent-src 指定正确目录", file=sys.stderr)

    agents_md = job_dir / "AGENTS.md"
    body = render_agent_md(job_dir, manifest, args, {"home": home})
    old = ""
    if agents_md.exists() and not agents_md.is_symlink():
        raw_old = _read_regular_bytes(agents_md, "AGENTS.md")
        if raw_old is not None:
            old = raw_old.decode("utf-8", errors="replace")
    if old.strip() and old != body and "文档变化调查员" not in old:
        # 只备份别人放进去的 AGENTS.md；我们自己生成的直接覆盖，不制造 .bak 垃圾
        try:
            _safe_write_text(job_dir / "AGENTS.md.bak", old)
        except OSError:
            pass
    _safe_write_text(agents_md, body)
    # 主服务是 root，pi 子进程要降权成 pi；只把 out/ 交给它，job_dir 保持 root。
    if args.pi_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        chown_job(out_dir, args.pi_user, args.pi_group)
    result = {"agent_dir": agent_dir, "home": home, "agents_md": agents_md,
              "out_dir": out_dir, "copied": copied, "extra_secrets": extra_secrets}
    result.update(auth_work)
    return result


# --------------------------------------------------------------------------- #
# 调 pi
# --------------------------------------------------------------------------- #

def build_env(job_dir: Path, manifest: dict, work: dict, args) -> dict:
    env = {
        "PATH": os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "LC_ALL": os.environ.get("LC_ALL") or "C.UTF-8",
        "TZ": os.environ.get("TZ") or "Asia/Shanghai",
        "HOME": str(work["home"]),
        "PI_CODING_AGENT_DIR": str(work["agent_dir"]),
        "NODE_OPTIONS": "--dns-result-order=ipv4first",   # 实测不设会走 IPv6 卡几十秒
        "DOCWATCH_JOB_DIR": str(job_dir),
        "DOCWATCH_JOB_ID": str(manifest.get("id", job_dir.name)),
        "DOCWATCH_DATA_DIR": str(Path(args.data_dir).expanduser()),
        "DOCWATCH_TOOLBOX": toolbox_prefix(args),
        "NO_COLOR": "1",
    }
    return env


def pi_argv(prompt: str, session_id: str, args, review: bool = False) -> list:
    argv = [args.pi,
            "--provider", args.provider,
            "--model", args.model,
            # pi 默认系统提示不含模型身份；补一句，让模型知道自己在哪个模型上运行。
            "--append-system-prompt",
            f"环境信息：本次请求使用的模型标识是 {args.provider}/{args.model}。"
            "这只是运行环境信息，与文档内容无关，不要写进判读结论。",
            "--thinking", args.thinking or DEFAULT_THINKING,
            "--mode", "json"]
    # 判读 / 重试 / 备用模型 / 复核一律只读：网页内容属于攻击者可控输入，
    # 不给 bash/edit/write，也不给 MCP 联网面；需要深挖时以 BRIEF.md/manifest.json/diff 为准。
    argv += ["--tools", "read", "--no-mcp"]
    if not args.keep_session:
        # 会话文件每个约 50KB，作业目录是一次性的，默认不留（调试时 --keep-session）
        argv.append("--no-session")
    argv += ["--session-id", session_id, "--print", prompt]
    return argv


def build_command(env: dict, pi_cmd: list, job_dir: Path, args, unit: str = "",
                  auth_root: Path | None = None) -> tuple:
    """返回 (argv, sandbox_used)。有 root + systemd 就套 systemd-run 围栏。"""
    sandbox = args.sandbox
    have_systemd = _systemd_available()
    if sandbox == "auto":
        sandbox = "systemd" if have_systemd else "none"
    if sandbox == "systemd" and not have_systemd:
        print("wake: 警告：要 systemd 围栏但当前不是 root 或没有 systemd-run，退化为普通子进程",
              file=sys.stderr)
        sandbox = "none"
    if sandbox != "systemd":
        # 没有 systemd 围栏也不能把 pi 跑成 root：用 setpriv 至少把身份降下来。
        # 找不到用户或 setpriv 时宁可失败（run.py 会退回规则文案），也不默默用 root。
        if args.pi_user and hasattr(os, "geteuid") and os.geteuid() == 0:
            ids = resolve_uid_gid(args.pi_user, args.pi_group)
            setpriv = shutil.which("setpriv")
            if ids and setpriv:
                uid, gid = ids
                print(f"wake: 未使用 systemd 围栏，改用 setpriv 降权到 {args.pi_user}:"
                      f"{args.pi_group or args.pi_user}", file=sys.stderr)
                return ([setpriv, "--reuid", str(uid), "--regid", str(gid),
                         "--init-groups"] + pi_cmd, sandbox)
            print("wake: 警告：未使用 systemd 围栏且无法降权，拒绝以 root 运行 pi",
                  file=sys.stderr)
            return ([sys.executable, "-c",
                     "import sys; print('wake: 无法降权，拒绝以 root 运行 pi', file=sys.stderr); "
                     "sys.exit(97)"], sandbox)
        return pi_cmd, sandbox

    unit = unit or f"docwatch-{int(time.time())}"
    pre = [
        "systemd-run", "--wait", "--pipe", "--quiet", "--collect", f"--unit={unit}",
        "--property=MemoryHigh=" + args.memory_high,
        "--property=MemoryMax=" + args.memory_max,
        "--property=MemorySwapMax=256M",
        "--property=CPUQuota=" + args.cpu_quota,
        "--property=TasksMax=128",
        "--property=NoNewPrivileges=yes",
        "--property=ProtectSystem=strict",
        # 模型只需要作业目录与它自己的临时配置；/home/user 下的 auth.json、
        # models.json 等凭据必须不可见，避免网页提示注入诱导 read 工具读取。
        "--property=ProtectHome=yes",
        "--property=InaccessiblePaths=-/opt/llm-gateway/data -/opt/docwatch-v2/env "
        "-/opt/deepseek-watch/env -/opt/deepseek-webwatch/env "
        "-/opt/docwatch-v2/data/docs.db -/opt/docwatch-v2/data/pi-auth",
        "--property=PrivateDevices=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectControlGroups=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "--property=OOMScoreAdjust=200",
        # 作业目录整体只读（证据不可被模型篡改），只有 out/ 额外 bind 成可写。
        "--property=BindReadOnlyPaths=" + str(job_dir),
        "--property=BindPaths=" + str(Path(job_dir) / "out"),
        # 凭据副本在 RuntimeDirectory 下，/run 整体只读，单独 bind 成可写，
        # 否则 pi 1.5.2 建 <auth>.lock 会 EACCES。
        *(["--property=BindPaths=" + str(auth_root)] if auth_root else []),
        "--property=WorkingDirectory=" + str(job_dir),
        "--property=RuntimeMaxSec=%ds" % int(args.timeout + 15),
    ]
    # 真正跑模型的 pi 降权到 pi 用户；主服务仍是 root，只是为了能调 systemd-run。
    if args.pi_user:
        pre += [f"--uid={args.pi_user}", f"--gid={args.pi_group or args.pi_user}"]
    if args.private_tmp:
        pre.append("--property=PrivateTmp=yes")
    for k, v in env.items():
        pre.append(f"--setenv={k}={v}")
    return pre + pi_cmd, sandbox


def _kill_tree(proc: subprocess.Popen, unit: str = "") -> None:
    """超时/中断时真的停掉 pi（只 kill systemd-run 客户端，单元还在跑，必须 systemctl stop）。"""
    if unit:
        try:
            subprocess.run(["systemctl", "stop", f"{unit}.service"], timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=8)
        return
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _watch_first_token(raw_path: Path, t0: float, stop: threading.Event,
                       out: dict) -> None:
    """旁路看 pi 的 NDJSON：记「首字」（第一个思考/正文增量）与首个正文增量的耗时。

    pi 的 --mode json 事件本身不带到达时间；这里按文件新增内容计时，20ms 精度足够
    区分「模型开始吐字」和「整轮结束」，不会影响 pi 进程。
    """
    pos = 0
    buf = b""
    while not stop.is_set():
        try:
            with open(raw_path, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except OSError:
            chunk = b""
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "message_update":
                    continue
                ame = ev.get("assistantMessageEvent") or {}
                kind = ame.get("type")
                if kind not in ("thinking_delta", "text_delta") or not ame.get("delta"):
                    continue
                now_ms = int((time.monotonic() - t0) * 1000)
                if "ttft_ms" not in out:
                    out["ttft_ms"] = now_ms
                    out["ttft_kind"] = kind
                if kind == "text_delta" and "first_text_ms" not in out:
                    out["first_text_ms"] = now_ms
        stop.wait(0.02)


_ACTIVE_PROC: subprocess.Popen | None = None
_ACTIVE_UNIT = ""


def _kill_active_child() -> None:
    """信号处理用：杀掉当前正在跑的 pi 进程组（若还活着）。"""
    proc = _ACTIVE_PROC
    if proc is None:
        return
    try:
        _kill_tree(proc, _ACTIVE_UNIT)
    except Exception:  # noqa: BLE001
        pass


def run_pi(argv: list, job_dir: Path, env: dict, raw_path: Path, err_path: Path,
           timeout: float, unit: str = "", sandbox: str = "none", args=None) -> dict:
    """跑一次 pi，stdout 直接落盘，返回耗时/退出码/是否超时。"""
    global _ACTIVE_PROC, _ACTIVE_UNIT
    t0 = time.monotonic()
    timed_out = False
    rc = None
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    with _safe_open_binary(raw_path) as fout, _safe_open_binary(err_path) as ferr:
        preexec = None
        if sandbox == "ulimit":
            limit = int(args.ulimit_vm_mb) * 1024 * 1024

            def preexec():  # noqa: F811  (RLIMIT_AS 只是兜底；Node 虚拟地址需求大，别设太小)
                import resource
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        try:
            proc = subprocess.Popen(argv, cwd=str(job_dir), env=env, stdin=subprocess.DEVNULL,
                                    stdout=fout, stderr=ferr, start_new_session=True,
                                    preexec_fn=preexec)
        except FileNotFoundError as e:
            return {"ok": False, "rc": 127, "elapsed_ms": 0, "timed_out": False,
                    "error": f"找不到可执行文件：{e}", "sandbox": sandbox}
        _ACTIVE_PROC = proc
        _ACTIVE_UNIT = unit
        timing = {}
        stop = threading.Event()
        watcher = threading.Thread(target=_watch_first_token,
                                   args=(Path(raw_path), t0, stop, timing), daemon=True)
        watcher.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc, unit)
            try:
                rc = proc.wait(timeout=20)
            except Exception:
                rc = -9
        finally:
            _ACTIVE_PROC = None
            _ACTIVE_UNIT = ""
            stop.set()
            watcher.join(timeout=2)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return {"ok": rc == 0, "rc": rc, "elapsed_ms": elapsed_ms, "timed_out": timed_out,
            "error": "" if rc == 0 else f"退出码 {rc}", "sandbox": sandbox, **timing}



# --------------------------------------------------------------------------- #
# NDJSON 解析
# --------------------------------------------------------------------------- #

def parse_ndjson(raw_text: str) -> dict:
    """从 pi 的 --mode json 事件流里抽：最终回答文本、token 用量、工具调用。

    实测事件：session / agent_start / turn_start / message_start / message_update /
    message_end / tool_execution_* / turn_end / agent_end / agent_settled。
    最终回答取 agent_end.messages 里最后一条 role=assistant 的 text 块（thinking/toolCall 丢弃）；
    token 按 turn_end 的 assistant usage 累加（每轮一次，不会重复计）。
    """
    out = {"final_text": "", "usage": {}, "turns": 0, "tool_calls": [], "aborted": False,
           "events": 0, "errors": [], "model": "", "provider": "", "stop_reason": "",
           "gen_ms": 0, "raw_len": len(raw_text or "")}
    usage_sum = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                 "reasoning": 0, "totalTokens": 0, "cost": 0.0}
    last_text, assistant_texts = "", []

    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            out["errors"].append(truncate(line, 200))
            continue
        if not isinstance(ev, dict):
            continue
        out["events"] += 1
        etype = ev.get("type")

        if etype == "tool_execution_start":
            out["tool_calls"].append(ev.get("toolName") or "?")
        elif etype == "turn_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                out["turns"] += 1
                _accum_usage(usage_sum, msg.get("usage") or {})
                try:
                    out["gen_ms"] += int(msg.get("durationMs") or 0)
                except (TypeError, ValueError):
                    pass
                out["model"] = out["model"] or msg.get("model") or ""
                out["provider"] = out["provider"] or msg.get("provider") or ""
                out["stop_reason"] = msg.get("stopReason") or out["stop_reason"]
                txt = _message_text(msg)
                if txt:
                    last_text = txt
                    assistant_texts.append(txt)
        elif etype == "agent_end":
            out["aborted"] = bool(ev.get("aborted"))
            for msg in ev.get("messages") or []:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    out["model"] = msg.get("model") or out["model"]
                    out["provider"] = msg.get("provider") or out["provider"]
                    txt = _message_text(msg)
                    if txt:
                        last_text = txt
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                txt = _message_text(msg)
                if txt:
                    last_text = txt
        elif etype in ("error", "session_error"):
            out["errors"].append(truncate(json.dumps(ev, ensure_ascii=False), 400))

    out["final_text"] = (last_text or (assistant_texts[-1] if assistant_texts else "")).strip()
    out["usage"] = usage_sum
    return out


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _accum_usage(acc: dict, u: dict) -> None:
    for k in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"):
        try:
            acc[k] = int(acc.get(k, 0)) + int(u.get(k) or 0)
        except (TypeError, ValueError):
            pass
    cost = u.get("cost")
    if isinstance(cost, dict):
        try:
            acc["cost"] = float(acc.get("cost", 0.0)) + float(cost.get("total") or 0)
        except (TypeError, ValueError):
            pass
    elif isinstance(cost, (int, float)):
        acc["cost"] = float(acc.get("cost", 0.0)) + float(cost)


# --------------------------------------------------------------------------- #
# 从文本里抽 JSON
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: str) -> tuple:
    """尽最大努力从模型输出里抽一个 JSON 对象。返回 (obj, 说明)。"""
    if not text or not text.strip():
        return None, "模型没有输出任何文本"
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)
    candidates.extend(_brace_objects(text))

    for cand in candidates:
        obj = _try_load(cand)
        if isinstance(obj, dict) and _looks_like_verdict(obj):
            return obj, "ok"
    for cand in candidates:
        obj = _try_load(cand)
        if isinstance(obj, dict):
            return obj, "ok(非标准字段)"
    return None, "输出里找不到合法 JSON 对象"


def _looks_like_verdict(obj: dict) -> bool:
    return bool(set(obj.keys()) & set(MODEL_FIELDS))


def _usable(obj) -> bool:
    """模型给了 JSON 但没有一个字段有可用内容（比如直接回 `{}`）→ 当作没解析出来。

    不能只看「字符串化后非空」：`{"sections":[{"title":"","paragraphs":[]}]}`、
    `{"sections":{"主题":["段落"]}}` 这类形状必须判为不可用，否则不会触发重试，
    下游还会把空组当有效结论。
    """
    if not isinstance(obj, dict):
        return False
    for key in MODEL_FIELDS:
        value = obj.get(key)
        if key == "tldr_points":
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, tuple)) and any(
                    isinstance(item, str) and item.strip() for item in value):
                return True
        elif key == "sections":
            if isinstance(value, (list, tuple)):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title")
                    paragraphs = item.get("paragraphs")
                    paragraphs_ok = (
                        isinstance(paragraphs, str) and paragraphs.strip()
                    ) or (
                        isinstance(paragraphs, (list, tuple))
                        and any(isinstance(p, str) and p.strip() for p in paragraphs)
                    )
                    if isinstance(title, str) and title.strip() and paragraphs_ok:
                        return True
        elif key == "evidence":
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, tuple)) and any(
                    isinstance(item, str) and item.strip() for item in value):
                return True
            if isinstance(value, dict) and value:
                return True
        elif isinstance(value, str) and value.strip():
            return True
    return False


def _try_load(text: str):
    text = (text or "").strip()
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i == -1:
        return None
    snippet = text[i:j + 1] if j > i else text[i:]
    for candidate in (snippet, text):
        for repair in (lambda s: s, _repair_json):
            try:
                return json.loads(repair(candidate))
            except Exception:
                continue
    return None


def _repair_json(s: str) -> str:
    """收拾模型常见的 JSON 毛病：中文引号、注释、尾逗号。"""
    out = s.replace("\u201c", '"').replace("\u201d", '"')
    out = re.sub(r"//[^\n\"]*$", "", out, flags=re.M)
    out = re.sub(r"/\*.*?\*/", "", out, flags=re.S)
    out = re.sub(r",(\s*[}\]])", r"\1", out)
    return out


def _brace_objects(text: str, limit: int = 40) -> list:
    """字符串感知的花括号配平，抽出所有 {...} 片段（长的优先）。"""
    found, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    found.append(text[start:i + 1])
    found.sort(key=len, reverse=True)
    return found[:limit]


# --------------------------------------------------------------------------- #
# 模型输出 → Verdict（解说字段）
# --------------------------------------------------------------------------- #

def review_pass(value) -> bool | None:
    """复核结论只接受明确的 true/false；缺字段或非法值返回 None（未完成复核）。

    不能用 coerce_bool(default=True)：模型漏写 pass 时会被当成「复核通过」，
    邮件页脚就会宣称二次校验成功，实际根本没做。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1", "是", "对"):
            return True
        if text in ("false", "no", "n", "0", "否", "不是"):
            return False
    return None


def coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "y", "1", "是", "对"):
            return True
        if s in ("false", "no", "n", "0", "否", "不是"):
            return False
    return default


def coerce_text(value, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return truncate(value.strip(), limit)
    if isinstance(value, (list, tuple)):
        return truncate("；".join(coerce_text(v, 400) for v in value), limit)
    if isinstance(value, dict):
        return truncate(json.dumps(value, ensure_ascii=False), limit)
    return truncate(str(value), limit)


def coerce_evidence(value) -> list:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for it in items[:20]:
        if isinstance(it, dict):
            label = it.get("file") or it.get("path") or it.get("source") or ""
            detail = it.get("detail") or it.get("text") or it.get("quote") or ""
            s = f"{label}: {detail}".strip(": ").strip() or json.dumps(it, ensure_ascii=False)
        else:
            s = str(it)
        s = truncate(s.strip(), 300)
        if s:
            out.append(s)
    return out


def coerce_text_list(value, limit: int = 7, item_chars: int = 300) -> list[str]:
    """把模型可能写成字符串 / 列表的字段收敛成干净字符串列表。

    对象/数组条目直接跳过：以前会 json.dumps 成 `{"point": "要点"}` 公开展示，
    读者看到的就是一段 JSON。坏形状由 normalize 写 warning 并在复核时重试。
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items[:limit]:
        if isinstance(item, (dict, list, tuple)):
            continue
        text = coerce_text(item, item_chars).strip()
        if text:
            out.append(text)
    return out


def coerce_sections(value) -> list[dict]:
    """收敛详情页 sections：只保留有标题且有非空段落的组。"""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        title = coerce_text(item.get("title"), 200).strip()
        paragraphs = coerce_text_list(item.get("paragraphs"), limit=12, item_chars=1000)
        if title and paragraphs:
            out.append({"title": title, "paragraphs": paragraphs})
    return out


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"oc_[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)(?:api[_-]?key|authorization)\s*[:=]\s*[^\s,;]{16,}"),
)


def load_auth_secrets(work: dict) -> list[str]:
    """从本次凭据副本/配置快照里取出密钥值，用于判读输出的精确匹配脱敏。"""
    found: list[str] = []
    found.extend(str(item) for item in (work.get("extra_secrets") or []) if item)
    path = work.get("auth_path")
    if not path:
        return found[:40]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return found[:40]

    def walk(value) -> None:
        if isinstance(value, str):
            text = value.strip()
            if len(text) >= 24 and not text.startswith(("http://", "https://")):
                found.append(text)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found[:40]


def scrub_secrets(text: str, secrets: list[str]) -> tuple[str, int]:
    """把模型输出里疑似凭据替换掉；返回 (文本, 命中数)。"""
    if not text:
        return text, 0
    hits = 0
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "[已脱敏]")
            hits += 1
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn("[已脱敏]", text)
        hits += count
    return text, hits


# 内部诊断 token：只替换成读者能理解的说法。文件名类 token 只从叙述字段清掉；
# evidence 允许保留「diff/pricing.diff 第 3 行」这类可核对证据，但不能出现作业目录。
_PUBLIC_TOKEN_PATTERNS = (
    (re.compile(r"BRIEF\.md", re.I), "作业简报"),
    (re.compile(r"manifest\.json", re.I), "证据清单"),
    (re.compile(r"pi-raw\.ndjson", re.I), "原始输出文件"),
    (re.compile(r"EACCES", re.I), "权限错误"),
    (re.compile(r"作业目录"), "本地证据目录"),
    (re.compile(r"(?:[A-Za-z]:\\|/)(?:opt|var/lib|home/pi|tmp)(?:[/\\][^\s，。；]*)*"), "本地路径"),
)
_EVIDENCE_TOKEN_PATTERNS = (
    (re.compile(r"BRIEF\.md", re.I), "作业简报"),
    (re.compile(r"manifest\.json", re.I), "证据清单"),
    (re.compile(r"pi-raw\.ndjson", re.I), "原始输出文件"),
    (re.compile(r"EACCES", re.I), "权限错误"),
    (re.compile(r"作业目录"), "本地证据目录"),
    (re.compile(r"(?:[A-Za-z]:\\|/)(?:opt|var/lib|home/pi)(?:[/\\][^\s，。；]*)*"), "本地路径"),
)


def _scrub_public_text(text: str, evidence: bool = False) -> tuple[str, int]:
    if not isinstance(text, str) or not text:
        return text, 0
    hits = 0
    for pattern, replacement in (_EVIDENCE_TOKEN_PATTERNS if evidence else _PUBLIC_TOKEN_PATTERNS):
        text, count = pattern.subn(replacement, text)
        hits += count
    return text, hits


def _scrub_public_value(value, evidence: bool = False):
    if isinstance(value, str):
        return _scrub_public_text(value, evidence)
    if isinstance(value, list):
        result, hits = [], 0
        for item in value:
            cleaned, count = _scrub_public_value(item, evidence)
            result.append(cleaned)
            hits += count
        return result, hits
    if isinstance(value, dict):
        result, hits = {}, 0
        for key, item in value.items():
            cleaned, count = _scrub_public_value(item, evidence)
            result[key] = cleaned
            hits += count
        return result, hits
    return value, 0


def scrub_verdict(verdict: dict, ctx: dict) -> dict:
    """判读结果离开进程前的最后一道：脱敏 + 清掉内部诊断 token。"""
    secrets = list(ctx.get("auth_secrets") or [])

    def scrub_value(value):
        if isinstance(value, str):
            return scrub_secrets(value, secrets)
        if isinstance(value, list):
            cleaned, hits = [], 0
            for item in value:
                text, count = scrub_value(item)
                cleaned.append(text)
                hits += count
            return cleaned, hits
        if isinstance(value, dict):
            cleaned, hits = {}, 0
            for key, item in value.items():
                text, count = scrub_value(item)
                cleaned[key] = text
                hits += count
            return cleaned, hits
        return value, 0

    redacted = 0
    internal_tokens = 0
    for key in MODEL_FIELDS:
        if key == "evidence":
            continue
        verdict[key], count = scrub_value(verdict.get(key))
        redacted += count
        verdict[key], count = _scrub_public_value(verdict[key], evidence=False)
        internal_tokens += count
    evidence = []
    for item in verdict.get("evidence") or []:
        cleaned, count = scrub_secrets(str(item), secrets)
        cleaned, internal = _scrub_public_text(cleaned, evidence=True)
        evidence.append(cleaned)
        redacted += count
        internal_tokens += internal
    verdict["evidence"] = evidence
    if redacted:
        verdict.setdefault("warnings", []).append(
            f"模型输出检测到 {redacted} 处疑似凭据，已脱敏")
    if internal_tokens:
        verdict.setdefault("warnings", []).append(
            f"模型输出含 {internal_tokens} 处内部诊断词，已改为读者可读文本")
    return verdict


def normalize_verdict(obj: dict, ctx: dict) -> dict:
    """把模型给的对象收敛成 CONTRACT §3.3 的 Verdict。

    **级别一律用 ctx["rule_level"]（规则给的），模型给的 level 直接丢弃**，
    只把模型自己的看法留在 `model_note` / `model_level_hint` 里。
    """
    obj = obj if isinstance(obj, dict) else {}
    warnings = []
    level = int(ctx["rule_level"])
    noise = bool(ctx.get("noise")) if ctx.get("noise") is not None else (level == 0)

    headline = coerce_text(obj.get("headline"), 200)
    what_changed = coerce_text(obj.get("what_changed"), 3000)
    why_matters = coerce_text(obj.get("why_matters"), 1500)
    action = coerce_text(obj.get("action"), 800)
    evidence = coerce_evidence(obj.get("evidence"))
    model_note = coerce_text(obj.get("model_note"), 400)
    # 详情页两版解读的结构化字段；老 verdict 没有时保持空列表/空字符串，走旧渲染。
    raw_tldr_points = obj.get("tldr_points")
    raw_sections = obj.get("sections")
    tldr_points = coerce_text_list(raw_tldr_points, limit=7, item_chars=300)
    overview = coerce_text(obj.get("overview"), 2000)
    sections = coerce_sections(raw_sections)
    if raw_tldr_points and not tldr_points:
        warnings.append("模型给了 tldr_points 但没有可用的字符串条目，已丢弃")
    if raw_sections and not sections:
        warnings.append("模型给了 sections 但没有可用的标题/段落，已丢弃")
    tldr = coerce_text(obj.get("tldr"), 120)
    if not tldr:
        first = re.split(r"[。；;\n]", what_changed.strip())[0] if what_changed else ""
        tldr = truncate(tldr_points[0] if tldr_points else (first or headline), 120)
    if not tldr_points and tldr:
        # 老模型没给结构化省流时，用一句话省流兜底成一个圆点，保证详情页仍有省流块。
        tldr_points = [tldr]

    if not headline:
        first = re.split(r"[。；;\n]", what_changed.strip())[0] if what_changed else ""
        headline = truncate(first or "检测到文档变化", 80)
        warnings.append("模型缺 headline，已从 what_changed 推断")
    if not why_matters:
        warnings.append("模型缺 why_matters（影响说明），发信端会留空")
    if not evidence:
        warnings.append("模型 evidence 为空（没有可引用的证据）")

    verdict = {
        # ---- 规则字段：level 不接受模型意见 ----
        "level": level,
        "level_source": "rule",
        "level_label": LEVEL_LABELS.get(level, ""),
        "noise": noise,
        # ---- 模型字段 ----
        "tldr": tldr,
        "tldr_points": tldr_points,
        "overview": overview,
        "sections": sections,
        "headline": headline,
        "what_changed": what_changed,
        "why_matters": why_matters,
        "action": action or ("无需操作" if level <= 1 else ""),
        "evidence": evidence,
        "model_note": model_note,
        # ---- 元数据 ----
        "model": ctx.get("model") or "",
        "model_fallback_from": ctx.get("model_fallback_from") or "",
        "elapsed_ms": int(ctx.get("elapsed_ms") or 0),
        "job_id": ctx.get("job_id"),
        "judged_at": now_iso(),
        "thinking": ctx.get("thinking"),
        "tokens": ctx.get("tokens") or {},
        "ttft_ms": ctx.get("ttft_ms"),
        "first_text_ms": ctx.get("first_text_ms"),
        "gen_ms": ctx.get("gen_ms"),
        "tps": ctx.get("tps"),
        "attempts": ctx.get("attempts", 1),
        "degraded": bool(ctx.get("degraded")),
        "warnings": warnings,
    }
    # 模型如果自己给了 level，只留档，绝不采用
    hint = obj.get("level")
    if hint is not None:
        lv = clamp_level(hint, default=None)
        if lv is not None:
            verdict["model_level_hint"] = lv
            if lv != level:
                warnings.append(f"模型自己认为 level={lv}，按契约忽略（level 只由规则决定）")
    for extra in ("level_reason", "severity"):
        if extra in obj:
            note = coerce_text(obj.get(extra), 300)
            if note:
                verdict["model_note"] = truncate((model_note + " " + note).strip(), 600)
    if ctx.get("review"):
        verdict["review"] = ctx["review"]
    return scrub_verdict(verdict, ctx)


def inherit_structured_fields(fixed: dict, previous: dict) -> dict:
    """复核未通过后的重写只按旧字段表返回时，不要连带把结构化详情清空。"""
    for key in ("tldr", "tldr_points", "overview", "sections"):
        if not fixed.get(key) and previous.get(key):
            fixed[key] = previous[key]
            fixed.setdefault("warnings", []).append(
                f"复核重写缺少 {key}，已沿用原解读")
    return fixed


def degraded_verdict(reason: str, raw_text: str, ctx: dict) -> dict:
    """两条路都解析失败时的兜底：不崩、可发信、原始输出留档。

    注意：**级别仍然用规则给的**（规则说发就发），只是解说缺失。
    """
    snippet = truncate((raw_text or "").strip().replace("\n", " "), 300)
    snippet, _snippet_hits = scrub_secrets(snippet, ctx.get("auth_secrets") or [])
    level = int(ctx["rule_level"])
    v = normalize_verdict({}, ctx)
    v.update({
        "headline": "检测到变化，本次未生成自动解读",
        "what_changed": "本次变化暂时无法自动说明，请打开原始页面或下方证据核对。",
        "why_matters": "自动解读缺失，影响暂未确认；级别仍由监控规则决定。",
        "action": "打开原始页面核对变化；需要解读时可稍后重试。",
        # 原始输出路径和片段只留在作业目录与 warnings 里，不进入公开事件的 evidence。
        "evidence": [],
        "model_note": "",
        "degraded": True,
    })
    v["warnings"] = [f"模型输出无法解析：{reason}"]
    if snippet:
        v["warnings"].append("原始输出已保留，供内部排查，不对外展示")
    return scrub_verdict(v, ctx)


# --------------------------------------------------------------------------- #
# 跑判读 / 复核
# --------------------------------------------------------------------------- #

def summarize_raw(raw_path: Path, parsed: dict, keep: str) -> int:
    """把 --mode json 的原始事件流瘦身；返回最终落盘字节数。

    原始流里 90% 是 message_update / tool_execution_update 的增量事件（一次判读 0.5~1.5 MB），
    15 分钟一轮巡检、有变化就建一个作业的话，一天能吃掉上百 MB，2C2G 的机器扛不住。
    默认只留一份「摘要 JSONL」（模型/用量/工具调用/最终文本/错误），要查细节用 --raw-keep full。
    """
    if keep == "full" or not raw_path.exists():
        return raw_path.stat().st_size if raw_path.exists() else 0
    if keep == "none":
        try:
            size = raw_path.stat().st_size
            raw_path.unlink()
            return size
        except OSError:
            return 0
    summary = {
        "type": "summary",
        "note": "原始事件流已瘦身；要完整 NDJSON 用 wake.py --raw-keep full 重跑",
        "model": parsed.get("model"),
        "provider": parsed.get("provider"),
        "turns": parsed.get("turns"),
        "tool_calls": parsed.get("tool_calls"),
        "gen_ms": parsed.get("gen_ms"),
        "usage": parsed.get("usage"),
        "events": parsed.get("events"),
        "aborted": parsed.get("aborted"),
        "stop_reason": parsed.get("stop_reason"),
        "raw_bytes": parsed.get("raw_len"),
        "errors": (parsed.get("errors") or [])[:20],
        "final_text": parsed.get("final_text"),
    }
    text = json.dumps(summary, ensure_ascii=False) + "\n"
    _safe_write_text(raw_path, text)
    return len(text.encode("utf-8"))


def run_judge(job_dir: Path, manifest: dict, work: dict, args, prompt: str,
              label: str = "judge", timeout: float | None = None) -> tuple:
    """跑一次 pi，返回 (parsed, info, raw_text)。"""
    call_timeout = float(args.timeout if timeout is None else timeout)
    sid_base = re.sub(r"[^A-Za-z0-9_.-]", "-", str(manifest.get("id", job_dir.name)))
    session_id = f"docwatch-{sid_base}-{label}"
    env = build_env(job_dir, manifest, work, args)
    pi_cmd = pi_argv(prompt, session_id, args, review=(label == "review"))
    unit = re.sub(r"[^A-Za-z0-9_.-]", "-",
                  f"docwatch-{sid_base}-{label}-{int(time.time())}")
    argv, sandbox = build_command(env, pi_cmd, job_dir, args, unit=unit,
                                  auth_root=work.get("auth_root"))
    raw_path = job_dir / ("pi-raw.ndjson" if label == "judge" else f"pi-{label}-raw.ndjson")
    err_path = job_dir / ("pi-stderr.log" if label == "judge" else f"pi-{label}-stderr.log")
    _safe_write_text(
        job_dir / ("pi-prompt.txt" if label == "judge" else f"pi-{label}-prompt.txt"),
        prompt,
    )

    info = run_pi(argv, job_dir, env, raw_path, err_path, call_timeout,
                  unit=unit if sandbox == "systemd" else "", sandbox=sandbox, args=args)
    info.update({"argv": argv, "raw_path": str(raw_path), "sandbox": sandbox,
                 "session_id": session_id})
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.exists() else ""

    # systemd-run 完全起不来（例如被外层 service 限制了 bus / NoNewPrivileges）时，
    # 一行输出都不会有；这时退回普通子进程再试一次，别让整次判读白跑。
    if sandbox == "systemd" and not raw_text and not info.get("timed_out") and args.sandbox_fallback:
        info["sandbox_fallback"] = True
        saved = args.sandbox
        args.sandbox = "none"
        try:
            argv2, sandbox2 = build_command(env, pi_cmd, job_dir, args, unit="",
                                            auth_root=work.get("auth_root"))
        finally:
            args.sandbox = saved
        info2 = run_pi(argv2, job_dir, env, raw_path, err_path, call_timeout,
                       unit="", sandbox="none", args=args)
        info2.update({"argv": argv2, "raw_path": str(raw_path), "sandbox": "none(兜底)",
                      "session_id": session_id, "sandbox_fallback": True,
                      "elapsed_ms": int(info.get("elapsed_ms") or 0) + int(info2.get("elapsed_ms") or 0)})
        info = info2
        raw_text = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.exists() else ""

    info["raw_full_bytes"] = len(raw_text.encode("utf-8", errors="replace"))
    parsed = parse_ndjson(raw_text)
    info["raw_kept_bytes"] = summarize_raw(raw_path, parsed, args.raw_keep)
    info["usage"] = parsed.get("usage") or {}
    info["turns"] = parsed.get("turns")
    info["tool_calls"] = parsed.get("tool_calls") or []
    info["gen_ms"] = int(parsed.get("gen_ms") or 0)
    try:
        out_tokens = int((info["usage"] or {}).get("output") or 0)
        info["tps"] = round(out_tokens / (info["gen_ms"] / 1000), 1) if info["gen_ms"] else None
    except (TypeError, ValueError, ZeroDivisionError):
        info["tps"] = None
    info["model"] = parsed.get("model") or args.model
    if info["timed_out"]:
        info["error"] = f"超时（>{call_timeout:.0f}s）已强杀"
    return parsed, info, raw_text


def run_review(job_dir: Path, manifest: dict, work: dict, args, verdict: dict,
               rule_level: int, timeout: float | None = None) -> dict:
    """只读复核：只查瞎编/抄错/漏关键/答非所问，**不改 level**。"""
    prompt = build_review_prompt(job_dir, manifest, verdict, args, rule_level)
    parsed, info, raw_text = run_judge(job_dir, manifest, work, args, prompt,
                                       label="review", timeout=timeout)
    result = {"pass": None, "reason": "", "elapsed_ms": info.get("elapsed_ms"),
              "rc": info.get("rc"), "timed_out": info.get("timed_out"),
              "usage": info.get("usage")}
    obj, how = extract_json(parsed.get("final_text") or "")
    if not isinstance(obj, dict):
        result["error"] = f"复核输出无法解析：{how}"
        return result
    passed = review_pass(obj.get("pass"))
    if passed is None:
        result["error"] = "复核输出缺少有效的 pass 字段，视为未完成复核"
    else:
        result["pass"] = passed
    result["reason"] = coerce_text(obj.get("reason"), 600)
    if info.get("timed_out"):
        result["pass"] = None
        result["error"] = "复核超时，视为未复核"
    return result


def apply_review(verdict: dict, review: dict) -> dict:
    """把复核结果挂到 verdict 上（只记录，不改 level）。"""
    verdict["review"] = {k: review.get(k) for k in
                         ("pass", "reason", "elapsed_ms", "error")}
    if review.get("pass") is None:
        verdict["warnings"].append("复核未完成：" + str(review.get("error") or "未知原因"))
    elif review.get("pass"):
        verdict["warnings"].append("复核通过：" + (review.get("reason") or ""))
    else:
        verdict["warnings"].append("复核未通过：" + (review.get("reason") or ""))
    return verdict


def _add_usage(acc: dict, usage: dict) -> None:
    for k in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"):
        try:
            acc[k] = int(acc.get(k, 0) or 0) + int(usage.get(k) or 0)
        except (TypeError, ValueError):
            pass
    try:
        acc["cost"] = float(acc.get("cost", 0.0) or 0.0) + float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        pass


def _tokens_dict(usage: dict) -> dict:
    return {
        "input": usage.get("input", 0),
        "output": usage.get("output", 0),
        "cache_read": usage.get("cacheRead", 0),
        "cache_write": usage.get("cacheWrite", 0),
        "reasoning": usage.get("reasoning", 0),
        "total": usage.get("totalTokens", 0),
        "cost": usage.get("cost", 0.0),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m docwatch.wake",
        description="唤醒 pi agent 调查 + 解说一次文档变化（level 由规则给），产出 verdict.json")
    ap.add_argument("--job", help="作业目录，或作业 id（配 --data-dir）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"数据目录（默认 {DEFAULT_DATA_DIR}）")
    ap.add_argument("--rule-level", type=int, choices=(0, 1, 2, 3),
                    help="规则判定的级别（rules.py 给的；wake.py 不采用模型意见）")
    ap.add_argument("--rule-file", help="规则结果 JSON 文件（含 level 字段，例如 <job>/rule.json）")
    ap.add_argument("--mode", choices=("high", "normal", "quiet"), help="覆盖 manifest 里的档位")
    ap.add_argument("--thinking", choices=("off", "minimal", "low", "medium", "high"),
                    help="pi 思考档位（默认 high；显式指定可覆盖）")
    ap.add_argument("--review", action="store_true", help="rule level>=2 时跑第二遍只读复核（不改级别）")
    ap.add_argument("--dry", action="store_true", help="不调 pi，只打印将发送的提示词")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 Verdict JSON")
    ap.add_argument("--out", help="verdict.json 输出路径（默认 <job>/verdict.json）")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="单次 pi 调用超时秒数（默认 300；超时强杀，仍会产出兜底结论）")
    ap.add_argument("--budget", type=float, default=300.0,
                    help="整次唤醒的耗时预算秒数（默认 300）：超了就不再跑复核/重写，保证邮件及时发出")
    ap.add_argument("--pi", default=DEFAULT_PI, help=f"pi 可执行文件（默认 {DEFAULT_PI}）")
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL,
                    help="主模型失败后的备用模型（默认关闭；路由组内部已按顺序切换）")
    ap.add_argument("--agent-src", default=DEFAULT_AGENT_SRC,
                    help="pi 的 agent 配置目录（复制其中的 models/mcp/auth）")
    ap.add_argument("--agent-md", help="AGENT.md 路径（默认与本文件同目录）")
    ap.add_argument("--toolbox", help="toolbox.py 路径（默认与本文件同目录）")
    ap.add_argument("--sandbox", choices=("auto", "systemd", "ulimit", "none"), default="auto")
    ap.add_argument("--pi-user", default=DEFAULT_PI_USER,
                    help=f"运行 pi 的用户（默认 {DEFAULT_PI_USER}；空=不降权）")
    ap.add_argument("--pi-group", default=DEFAULT_PI_GROUP,
                    help=f"运行 pi 的组（默认 {DEFAULT_PI_GROUP}）")
    ap.add_argument("--sandbox-fallback", dest="sandbox_fallback", action="store_true",
                    default=False,
                    help="systemd 围栏起不来时退回普通子进程（默认关闭：失败即用规则文案，"
                         "不让判读在无文件系统隔离下运行；仅调试用）")
    ap.add_argument("--memory-max", default="400M")
    ap.add_argument("--memory-high", default="280M")
    ap.add_argument("--cpu-quota", default="80%")
    ap.add_argument("--ulimit-vm-mb", default="1024", help="sandbox=ulimit 时的 RLIMIT_AS（MB）")
    ap.add_argument("--private-tmp", action="store_true",
                    help="systemd 围栏里再套 PrivateTmp（作业目录在 /tmp 下时会冲突，别开）")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS,
                    help=f"调查步数上限（默认 {MAX_STEPS}）")
    ap.add_argument("--raw-keep", choices=("summary", "full", "none"), default="summary",
                    help="原始事件流怎么留：summary=只留摘要（默认，约 5KB/次）；"
                         "full=完整 NDJSON（0.5~1.5MB/次，调试用）；none=不留")
    ap.add_argument("--keep-session", action="store_true",
                    help="保留 pi 的会话文件（默认不留：一次 2~4 个文件、共约 50~170KB）")
    ap.add_argument("--cleanup", action="store_true",
                    help="跑完删掉作业目录里的 .home / .pi-agent（调试配置，不该跟证据混在一起）")
    ap.add_argument("--parse-file", help="调试：把已有 NDJSON 解析成 Verdict（不调模型）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    job_dir = resolve_job_dir(args)
    manifest = load_manifest(job_dir)
    if args.mode:
        manifest["mode"] = args.mode
    rule_level, level_source, level_warnings = resolve_rule_level(job_dir, manifest, args)
    args.thinking = pick_thinking(args)
    rule_noise = getattr(args, "_rule_noise", None)
    if rule_noise is None:
        rule_noise = (rule_level == 0)

    # 1) 只打印提示词
    if args.dry:
        prompt = build_judge_prompt(job_dir, manifest, args, rule_level)
        print(prompt)
        for w in level_warnings:
            print(f"# wake 警告：{w}", file=sys.stderr)
        print(f"# 规则级别：{rule_level}（{LEVEL_LABELS.get(rule_level)}） 来源：{level_source}",
              file=sys.stderr)
        if args.review:
            sample = {"headline": "（示例）高峰输出价 13.5 → 15 元/百万 tokens",
                      "what_changed": "（示例）pricing 页输出价一行变化",
                      "why_matters": "（示例）按量成本 +11%",
                      "action": "（示例）无需操作", "evidence": ["（示例）diff/pricing.diff 第 3 行"],
                      "model_note": ""}
            rp = build_review_prompt(job_dir, manifest, sample, args, rule_level)
            print("# ---- 复核（第二遍，只读）提示词预览 ----\n" + rp, file=sys.stderr)
        if args.verbose:
            work = {"home": job_dir / ".home", "agent_dir": job_dir / ".pi-agent"}
            env = build_env(job_dir, manifest, work, args)
            pi_cmd = pi_argv("<提示词>", "docwatch-dry", args)
            argv, sandbox = build_command(env, pi_cmd, job_dir, args, unit="docwatch-dry")
            print("\n--- 将执行的命令（dry，未执行）---", file=sys.stderr)
            print("sandbox:", sandbox, file=sys.stderr)
            print(" ".join(argv), file=sys.stderr)
        return 0

    # 2) 离线解析已有输出（调试解析器）
    if args.parse_file:
        raw_text = Path(args.parse_file).read_text(encoding="utf-8", errors="replace")
        parsed = parse_ndjson(raw_text)
        obj, how = extract_json(parsed.get("final_text") or "")
        if obj is not None and not _usable(obj):
            obj, how = None, "输出是空 JSON 对象，没有任何可用字段"
        ctx = {"model": parsed.get("model") or args.model, "elapsed_ms": 0,
               "job_id": manifest.get("id"), "thinking": args.thinking,
               "tokens": _tokens_dict(parsed.get("usage") or {}), "rule_level": rule_level,
               "noise": rule_noise}
        if obj is None:
            verdict = degraded_verdict(how, parsed.get("final_text") or raw_text, ctx)
        else:
            verdict = normalize_verdict(obj, ctx)
            verdict["parse"] = how
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0

    work = prepare_workspace(job_dir, manifest, args)
    _install_cleanup(job_dir, work, args)
    prompt = build_judge_prompt(job_dir, manifest, args, rule_level)
    log_lines = [f"[{now_iso()}] wake job={manifest.get('id')} dir={job_dir} "
                 f"rule_level={rule_level}({level_source}) thinking={args.thinking} "
                 f"timeout={args.timeout}s sandbox={args.sandbox}"]

    t_all = time.monotonic()
    phase_ms = {"judge": 0, "review": 0, "rejudge": 0}
    out_path = Path(args.out) if args.out else (job_dir / "verdict.json")

    def remaining_budget() -> float:
        return max(0.0, float(args.budget) - (time.monotonic() - t_all))

    def budget_left() -> bool:
        return remaining_budget() > 5.0

    def next_timeout() -> float:
        """单次调用不能超过总预算剩余（也别把外层 780s 撑爆）。"""
        return max(5.0, min(float(args.timeout), remaining_budget()))

    def write_draft(v: dict) -> None:
        """解析出可用结论后立刻落一版：外层超时被 kill 也能读到模型解读。"""
        draft = dict(v)
        draft["draft"] = True
        try:
            write_json(out_path, draft)
        except OSError as exc:
            print(f"wake: 警告：draft verdict 写入失败：{exc}", file=sys.stderr)

    primary_model = args.model
    used_model = primary_model
    fallback_used = False
    parsed, info, raw_text = run_judge(job_dir, manifest, work, args, prompt,
                                       label="judge", timeout=next_timeout())
    attempts = 1
    phase_ms["judge"] += int(info.get("elapsed_ms") or 0)
    obj, how = extract_json(parsed.get("final_text") or "")
    if obj is not None and not _usable(obj):
        obj, how = None, "输出是空 JSON 对象，没有任何可用字段"
    total_usage = dict(parsed.get("usage") or {})
    log_lines.append(f"judge#1 rc={info['rc']} elapsed={info['elapsed_ms']}ms "
                     f"ttft={info.get('ttft_ms')}ms tps={info.get('tps')} "
                     f"turns={info.get('turns')} tools={info.get('tool_calls')} parse={how}")

    # 3) 解析失败 → 更严厉的提示词重试一次（先看预算，慢轮次不能把外层超时撑爆）
    if obj is None and not budget_left():
        log_lines.append("judge-retry skipped: budget")
    elif obj is None:
        retry_prompt = prompt + "\n" + STRICT_RETRY_NOTE.format(
            fields=", ".join(MODEL_FIELDS), task=prompt)
        parsed2, info2, raw2 = run_judge(job_dir, manifest, work, args, retry_prompt,
                                         label="judge-retry", timeout=next_timeout())
        attempts = 2
        phase_ms["judge"] += int(info2.get("elapsed_ms") or 0)
        _add_usage(total_usage, parsed2.get("usage") or {})
        obj, how2 = extract_json(parsed2.get("final_text") or "")
        if obj is not None and not _usable(obj):
            obj, how2 = None, "重试后仍是空 JSON 对象"
        log_lines.append(f"judge#2 rc={info2['rc']} elapsed={info2['elapsed_ms']}ms "
                         f"ttft={info2.get('ttft_ms')}ms tps={info2.get('tps')} parse={how2}")
        info = dict(info2)
        parsed = parsed2
        raw_text = raw2 if obj is not None else (raw_text + "\n" + raw2)
        how = how2

    # 3b) 主模型仍没给出可用结论 → 换备用模型再试一次。
    # 这里让 args.model 真正变成备用模型，pi 的模型身份提示也会跟着变，
    # 不会出现「实际跑 DeepSeek，模型却以为自己在 Example Model」的情况。
    if obj is None and args.fallback_model and args.fallback_model != primary_model:
        if not budget_left():
            log_lines.append("fallback skipped: budget")
        else:
            fallback_prompt = prompt + "\n" + STRICT_RETRY_NOTE.format(
                fields=", ".join(MODEL_FIELDS), task=prompt)
            saved_model = args.model
            args.model = args.fallback_model
            try:
                parsed4, info4, raw4 = run_judge(job_dir, manifest, work, args,
                                                 fallback_prompt, label="judge-fallback",
                                                 timeout=next_timeout())
            finally:
                args.model = saved_model
            attempts += 1
            phase_ms["judge"] += int(info4.get("elapsed_ms") or 0)
            _add_usage(total_usage, parsed4.get("usage") or {})
            obj4, how4 = extract_json(parsed4.get("final_text") or "")
            if obj4 is not None and not _usable(obj4):
                obj4, how4 = None, "备用模型输出是空 JSON 对象"
            log_lines.append(f"judge#fallback rc={info4['rc']} "
                             f"elapsed={info4['elapsed_ms']}ms "
                             f"ttft={info4.get('ttft_ms')}ms tps={info4.get('tps')} "
                             f"model={args.fallback_model} parse={how4}")
            raw_text = raw_text + "\n" + raw4
            if obj4 is not None:
                obj, how = obj4, how4
                info, parsed = dict(info4), parsed4
                used_model = args.fallback_model
                fallback_used = True
            else:
                how = f"主模型和备用模型都没给出可解析结论：{how4}"
                parsed = parsed4
                log_lines.append("fallback failed")

    if fallback_used:
        args.model = used_model

    tokens = _tokens_dict(total_usage)
    ctx = {"model": info.get("model") or used_model,
           "elapsed_ms": phase_ms["judge"],
           "job_id": manifest.get("id", job_dir.name),
           "thinking": args.thinking, "tokens": tokens, "attempts": attempts,
           "model_fallback_from": primary_model if fallback_used else "",
           "ttft_ms": info.get("ttft_ms"), "first_text_ms": info.get("first_text_ms"),
           "gen_ms": info.get("gen_ms"), "tps": info.get("tps"),
           "raw_path": info.get("raw_path", ""), "rule_level": rule_level,
           "noise": rule_noise, "auth_secrets": load_auth_secrets(work)}

    if obj is not None:
        verdict = normalize_verdict(obj, ctx)
        verdict["parse"] = how
        if fallback_used:
            verdict["warnings"].append(
                f"主模型 {primary_model} 未给出可用结论，已自动切换 {used_model}")
    else:
        verdict = degraded_verdict(how, parsed.get("final_text") or raw_text, ctx)
        verdict["parse"] = "failed"
        log_lines.append(f"降级：{how}")

    if info.get("sandbox_fallback"):
        verdict["warnings"].append("systemd 围栏不可用，已退回普通子进程（仅降权运行）")
    # 增量落盘：后面的复核/重写哪怕被外层超时杀掉，run.py 也能读到这份模型解读。
    write_draft(verdict)

    # 4) 复核（只在规则 level >= 2 且开了 --review；只查瞎编，不改级别）
    if args.review and rule_level >= 2 and not budget_left():
        verdict["warnings"].append(f"总耗时预算 {args.budget:.0f}s 已到，跳过复核")
        log_lines.append("review skipped: budget")
    elif args.review and rule_level >= 2:
        review = run_review(job_dir, manifest, work, args, verdict, rule_level,
                            timeout=next_timeout())
        phase_ms["review"] += int(review.get("elapsed_ms") or 0)
        log_lines.append(f"review pass={review.get('pass')} elapsed={review.get('elapsed_ms')}ms "
                         f"reason={truncate(review.get('reason'), 140)}")
        verdict = apply_review(verdict, review)

        # 复核没过（瞎编/抄错）→ 带着复核意见重写一次解读（级别不变），然后再复核一次
        if review.get("pass") is False and not budget_left():
            verdict["review"]["applied"] = "flag"
            verdict["warnings"].append(f"总耗时预算 {args.budget:.0f}s 已到，跳过重写")
            log_lines.append("rejudge skipped: budget")
        elif review.get("pass") is False:
            feedback = (
                "\n\n【复核员的意见：以下内容与证据不符，必须修正（级别不变，规则仍判 {}-{}）】\n"
                "{}\n\n"
                "处理要求：\n"
                "1. 复核员指出「证据里找不到 / 抄错」的句子，从 what_changed、why_matters、"
                "evidence 里删掉，或改成与文件实际内容逐字一致的表述；\n"
                "2. 不要新增任何证据里没有的数字、字节数、参数名、示例；\n"
                "3. evidence 只保留你能逐字核对到的行（写清文件名 + 该行内容）；\n"
                "4. 最后仍然只输出那一个 JSON 对象（headline/tldr/tldr_points/overview/"
                "sections/what_changed/why_matters/action/evidence/model_note），不要解释。"
                "如果原解读里已有 tldr/tldr_points/overview/sections，重写时必须保留并修正，"
                "不能省略成空值。"
            ).format(rule_level, LEVEL_LABELS.get(rule_level, ""), review.get("reason") or "")
            parsed3, info3, _raw3 = run_judge(job_dir, manifest, work, args,
                                              prompt + feedback, label="judge-rejudge",
                                              timeout=next_timeout())
            phase_ms["rejudge"] += int(info3.get("elapsed_ms") or 0)
            obj3, how3 = extract_json(parsed3.get("final_text") or "")
            if obj3 is not None and not _usable(obj3):
                obj3, how3 = None, "重写后仍是空 JSON 对象"
            attempts += 1
            _add_usage(total_usage, parsed3.get("usage") or {})
            if obj3 is not None:
                ctx2 = dict(ctx)
                ctx2.update({"attempts": attempts, "tokens": _tokens_dict(total_usage)})
                fixed = inherit_structured_fields(normalize_verdict(obj3, ctx2), verdict)
                prev_review = dict(verdict.get("review") or {})
                fixed["review"] = dict(prev_review, applied="rejudge", rejudged=True, rounds=1)
                fixed["warnings"].append("复核未通过，已按复核意见重写解读")
                verdict = fixed
                log_lines.append(f"rejudge rc={info3['rc']} parse={how3}")

                # 重写后再复核一次（第二轮，也是最后一轮）：确认瞎编真的改掉了
                if budget_left():
                    review2 = run_review(job_dir, manifest, work, args, verdict, rule_level,
                                         timeout=next_timeout())
                    phase_ms["review"] += int(review2.get("elapsed_ms") or 0)
                    verdict["review"].update({
                        "rounds": 2,
                        "final_pass": review2.get("pass"),
                        "final_reason": review2.get("reason"),
                        "elapsed_ms": (prev_review.get("elapsed_ms") or 0)
                                      + (review2.get("elapsed_ms") or 0),
                    })
                    log_lines.append(f"review#2 pass={review2.get('pass')} "
                                     f"elapsed={review2.get('elapsed_ms')}ms")
                    if review2.get("pass") is False:
                        verdict["needs_human"] = True
                        verdict["warnings"].append(
                            "重写后复核仍不通过（可能仍有与证据不符的表述）："
                            + (review2.get("reason") or ""))
                    elif review2.get("pass"):
                        verdict["warnings"].append("重写后复核通过")
                else:
                    verdict["warnings"].append(
                        f"总耗时预算 {args.budget:.0f}s 已到，跳过第二轮复核")
            else:
                verdict["review"]["applied"] = "flag"
                verdict["warnings"].append("复核未通过，但重写输出无法解析，保留原解读")
                log_lines.append(f"rejudge 解析失败（{how3}），保留原解读")
        elif review.get("pass"):
            verdict["review"]["applied"] = "none"

    verdict["elapsed_ms"] = int((time.monotonic() - t_all) * 1000)
    verdict["judge_ms"] = phase_ms["judge"]
    if phase_ms["review"]:
        verdict["review_ms"] = phase_ms["review"]
    verdict["level_source"] = "rule"
    verdict["level"] = rule_level
    verdict["level_label"] = LEVEL_LABELS.get(rule_level, "")
    verdict["noise"] = bool(rule_noise)

    # 5) 落盘（最终版；draft 标记只在进程被超时打断时供 run.py 读取）
    verdict.pop("draft", None)
    write_json(out_path, verdict)
    meta = {
        "job_id": manifest.get("id", job_dir.name),
        "job_dir": str(job_dir),
        "created_at": now_iso(),
        "mode": manifest.get("mode"),
        "thinking": args.thinking,
        "model": ctx["model"],
        "requested_model": primary_model,
        "used_model": used_model,
        "fallback_model": args.fallback_model,
        "model_fallback_from": primary_model if fallback_used else "",
        "provider": args.provider,
        "rule_level": rule_level,
        "rule_level_source": level_source,
        "rule_level_warnings": level_warnings,
        "attempts": attempts,
        "elapsed_ms": verdict["elapsed_ms"],
        "phase_ms": phase_ms,
        "ttft_ms": info.get("ttft_ms"),
        "first_text_ms": info.get("first_text_ms"),
        "gen_ms": info.get("gen_ms"),
        "tps": info.get("tps"),
        "pi_rc": info.get("rc"),
        "timed_out": info.get("timed_out"),
        "sandbox": info.get("sandbox"),
        "sandbox_fallback": bool(info.get("sandbox_fallback")),
        "session_id": info.get("session_id"),
        "tokens": verdict.get("tokens"),
        "tool_calls": info.get("tool_calls"),
        "turns": info.get("turns"),
        "raw": info.get("raw_path"),
        "raw_keep": args.raw_keep,
        "raw_full_bytes": info.get("raw_full_bytes"),
        "raw_kept_bytes": info.get("raw_kept_bytes"),
        "verdict_path": str(out_path),
        "degraded": verdict.get("degraded"),
        "needs_human": verdict.get("needs_human", False),
        "review": verdict.get("review"),
        "limits": {"memory_max": args.memory_max, "memory_high": args.memory_high,
                   "cpu_quota": args.cpu_quota, "timeout_s": args.timeout,
                   "budget_s": args.budget, "max_steps": args.max_steps},
        "argv": info.get("argv") if args.verbose else None,
    }
    write_json(job_dir / "wake.json", meta)
    log_lines.append(f"verdict level={verdict['level']}(rule,{level_source}) "
                     f"headline={truncate(verdict['headline'], 80)} -> {out_path}")
    _safe_write_text(job_dir / "wake.log", "\n".join(log_lines) + "\n", append=True)

    if args.cleanup:
        # .home / .pi-agent 是给 pi 跑一次用的，不该跟证据混在一起
        cleanup_workspace(job_dir, work, remove_config=True)
    else:
        # 即使保留配置目录，凭据副本也必须立刻删除
        cleanup_workspace(job_dir, work, remove_config=False)

    # 6) 输出
    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        lv = verdict["level"]
        print(f"[docwatch] level={lv}（{LEVEL_LABELS[lv]}，来源 {verdict['level_source']}） "
              f"noise={verdict['noise']} degraded={verdict.get('degraded', False)}")
        print(f"  headline: {verdict['headline']}")
        if verdict.get("what_changed"):
            print(f"  changed:  {truncate(verdict['what_changed'], 220)}")
        if verdict.get("why_matters"):
            print(f"  why:      {truncate(verdict['why_matters'], 200)}")
        if verdict.get("model_note"):
            print(f"  note:     {truncate(verdict['model_note'], 160)}")
        t = verdict.get("tokens") or {}
        print(f"  model={ctx['model']} elapsed={verdict['elapsed_ms']}ms "
              f"ttft={verdict.get('ttft_ms')}ms tps={verdict.get('tps')} "
              f"tokens={t.get('total', 0)}（in {t.get('input', 0)}/cache {t.get('cache_read', 0)}/"
              f"out {t.get('output', 0)}）cost={t.get('cost', 0)} attempts={verdict['attempts']}")
        if verdict.get("review"):
            r = verdict["review"]
            print(f"  review: pass={r.get('pass')} applied={r.get('applied', '-')} "
                  f"rounds={r.get('rounds', 1)} final_pass={r.get('final_pass', '-')} "
                  f"reason={truncate(r.get('reason'), 120)}")
        if verdict.get("needs_human"):
            print("  !! needs_human：重写后复核仍不通过，建议人工扫一眼证据")
        for w in verdict.get("warnings") or []:
            print(f"  ! {w}")
        print(f"  verdict -> {out_path}")

    return 0


def main(argv=None) -> int:
    """入口包装：try/finally 确保异常退出也会清掉凭据与一次性工作区。"""
    try:
        return _main(argv)
    finally:
        _cleanup_all()


if __name__ == "__main__":
    sys.exit(main())
