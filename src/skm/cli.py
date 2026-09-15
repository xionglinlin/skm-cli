"""命令行入口：list / status / import / enable / disable / agents / config / issues。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import NoReturn

from . import __version__, actions, config as config_mod, importer, scan
from .model import LinkState, Skill

EXIT_OK = 0
EXIT_PROBLEM = 1   #: 有操作被拒绝或失败
EXIT_USAGE = 2     #: 用法 / 查询无结果

MARK_ENABLED = "✓"
MARK_DISABLED = "·"
MARK_PARTIAL = "~"
MARK_UNKNOWN = "?"

# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


class Style:
    """终端着色。管道、重定向、NO_COLOR 时自动关闭 —— 输出要能被 grep。"""

    def __init__(self, stream) -> None:
        self.on = (
            stream.isatty()
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb"
        )

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def _mark(style: Style, snapshot: scan.Snapshot, skill: Skill) -> tuple[str, str]:
    """返回 ``(标记, 说明)``。说明里写清是哪些 agent —— 共享目录下"启用"不止一个读者。"""
    statuses = snapshot.links_of(skill)
    good = [s for s in statuses if s.state is LinkState.LINKED]
    total = len(statuses)
    if total == 0:
        return MARK_UNKNOWN, style.dim("没有可写目标")
    if len(good) == total:
        return MARK_ENABLED, style.green("、".join(s.agent_id for s in good))
    if not good:
        return MARK_DISABLED, style.dim("未启用")
    return MARK_PARTIAL, style.yellow(
        f"部分启用：{('、'.join(s.agent_id for s in good))}（共 {total} 个目标）"
    )


def _continuation(branch: str) -> str:
    """把枝干占位换成等宽空格 —— 后续行（告警）不该继续画树枝。

    每个制表字形（``├``/``└``/``│``/``─``/空格）都是 1 列，所以直接按长度补空格。
    """
    return " " * len(branch)


def _skill_line(
    style: Style, snapshot: scan.Snapshot, skill: Skill, name_width: int, stem: str = ""
) -> str:
    """一行 skill 摘要。``stem`` 是把后续告警行也对齐到同一列用的缩进。"""
    mark, note = _mark(style, snapshot, skill)
    line = f"{mark} {skill.dirname.ljust(name_width)}  {note}"
    if skill.name != skill.dirname:
        line += style.dim(f"  [name: {skill.name}]")
    for warning in skill.warnings:
        line += "\n" + stem + style.dim(f"    ⚠ {warning}")
    return line


# --------------------------------------------------------------------------
# 选择与展示
# --------------------------------------------------------------------------


def _select(snapshot: scan.Snapshot, queries: list[str], args) -> list:
    """按查询、分类与状态过滤出目标 skill。查询无匹配即整体报错，不静默降级。"""
    if queries:
        chosen: list = []
        for query in queries:
            hits = scan.resolve(snapshot, query)
            if not hits:
                print(f"没有匹配的 skill：{query}", file=sys.stderr)
                raise SystemExit(EXIT_USAGE)
            chosen.extend(hits)
        skills = list(dict.fromkeys(chosen))
    else:
        skills = list(snapshot.skills)

    category = getattr(args, "category", None)
    if category:
        skills = [s for s in skills if (s.category or "") == category]

    if getattr(args, "enabled", False):
        skills = [s for s in skills if snapshot.enabled(s)]
    if getattr(args, "disabled", False):
        skills = [s for s in skills if not snapshot.enabled(s)]
    return sorted(skills, key=lambda s: ((s.category or ""), s.dirname))


def _resolve_targets(snapshot: scan.Snapshot, args) -> list:
    """决定写哪些 agent 目录。``--agent`` 可重复；缺省 = 全部可写目标。"""
    wanted = getattr(args, "agent", None) or []
    if not wanted:
        return snapshot.config.link_targets()
    targets = []
    for agent_id in wanted:
        agent = snapshot.config.agent(agent_id)
        if agent is None:
            known = "、".join(a.id for a in snapshot.config.agents) or "（无）"
            print(f"没有这个 agent：{agent_id}（可用：{known}）", file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
        if not agent.enabled:
            print(f"agent 已在配置里停用：{agent_id}", file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
        targets.append(agent)
    return targets


def _print_outcomes(style: Style, outcomes: list[actions.Outcome]) -> int:
    labels = {
        actions.Result.CREATE: style.green("已启用"),
        actions.Result.REMOVE: style.green("已停用"),
        actions.Result.ALREADY: style.dim("未改动"),
        actions.Result.SKIPPED: style.yellow("跳过"),
        actions.Result.REFUSED: style.red("拒绝"),
        actions.Result.FAILED: style.red("失败"),
    }
    problems = 0
    for outcome in outcomes:
        # 标签用全角空格（2 列）补齐：中文标签宽度一致，斜体/颜色不影响列对齐
        print(f"  {labels[outcome.result]}　{outcome.agent_id}  {outcome.path}")
        if outcome.detail:
            print(style.dim(f"        {outcome.detail}"))
        if outcome.result in (actions.Result.REFUSED, actions.Result.FAILED):
            problems += 1
    return problems


def _emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def snapshot_payload(snapshot: scan.Snapshot) -> dict:
    return {
        "store": str(snapshot.store_dir),
        "agents": [
            {"id": a.id, "name": a.name, "dir": str(a.dir), "shared": a.shared,
             "enabled": a.enabled, "present": a.present}
            for a in snapshot.config.agents
        ],
        "skills": [
            {
                "id": skill.id,
                "name": skill.name,
                "dirname": skill.dirname,
                "category": skill.category,
                "description": skill.description,
                "path": str(skill.dir_path),
                "enabled": snapshot.enabled(skill),
                "warnings": skill.warnings,
                "links": [
                    {
                        "agent": status.agent_id,
                        "path": str(status.link_path),
                        "state": status.state.value,
                        "target": status.target,
                        "resolved": status.resolved,
                    }
                    for status in snapshot.links_of(skill)
                ],
            }
            for skill in snapshot.skills
        ],
        "orphans": [
            {"agent": o.agent_id, "path": str(o.link_path), "target": o.target,
             "broken": o.broken}
            for o in snapshot.orphans
        ],
        "conflicts": [
            {"kind": c.kind, "agent": c.agent_id, "path": str(c.path),
             "skill": c.skill, "message": c.message}
            for c in snapshot.conflicts
        ],
        "warnings": snapshot.warnings,
    }


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_list(args) -> int:
    style = Style(sys.stdout)
    snapshot = scan.scan(config_mod.load())
    skills = _select(snapshot, args.query, args)

    if args.json:
        payload = snapshot_payload(snapshot)
        payload["skills"] = [s for s in payload["skills"]
                             if s["id"] in {sk.id for sk in skills}]
        _emit_json(payload)
        return EXIT_OK

    print(style.dim(f"仓库：{_tilde(snapshot.store_dir)}"))
    if not skills:
        if snapshot.skills:
            print(style.yellow("没有可显示的 skill"))
        else:
            # 空仓库是新用户的第一站：这里必须给出可执行的下一步，而不是一句"没有"。
            # 两种情况都覆盖到 —— 手动拷贝是常规做法，import 是省事的做法。
            print(style.yellow("仓库里还没有 skill"))
            print(style.dim(f"  放进去：{_tilde(snapshot.store_dir)}/<名字>/SKILL.md"
                            f"（分类 = 中间加一层目录）"))
            print(style.dim("  或扫描已有 skill（会搬进仓库并回填链接）：skm import"))
        return EXIT_OK

    width = min(max((len(s.dirname) for s in skills), default=10), 40)
    grouped = snapshot.categories()
    chosen = {s.id for s in skills}
    enabled_total = sum(1 for s in snapshot.skills if snapshot.enabled(s))

    # 树的根层：未分类的 skill 各自是一根枝条，分类目录是一根枝条带子项。
    # 先摊平成一个列表，再统一用"最后一个"决定收尾字形，避免两套缩进逻辑。
    roots: list[tuple[str, Skill | None, list[Skill]]] = []
    for skill in grouped.get("", []):
        if skill.id in chosen:
            roots.append(("skill", skill, []))
    for category in sorted(key for key in grouped if key):
        members = [s for s in grouped[category] if s.id in chosen]
        if members:
            roots.append(("category", None, members))

    for index, (kind, leaf, members) in enumerate(roots):
        last_root = index == len(roots) - 1
        branch = "└── " if last_root else "├── "
        stem = "    " if last_root else "│   "

        if kind == "skill" and leaf is not None:
            print(branch + _skill_line(style, snapshot, leaf, width,
                                       _continuation(branch)))
            continue

        applied = sum(1 for s in members if snapshot.enabled(s))
        print(style.bold(branch + style.cyan(f"{members[0].category}/"))
              + style.dim(f"  {len(members)} 个 · 已启用 {applied}"))
        for position, skill in enumerate(members):
            glyph = "└── " if position == len(members) - 1 else "├── "
            print(stem + glyph
                  + _skill_line(style, snapshot, skill, width,
                                _continuation(stem + glyph)))

    print()
    if len(skills) == len(snapshot.skills):
        print(f"共 {len(snapshot.skills)} 个 skill，已启用 {enabled_total}，"
              f"未启用 {len(snapshot.skills) - enabled_total}")
    else:
        active = sum(1 for s in skills if snapshot.enabled(s))
        print(f"筛出 {len(skills)} 个 skill，已启用 {active}，未启用 {len(skills) - active}")
    print(style.dim("✓ 已启用   ~ 部分启用   · 未启用"))
    return EXIT_OK


def cmd_status(args) -> int:
    style = Style(sys.stdout)
    snapshot = scan.scan(config_mod.load())
    skills = _select(snapshot, args.query, args)

    if args.json:
        payload = snapshot_payload(snapshot)
        chosen = {sk.id for sk in skills}
        _emit_json({
            "skills": [s for s in payload["skills"] if s["id"] in chosen],
            "orphans": payload["orphans"],
            "conflicts": payload["conflicts"],
        })
        return EXIT_OK

    if not skills and not args.query:
        if snapshot.skills:
            # 仓库里有东西，只是被 --enabled / --disabled / --category 滤空了
            print(style.yellow("没有符合筛选条件的 skill"))
            return EXIT_OK
        print(style.yellow(f"仓库里还没有 skill：{_tilde(snapshot.store_dir)}"))
        print(style.dim("  放进去：<仓库>/<名字>/SKILL.md；"
                        "或扫描已有 skill：skm import"))
        return EXIT_OK

    for skill in skills:
        mark, note = _mark(style, snapshot, skill)
        print(f"{mark} {style.bold(skill.id)}  {note}")
        if skill.description:
            print(style.dim(f"  {_clip(skill.description, _width() - 4)}"))
        # name 只在"与目录名不一致"时才有信息量 —— 那时它正是 OMP 判定身份用的名字，
        # 而上面显示的 id 是目录名。一致时不再重复一行。
        if skill.name != skill.dirname:
            print(style.dim(f"  name {skill.name}"))
        print(style.dim(f"  {_tilde(skill.dir_path)}"))
        _print_links(style, snapshot, skill)
        for warning in skill.warnings:
            print(style.yellow(f"  ⚠ {warning}"))
        print()

    if snapshot.orphans:
        print(style.bold(style.red("残留链接（指向仓库，但仓库里没有对应 skill）")))
        for orphan in snapshot.orphans:
            state = "断链" if orphan.broken else "有效"
            note = _conflict_note(snapshot.conflicts, orphan.link_path)
            print(f"  {orphan.agent_id:6} {_tilde(orphan.link_path)}  [{state}]")
            print(style.dim(f"         → {orphan.target}{note}"))
        print(style.dim("  清理：skm issues --fix"))
        print()

    # 冲突分三类，各自出现在最合适的位置，不重复：
    #   1. 位置就是**选中 skill** 的链接位 —— 上面逐条状态已如实显示，这里只报数；
    #   2. 仓库内部的重名（与具体位置无关）—— 在这里列出来；
    #   3. 属于本次没选中的 skill —— 只提示还有多少，不喧宾夺主。
    shown_paths = {st.link_path for skill in skills for st in snapshot.links_of(skill)}
    inside_listing = [c for c in snapshot.conflicts if c.path in shown_paths]
    repo_wide = [c for c in snapshot.conflicts if not c.agent_id]
    elsewhere = [c for c in snapshot.conflicts
                 if c.agent_id and c.path not in shown_paths]

    if repo_wide:
        print(style.bold(style.red("冲突（不会被自动覆盖）")))
        for conflict in repo_wide:
            print(f"  [{conflict.kind}] {conflict.path}")
            print(style.dim(f"      {conflict.message}"))
        print()

    notes = []
    if inside_listing:
        notes.append(f"{len(inside_listing)} 处冲突已在上面标出（详见 skm issues）")
        # "别处还有冲突"只在本次结果**本身**已经出了问题才提：否则查一个干净的
        # skill 也会被无关的告警打扰，反而让人以为它有问题。
        if elsewhere:
            notes.append(f"另有 {len(elsewhere)} 处冲突在未列出的 skill 上")
    for note in notes:
        for line in _wrap(note, _width() - 2):
            print(style.dim(f"  {line}"))
    if notes:
        print()

    for warning in snapshot.warnings:
        print(style.yellow(f"⚠ {warning}"), file=sys.stderr)

    selected = {s.id for s in skills}
    enabled = sum(1 for s in skills if snapshot.enabled(s))
    print(f"合计：{len(skills)} 个（已启用 {enabled}，未启用 {len(skills) - enabled}）")
    if len(selected) < len(snapshot.skills):
        print(style.dim(f"（仓库共 {len(snapshot.skills)} 个）"))
    return EXIT_OK


def _width(default: int = 100) -> int:
    """输出宽度上限。终端更窄时以终端为准 —— 换行交给终端会破坏缩进对齐。"""
    try:
        return min(shutil.get_terminal_size().columns, default) - 1
    except OSError:
        return default - 1


def _tilde(path: Path) -> str:
    """``$HOME`` 缩写成 ``~``：状态行的主体是位置，长前缀只会挤掉有用信息。"""
    return _shrink_paths(str(path))


def _shrink_paths(text: str) -> str:
    """把文本里的家目录前缀缩成 ``~``（按路径边界匹配，不会误伤 ``/home/u2``）。"""
    home = os.path.expanduser("~")
    if not home or home == "/":
        return text
    return re.sub(re.escape(home) + r"(?=[/\\]|$)", "~", text)


#: 折行时可以在这些字符**之后**断开：路径分隔符、空白、以及中文里本就成词的标点。
#: 特意**不含**开括号 —— 在 ``（`` 后断会让括号独占行尾。
_BREAK_AFTER = frozenset("/ \\、，,；;·—")


def _wrap(text: str, width: int) -> list[str]:
    """按**显示宽度**折行。优先在空格/路径分隔符处断，实在没有断点才按列硬切。

    交给终端自动折行会破坏缩进对齐（续行从第 0 列开始），所以这里自己折。
    """
    if width <= 0 or _display_width(text) <= width:
        return [text]

    lines: list[str] = []
    current = ""
    for chunk in _chunks(text):
        if _display_width(current + chunk) <= width:
            current += chunk
            continue
        if current.strip():
            lines.append(current.rstrip())
        current = chunk.lstrip()
        # 单个片段就超宽（很长的路径名，中间没有断点）：按列硬切
        while _display_width(current) > width:
            head, current = _cut_at(current, width)
            lines.append(head)
    if current.strip():
        lines.append(current.rstrip())
    return [_avoid_orphan(line) for line in lines]


def _avoid_orphan(line: str) -> str:
    """不让行尾留下孤零零的开括号（``…project（`` 后面本该跟着内容）。"""
    stripped = line.rstrip()
    if stripped and stripped[-1] in "（(":
        return stripped[:-1].rstrip()
    return stripped


def _chunks(text: str) -> list[str]:
    """切成"片段 + 其后的断点"，断点保留在前一片段里（``/tmp/`` 不会被拆开）。"""
    chunks: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in _BREAK_AFTER:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _cut_at(text: str, width: int) -> tuple[str, str]:
    """在显示宽度 ``width`` 处切开，返回 ``(前半, 后半)``，不拆散 2 列宽字符。"""
    taken = 0
    for index, char in enumerate(text):
        size = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if taken + size > width:
            return text[:index], text[index:]
        taken += size
    return text, ""


def _conflict_note(conflicts: list[scan.Conflict], path: Path) -> str:
    """该位置对应的冲突说明（残留链接用：有效或断链本身还不足以说明问题）。"""
    for conflict in conflicts:
        if conflict.path == path:
            return f"  {conflict.message}"
    return ""


def _print_links(style: Style, snapshot: scan.Snapshot, skill: Skill) -> None:
    """逐条位置。缩进按**显示宽度**算 —— 中文/框线字形的列宽与 ``len`` 不等。"""
    statuses = snapshot.links_of(skill)
    if not statuses:
        print(style.dim("  没有可写的 agent 目录（检查配置与目录是否存在）"))
        return

    agent_width = max(len(st.agent_id) for st in statuses)
    # 状态列用固定宽度：全局取最宽的那个状态名，这样多个 skill 块之间缩进一致，
    # 不会因为某块恰好只有"已启用"而整体左移、看起来像没对齐。
    state_width = max(_display_width(label) for _, label in _STATE_LABELS.values())
    # 行首：`  ␣ 枝干 agent  状态  ` —— 枝干 4 列、"字形+空格" 2 列
    indent = 2 + 4 + 2 + agent_width + 2 + state_width + 2

    for index, status in enumerate(statuses):
        last = index == len(statuses) - 1
        branch = "└── " if last else "├── "
        glyph, label = _state_label(status.state)
        colour = _state_style(style, status.state)
        print(f"  {branch}{colour(glyph)} {status.agent_id.ljust(agent_width)}  "
              f"{colour(_pad(label, state_width))}  {_tilde(status.link_path)}")
        note = status.target_note(snapshot.store_dir)
        if note:
            # 续行的竖线要落在枝干正下方（第 2 列），才能看出它属于上一条
            stem = "  " + ("│" if not last else " ") + "  "
            pad = " " * (indent - len(stem))
            for line in _wrap(_shrink_paths(note), _width() - indent):
                print(style.dim(f"{stem}{pad}{line}"))


#: 字形 + 中文状态名。英文枚举（linked/occupied…）留给 ``--json``，界面上不用。
_STATE_LABELS: dict[LinkState, tuple[str, str]] = {
    LinkState.LINKED: ("✓", "已启用"),
    LinkState.ABSENT: ("·", "未启用"),
    LinkState.BROKEN: ("✗", "断链"),
    LinkState.ELSEWHERE: ("✗", "指向别处"),
    LinkState.OCCUPIED: ("✗", "被占位"),
}


def _state_label(state: LinkState) -> tuple[str, str]:
    return _STATE_LABELS[state]


def _state_style(style: Style, state: LinkState):
    if state is LinkState.LINKED:
        return style.green
    if state in (LinkState.OCCUPIED, LinkState.ELSEWHERE, LinkState.BROKEN):
        return style.red
    return style.dim


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补齐（中文占 2 列），否则中文状态名会让整列错位。"""
    return text + " " * max(0, width - _display_width(text))


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _clip(text: str, limit: int) -> str:
    """按**显示宽度**截断（中文占 2 列）。用 ``len`` 算会把中文当半个字宽，导致溢出。"""
    single = " ".join(text.split())
    if _display_width(single) <= limit:
        return single
    head, _ = _cut_at(single, max(1, limit - 1))
    return head.rstrip() + "…"


def _run_write(args, *, enable: bool) -> int:
    style = Style(sys.stdout)
    snapshot = scan.scan(config_mod.load())
    skills = _select(snapshot, args.query, args)
    if not skills:
        print("没有选中任何 skill（用 --all 表示全部）", file=sys.stderr)
        return EXIT_USAGE
    if not snapshot.store_dir.is_dir():
        print(f"仓库目录不存在：{_tilde(snapshot.store_dir)}", file=sys.stderr)
        return EXIT_PROBLEM

    targets = _resolve_targets(snapshot, args)
    if not targets:
        print("没有任何可写的 agent 目录（检查配置与目录是否存在）", file=sys.stderr)
        return EXIT_PROBLEM

    if args.dry_run:
        print(style.dim("[dry-run] 只报告将要发生的事，不做任何改动"))

    problems = 0
    for skill in skills:
        verb = "启用" if enable else "停用"
        print(style.bold(f"{verb} {skill.id}") + style.dim(f"  （{skill.dir_path}）"))
        if args.dry_run:
            for target in targets:
                print(f"  {style.dim('将改动'):　<6} {target.id:6} "
                      f"{target.dir / skill.link_name}")
            continue
        outcomes = (
            actions.enable(snapshot.skills, skill, targets, force=args.force)
            if enable
            else actions.disable(skill, targets)
        )
        problems += _print_outcomes(style, outcomes)
    return EXIT_PROBLEM if problems else EXIT_OK


def cmd_enable(args) -> int:
    return _run_write(args, enable=True)


def cmd_disable(args) -> int:
    return _run_write(args, enable=False)


def cmd_agents(args) -> int:
    style = Style(sys.stdout)
    cfg = config_mod.load()
    if args.json:
        _emit_json({
            "config": str(cfg.path) if cfg.path else None,
            "store": str(cfg.store_dir),
            "agents": [
                {"id": a.id, "name": a.name, "dir": str(a.dir), "shared": a.shared,
                 "enabled": a.enabled, "present": a.present}
                for a in cfg.agents
            ],
        })
        return EXIT_OK

    print(style.dim(f"配置文件：{cfg.path or '（无，使用内置默认）'}"))
    print(style.dim(f"仓库：    {_tilde(cfg.store_dir)}"))
    print()
    for agent in cfg.agents:
        state = style.green("目录存在") if agent.present else style.yellow("目录不存在")
        flags = []
        if agent.shared:
            flags.append("共享")
        if not agent.enabled:
            flags.append(style.yellow("已停用"))
        print(f"  {agent.id:8} {agent.name}  {style.dim(_tilde(agent.dir))}")
        print(f"           {state}" + (f"  ({'、'.join(flags)})" if flags else ""))
    print()
    print(style.dim("新增 agent：编辑配置文件里的 [[agents]]（skm config path 显示位置）"))
    return EXIT_OK


def cmd_config(args) -> int:
    style = Style(sys.stdout)
    cfg = config_mod.load()
    if args.action == "path":
        print(cfg.path or config_mod.config_path(cfg.base_dir))
        return EXIT_OK
    if args.action == "show":
        if cfg.path:
            print(cfg.path.read_text(encoding="utf-8"), end="")
        else:
            print(config_mod.render_default(cfg.base_dir), end="")
        return EXIT_OK
    # init
    if args.base_dir:
        base = Path(os.path.expanduser(args.base_dir))
    else:
        base = cfg.base_dir
    path = config_mod.config_path(base)
    if path.exists() and not args.force:
        print(f"配置文件已存在：{path}（要覆盖请加 --force）", file=sys.stderr)
        return EXIT_PROBLEM
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod.render_default(base), encoding="utf-8")
    print(style.green(f"已写入配置：{path}"))
    (base / "skills").mkdir(parents=True, exist_ok=True)
    print(style.dim(f"仓库目录：{base / 'skills'}"))
    return EXIT_OK


def _import_roots(cfg: config_mod.Config, args) -> list[importer.Root]:
    """决定扫哪些目录：给了路径就扫路径（可以有多个），否则扫全部 agent 目录。

    agent 目录即使 ``enabled = false`` 也扫 —— "不再向它铺链接"和"不管它的 skill"
    是两件事；用户的 skill 真身躺在里面，照样该被收进仓库。
    """
    if args.paths:
        roots = []
        for raw in args.paths:
            path = Path(os.path.expandvars(os.path.expanduser(raw)))
            if path.is_symlink():
                # 根目录本身是软链（例如你把 ~/.agents/skills 链到 dotfiles 里）：
                # 按真身扫，这样"root 是链接"不会把整次扫描挡掉。
                path = Path(os.path.realpath(path))
            roots.append(importer.Root(path, "指定路径", explicit=True))
        return roots
    return [
        importer.Root(
            Path(os.path.realpath(a.dir)) if a.dir.is_symlink() else a.dir, a.id, False
        )
        for a in cfg.agents
    ]


def cmd_import(args) -> int:
    """收编散落的 skill 真身：扫目录 → 搬进仓库 → 原位置补链接。

    默认**只出计划**，``--yes`` 才动手 —— 这一步会移动真身（别的命令只动链接），
    先让用户看清"哪个目录要搬到哪"是对数据的最低尊重。
    """
    style = Style(sys.stdout)
    cfg = config_mod.load()
    roots = _import_roots(cfg, args)
    if not roots:
        print("没有可扫描的目录：给出路径，或先让某个 agent 目录存在", file=sys.stderr)
        print("例：skm import ~/Downloads/skills", file=sys.stderr)
        return EXIT_USAGE

    plan = importer.plan(cfg, roots, category=args.category, copy=args.copy)
    for warning in plan.warnings:
        print(style.yellow(f"⚠ {_shrink_paths(warning)}"), file=sys.stderr)
    for path, reason in plan.ignored:
        print(style.dim(f"  忽略 {_tilde(path)}（{reason}）"), file=sys.stderr)

    records: list[importer.Record] = []
    execute = args.yes and not args.dry_run
    if execute:
        try:
            records = importer.execute(plan, copy=args.copy)
        except OSError as exc:
            print(f"无法创建仓库目录 {_tilde(cfg.store_dir)}：{exc.strerror or exc}",
                  file=sys.stderr)
            return EXIT_PROBLEM

    if args.json:
        _emit_json(_import_payload(plan, records))
        return EXIT_PROBLEM if _import_problems(plan, records) else EXIT_OK

    print(style.dim(f"仓库：{_tilde(cfg.store_dir)}"))
    print(style.dim("扫描：" + "、".join(
        _tilde(root.path) if root.explicit else f"{root.origin}（{_tilde(root.path)}）"
        for root in roots
    )))
    print()

    _print_import_plan(style, plan, executed=execute)
    if execute:
        _print_import_records(style, records)
    problems = _import_problems(plan, records)

    print()
    if execute:
        imported = [r for r in records
                    if r.action in (importer.Action.MOVED, importer.Action.COPIED)]
        print(f"共收进 {len(imported)} 个 skill；"
              f"仓库现有 {len(scan.scan(cfg).skills)} 个")
        if imported:
            print(style.dim("下一步：skm list 确认，skm enable <名字> 启用想用的"))
        return EXIT_PROBLEM if problems else EXIT_OK

    if plan.importing():
        print(style.dim("以上为计划，未改动任何文件。执行：skm import --yes"))
    elif problems:
        print(style.yellow("没有可执行的项（上面被拒绝的需先处理）"))
    else:
        print(style.dim("没有需要收编的 skill"))
    return EXIT_PROBLEM if problems else EXIT_OK


def _import_problems(plan: importer.Plan, records: list[importer.Record]) -> int:
    problems = plan.count(importer.Verdict.REFUSE)
    problems += sum(1 for r in records
                    if r.action in (importer.Action.REFUSED, importer.Action.FAILED,
                                    importer.Action.PARTIAL))
    return problems


def _print_import_plan(style: Style, plan: importer.Plan, *, executed: bool) -> None:
    for verdict, title in (
        (importer.Verdict.IMPORT, "已收进仓库" if executed else "将被收进仓库"),
        (importer.Verdict.REFUSE, "拒绝"),
        (importer.Verdict.SKIP, "跳过"),
    ):
        items = [i for i in plan.items if i.verdict is verdict]
        if not items:
            continue  # 空分组不占版面
        colour = style.red if verdict is importer.Verdict.REFUSE else style.bold
        print(colour(f"{title}（{len(items)}）"))
        width = min(max((len(i.rel) for i in items), default=10), 40)
        for item in items:
            print(f"  {item.rel.ljust(width)}  {style.dim('←')} {item.candidate.source}")
            if item.detail:
                print(style.dim(f"  {' ' * width}    {item.detail}"))
            for warning in item.warnings:
                print(style.yellow(f"  {' ' * width}    ⚠ {warning}"))
        print()


def _print_import_records(style: Style, records: list[importer.Record]) -> None:
    labels = {
        importer.Action.MOVED: style.green("已搬动"),
        importer.Action.COPIED: style.green("已复制"),
        importer.Action.PARTIAL: style.yellow("已入库，链接未完成"),
        importer.Action.REFUSED: style.red("拒绝"),
        importer.Action.FAILED: style.red("失败"),
    }
    for record in records:
        item = record.item
        print(f"  {labels[record.action]}　{item.rel}  {item.candidate.source}")
        if record.detail:
            print(style.dim(f"        {record.detail}"))


def _import_payload(plan: importer.Plan, records: list[importer.Record]) -> dict:
    # 按对象身份取结果：rel 在"同一次扫描出现同名"时并不唯一
    by_item = {id(r.item): r for r in records}
    return {
        "store": str(plan.config.store_dir),
        "items": [
            {
                "rel": item.rel,
                "source": str(item.candidate.source),
                "store_path": str(item.store_path),
                "origin": item.candidate.origin,
                "verdict": item.verdict.value,
                "detail": item.detail,
                "link_path": str(item.link_path) if item.link_path else None,
                "name": item.candidate.skill.name,
                "description": item.candidate.skill.description,
                "warnings": item.warnings,
                "result": by_item[id(item)].action.value if id(item) in by_item else None,
                "result_detail": by_item[id(item)].detail if id(item) in by_item else None,
            }
            for item in plan.items
        ],
        "ignored": [{"path": str(p), "reason": reason} for p, reason in plan.ignored],
        "warnings": plan.warnings,
    }


def cmd_issues(args) -> int:
    """报告残留链接与冲突；仅在 ``--fix`` 时清理能证明属于本工具的残留链接。"""
    style = Style(sys.stdout)
    snapshot = scan.scan(config_mod.load())
    if args.json:
        _emit_json({
            "orphans": snapshot_payload(snapshot)["orphans"],
            "conflicts": snapshot_payload(snapshot)["conflicts"],
            "warnings": snapshot.warnings,
        })
        return EXIT_OK

    for warning in snapshot.warnings:
        print(style.yellow(f"⚠ {warning}"), file=sys.stderr)

    if not snapshot.orphans and not snapshot.conflicts:
        print(style.green("没有发现残留链接或冲突"))
        return EXIT_OK

    for orphan in snapshot.orphans:
        state = "断链" if orphan.broken else "有效"
        print(f"{style.yellow('[残留链接]')} {orphan.agent_id}")
        print(f"    {_tilde(orphan.link_path)} → {_shrink_paths(orphan.target)}  [{state}]")
    for conflict in snapshot.conflicts:
        where = f"{conflict.agent_id}  " if conflict.agent_id else ""
        print(f"{style.red('[' + _conflict_label(conflict.kind) + ']')} {where}"
              f"{_tilde(conflict.path)}")
        print(style.dim(f"    {_shrink_paths(conflict.message)}"))

    if not args.fix:
        print()
        print(style.dim("残留链接可用 `skm issues --fix` 删除；"
                        "冲突不会被自动处理（可能是别人的数据）"))
        return EXIT_PROBLEM

    print()
    problems = 0
    for orphan in snapshot.orphans:
        # 只删指向仓库的链接（scan 已经确认过目标在仓库内），绝不递归、不碰真目录
        outcome = actions.remove_orphan(orphan.link_path)
        print(f"  {style.green('已删除') if outcome.changed else style.yellow(outcome.result.value)}"
              f"  {orphan.agent_id:8} {_tilde(orphan.link_path)}")
        if outcome.detail and not outcome.changed:
            print(style.dim(f"        {outcome.detail}"))
        if outcome.result in (actions.Result.REFUSED, actions.Result.FAILED):
            problems += 1
    if snapshot.conflicts:
        print(style.yellow("冲突未处理 —— 需要你判断它们是不是别人的数据"))
        # 冲突也算未解决：脚本据此判断"这台机器是否干净"
        problems += len(snapshot.conflicts)
    return EXIT_PROBLEM if problems else EXIT_OK


#: 冲突类别 -> 中文名。``kind`` 是给 ``--json`` 用的稳定标识，界面上不用露英文。
_CONFLICT_LABELS = {
    "occupied": "被占位",
    "elsewhere": "指向别处",
    "broken": "断链",
    "link_name_collision": "目录名撞车",
    "display_name_collision": "name 撞车",
    "dir_mismatch": "目录名不一致",
    "name_collision": "重名",
}


def _conflict_label(kind: str) -> str:
    return _CONFLICT_LABELS.get(kind, kind)


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------


_OVERVIEW = """\
管理本地 skill 仓库，控制每个 skill 对各个 AI agent 是启用还是停用。

「启用」= 在 agent 的 skills 目录里建一条指向仓库真身的符号链接。
「停用」= 删掉那条链接。仓库里的真身（含分类目录）始终保持不变。

  仓库（真身）                 agent 目录（链接）
  ~/.skill-manager/skills/     ~/.agents/skills/
    pdf/            ──链接──►     pdf -> …/skills/pdf
    qt-skills/                   qt-qml -> …/skills/qt-skills/qt-qml
      qt-qml/

链接在不在就是唯一真相：skm 不保存任何"启用状态"，因此不会出现
"配置说有、实际却没有"的情况；别的工具改了目录，下次查询就如实反映。

目录结构（分类 = 仓库里的一层目录名，深度固定为两层）：

  <仓库>/<skill>/SKILL.md            → 未分类
  <仓库>/<分类>/<skill>/SKILL.md      → 归入该分类

仓库还是空的？两个办法把 skill 放进去：

  手动：把目录拷进 <仓库>/<名字>/（须含 SKILL.md）
  扫描：skm import   —— 扫已有 skill 收进仓库，原位置自动补链接
"""

_EXAMPLES = """\
常用流程：

  # 1. 先看看仓库里有什么、现在都什么状态
  skm list

  # 2. 仓库空着？扫一遍已有 skill 收进来（先看计划，再加 --yes）
  skm import
  skm import --yes

  # 3. 启用单个 / 一整类
  skm enable pdf
  skm enable --category qt-skills

  # 4. 确认没问题再动手（-n 只显示不改）
  skm disable 'qt-*' -n

  # 5. 停用
  skm disable pdf

查询示例：

  skm list                            树形列出全部，带启用状态
  skm list --enabled                  只看已启用的
  skm list --disabled 'qt-*'          只看「qt- 开头」里未启用的
  skm list --category security-skills 只看某个分类
  skm status pdf qt-qml               批量查看详细状态
  skm status --json | jq .skills      机器可读输出

收编示例：

  skm import                          扫 agent 目录，只出计划
  skm import ~/Downloads/skills       扫指定目录（可给多个）
  skm import ~/s --category qt --yes  收进指定分类并执行
  skm import --copy --yes             复制而非移动（草稿目录用）

启用 / 停用示例：

  skm enable pdf docx                 一次启用多个
  skm enable --all                    启用仓库内全部
  skm enable --all --disabled         只把「还没启用的」启用（等价于全部启用）
  skm enable --category qt-skills     按分类批量启用
  skm disable --all                   全部停用（真身不动）
  skm disable pdf --agent codex       只对某个 agent 停用
  skm enable pdf -n                   预演，不实际改

agent 与配置示例：

  skm agents                          看当前会往哪些目录铺链接
  skm config path                     配置文件在哪
  skm config init                     生成默认配置（首次部署时执行一次）

排查示例：

  skm issues                          残留链接、冲突（不会被自动覆盖）
  skm issues --fix                    只清理能证明属于本工具的残留链接

退出码：0 成功 · 1 有操作被拒绝或失败 · 2 用法错误或查询无结果
"""


def _localise_help(text: str) -> str:
    """把 argparse 自带的英文骨架翻成中文。

    argparse 没有提供本地化这几个固定词条的公开接口（``usage:``、
    ``positional arguments:``、``options:``、``-h`` 的说明由 gettext 在没有语言包时
    输出英文），而这几行恰好是用户最先看到的内容。这里只替换这些固定字串 ——
    我们自己写的正文里不会出现它们，所以替换是安全的。
    """
    text = text.replace("usage: ", "用法：", 1)
    text = text.replace("positional arguments:", "命令：")
    text = text.replace("options:", "选项：")
    text = text.replace("optional arguments:", "选项：")
    text = text.replace("show this help message and exit", "显示本帮助并退出")
    return text


_ERROR_PREFIXES = (
    ("unrecognized arguments: ", "无法识别的参数："),
    ("invalid choice: ", "无效的取值："),
    ("expected one argument", "该选项需要一个值"),
    ("the following arguments are required: ", "缺少必需参数："),
)


def _localise_error(message: str) -> str:
    """把 argparse 生成的英文错误翻成中文；未覆盖的原文照旧输出，便于对照排错。"""
    for prefix, translated in _ERROR_PREFIXES:
        if message.startswith(prefix):
            return translated + message[len(prefix):]
    return message


class Parser(argparse.ArgumentParser):
    """带中文骨架的 ArgumentParser（子命令也继承同样行为）。"""

    def format_help(self) -> str:
        return _localise_help(super().format_help())

    def format_usage(self) -> str:
        # parser.error() 走的是 format_usage，不翻这里的话报错时会露出英文用法行
        return _localise_help(super().format_usage())

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        super().error(_localise_error(message))


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="skm",
        description=_OVERVIEW,
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"skm {__version__}",
        help="显示版本号并退出",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<命令>",
                                parser_class=Parser)

    common_filters = argparse.ArgumentParser(add_help=False)
    common_filters.add_argument("--category", "-c", metavar="分类",
                                help="只看某个分类（未分类用 ''）")
    common_filters.add_argument("--enabled", action="store_true", help="只看已启用")
    common_filters.add_argument("--disabled", action="store_true", help="只看未启用")
    common_filters.add_argument("--json", action="store_true", help="输出 JSON")

    listing = sub.add_parser(
        "list", parents=[common_filters], aliases=["ls"],
        help="树形列出仓库里的全部 skill 及启用状态",
        description="树形列出仓库里的全部 skill 及启用状态。可给标识做筛选（支持 glob）。",
        epilog="例：skm list\n"
               "    skm list --enabled\n"
               "    skm list --disabled 'qt-*'\n"
               "    skm list --category security-skills --json\n"
               "    skm list --category security-skills --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    listing.add_argument("query", nargs="*", metavar="标识",
                         help="只显示匹配的 skill（目录名 / 分类/目录名 / frontmatter name，支持 glob）")
    listing.set_defaults(func=cmd_list)

    status = sub.add_parser(
        "status", parents=[common_filters],
        help="查看指定 skill 的详细启用状态（可批量，缺省为全部）",
        description="查看 skill 的详细状态：每个 agent 目录里那块位置到底是什么"
                    "（本 skill 的链接 / 空位 / 断链 / 指向别处 / 被真目录占住）。",
        epilog="例：skm status pdf\n"
               "    skm status pdf qt-qml docx\n"
               "    skm status --category qt-skills\n"

               "    skm status pdf --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status.add_argument("query", nargs="*", metavar="标识",
                        help="skill 标识，可给多个；缺省 = 全部")
    status.set_defaults(func=cmd_status)

    write_flags = argparse.ArgumentParser(add_help=False)
    write_flags.add_argument("query", nargs="*", metavar="标识",
                             help="skill 标识，可给多个；用 --all 表示全部")
    write_flags.add_argument("--all", "-a", action="store_true", help="对仓库内全部 skill")
    write_flags.add_argument("--agent", action="append", metavar="ID",
                             help="只操作某个 agent（可重复；缺省 = 全部可写目标）")
    write_flags.add_argument("--force", action="store_true",
                             help="允许替换已存在的断链（仍不会覆盖真目录/别人的链接）")
    write_flags.add_argument("--dry-run", "-n", action="store_true", help="只显示不改动")
    write_flags.add_argument("--category", "-c", metavar="分类", help="按分类批量操作")
    write_flags.add_argument("--enabled", action="store_true", help="只处理当前已启用的")
    write_flags.add_argument("--disabled", action="store_true",
                             help="只处理当前未启用的（与 --all 合用即「全部启用」）")

    enable = sub.add_parser(
        "enable", aliases=["on"], parents=[write_flags],
        help="启用：在 agent 目录里建指向仓库真身的链接",
        description="启用 skill：在 agent 的 skills 目录里建立指向仓库真身的符号链接。"
                    "已是启用状态则跳过（幂等）。",
        epilog="例：skm enable pdf\n"
               "    skm enable pdf docx qt-qml\n"
               "    skm enable --category qt-skills\n"

               "    skm enable 'dbus-*'        （glob）\n"
               "    skm enable --all\n"
               "    skm enable --all --disabled  只启用还没启用的\n"
               "    skm enable pdf --agent codex\n"
               "    skm enable pdf -n          预演，不实际改\n"
               "\n"
               "安全性：位置被真目录/真文件占住、或链接指向别的 skill 时一律拒绝，\n"
               "绝不覆盖；断链需显式加 --force 才替换。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    enable.set_defaults(func=cmd_enable, command="enable", subparser=enable)

    disable = sub.add_parser(
        "disable", aliases=["off"], parents=[write_flags],
        help="停用：删除那些链接（仓库真身保留）",
        description="停用 skill：删除 agent 目录里指向本 skill 的链接。"
                    "仓库里的真身与分类目录不受任何影响。",
        epilog="例：skm disable pdf\n"
               "    skm disable pdf qt-qml\n"
               "    skm disable --category security-skills\n"

               "    skm disable --all\n"
               "    skm disable pdf --agent codex   只停用某个 agent 的链接\n"
               "    skm disable qt-qml -n           预演，不实际改\n"
               "\n"
               "安全性：只删除「解析后正是本 skill 真身」的链接；真目录和指向别处的\n"
               "链接一律不动，即使指定了 --force。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    disable.set_defaults(func=cmd_disable, command="disable", subparser=disable)

    agents = sub.add_parser(
        "agents", help="列出配置里的 agent 及其加载目录",
        description="列出配置里的各个 agent、它们的 skills 加载目录是否存在，"
                    "以及是否与其他 agent 共用同一目录。",
        epilog="例：skm agents\n    skm agents --json\n"
               "\n"
               "新增 agent：编辑配置文件（skm config path）里的 [[agents]]，"
               "改 id / name / dir 即可，无需改代码。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agents.add_argument("--json", action="store_true")
    agents.set_defaults(func=cmd_agents)

    cfg = sub.add_parser(
        "config", help="配置文件的查看与初始化",
        description="查看配置文件位置 / 内容，或生成默认配置。",
        epilog="例：skm config path       配置文件在哪\n"
               "    skm config show       打印当前生效的配置\n"
               "    skm config init       生成默认配置（已存在则不动）\n"
               "    skm config init --base-dir ~/my-skills   指定仓库根目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cfg.add_argument("action", choices=["init", "show", "path"], nargs="?",
                     default="show", metavar="{init,show,path}")
    cfg.add_argument("--base-dir", metavar="目录", help="init 时使用的仓库根目录")
    cfg.add_argument("--force", action="store_true", help="init 时覆盖已有文件")
    cfg.set_defaults(func=cmd_config)

    imp = sub.add_parser(
        "import", aliases=["scan"], help="扫描已有 skill 并收进仓库",
        description="扫描目录，把找到的 skill 真身收进仓库 —— 给「新装好、仓库还空着」"
                    "和「skill 散落在各处」两种情况用。\n"
                    "不给路径时扫配置里的全部 agent 目录（含 enabled = false 的）。\n"
                    "\n"
                    "真身原本就在 agent 目录里时，搬走后会在原位置补一条链接：\n"
                    "agent 读到的内容分毫不变，仓库从此才是真身唯一所在。",
        epilog="例：skm import                     扫 agent 目录，先出计划（不改动）\n"
               "    skm import --yes               确认后收编\n"
               "    skm import ~/Downloads/skills  扫指定目录（可给多个）\n"
               "    skm import ~/s --category qt   收进指定分类\n"
               "    skm import -n                  只出计划（同缺省）\n"
               "    skm import --copy --yes        复制而不是移动（草稿目录用）\n"
               "    skm import --json | jq .items  机器可读\n"
               "\n"
               "安全性：仓库里已有同名条目、同一次扫描出现两处同名真身 —— 一律拒绝，\n"
               "不覆盖；符号链接不是真身，跳过不搬。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    imp.add_argument("paths", nargs="*", metavar="路径",
                     help="要扫描的目录（可给多个）；缺省 = 配置里的全部 agent 目录")
    imp.add_argument("--yes", "-y", action="store_true",
                     help="真的执行（缺省只出计划，因为这一步会移动真身）")
    imp.add_argument("--copy", action="store_true",
                     help="复制而不是移动（源目录保留，不回填链接）")
    imp.add_argument("--category", "-c", metavar="分类",
                     help="收进仓库的哪个分类（缺省沿用源目录的分类结构）")
    imp.add_argument("--dry-run", "-n", action="store_true", help="只出计划，不改动")
    imp.add_argument("--json", action="store_true", help="输出 JSON")
    imp.set_defaults(func=cmd_import, command="import")

    issues = sub.add_parser(
        "issues", aliases=["doctor"], help="报告残留链接与冲突",
        description="报告两类需要人看一眼的情况：\n"
                    "  残留链接 —— 指向仓库、但仓库里已无对应 skill 的链接\n"
                    "  冲突     —— 位置被真目录占住、链接指向别处、断链、重名",
        epilog="例：skm issues\n    skm issues --fix\n"
               "\n"
               "说明：--fix 只删除「能证明属于本工具的残留链接」；真目录与指向别处的\n"
               "链接不会被自动处理 —— 它们可能是别的工具的数据。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    issues.add_argument("--fix", action="store_true",
                        help="删除可证明属于本工具的残留链接")
    issues.add_argument("--json", action="store_true")
    issues.set_defaults(func=cmd_issues)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) in ("enable", "disable"):
        if args.all:
            args.query = []
        # 用子命令自己的 parser 报错：这样用法行是 `skm enable ...` 而不是顶层用法
        if not (args.query or args.all or args.category):
            args.subparser.error("请给出 skill 标识，或用 --all / --category 表示批量")
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # 输出被 head/less 截断是正常用法，不是错误：安静退出。
        # devnull 这一步是为了让解释器退出时的 flush 不再抛第二次。
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
