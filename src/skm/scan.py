"""扫描：把仓库与各 agent 目录的**当前事实**读成一份快照。

只读。任何写操作都在 ``actions``。扫描不做"修复"，也不跟随链接递归。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import AgentTarget, Config
from .model import (
    SKILL_FILE,
    LinkState,
    LinkStatus,
    Skill,
    inspect_link,
    read_skill_file,
)


@dataclass
class OrphanLink:
    """目标目录里指向本仓库、但仓库里已无对应 skill 的链接（陈旧残留）。"""

    agent_id: str
    link_path: Path
    target: str
    broken: bool


@dataclass
class Conflict:
    """某个位置现在不是我们的链接，且**不允许**被覆盖 —— 只报告。"""

    kind: str            #: occupied | elsewhere | broken | dir_mismatch | name_collision
    agent_id: str
    path: Path
    skill: str
    message: str


@dataclass
class Snapshot:
    config: Config
    store_dir: Path
    skills: list[Skill] = field(default_factory=list)
    links: dict[str, list[LinkStatus]] = field(default_factory=dict)  #: skill.id -> 状态
    orphans: list[OrphanLink] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def links_of(self, skill: Skill) -> list[LinkStatus]:
        return self.links.get(skill.id, [])

    def enabled(self, skill: Skill) -> bool:
        return any(st.state is LinkState.LINKED for st in self.links_of(skill))

    def categories(self) -> dict[str, list[Skill]]:
        """分类名 -> skill（未分类归到空字符串键，由调用方决定怎么显示）。"""
        grouped: dict[str, list[Skill]] = {}
        for skill in self.skills:
            grouped.setdefault(skill.category or "", []).append(skill)
        for items in grouped.values():
            items.sort(key=lambda s: s.dirname)
        return grouped


def _scan_store(store_dir: Path, warnings: list[str]) -> list[Skill]:
    """扫仓库：一层是 skill，两层是「分类 / skill」，就此为止（深度固定）。"""
    if not store_dir.is_dir():
        warnings.append(f"仓库目录不存在：{store_dir}")
        return []

    skills: list[Skill] = []
    seen_real: dict[str, Skill] = {}
    for entry in sorted(store_dir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and (entry / SKILL_FILE).is_file():
            skills.append(_make_skill(entry, None, seen_real, warnings))
        elif entry.is_dir():
            # 二层：分类目录。is_dir() 会跟随软链，所以"把分类指向别处"也能扫到。
            for sub in sorted(entry.iterdir(), key=lambda p: p.name):
                if sub.name.startswith("."):
                    continue
                if sub.is_dir() and (sub / SKILL_FILE).is_file():
                    skills.append(_make_skill(sub, entry.name, seen_real, warnings))
                elif sub.is_dir():
                    warnings.append(
                        f"{entry.name}/{sub.name} 里没有 {SKILL_FILE}，已跳过"
                    )
        else:
            warnings.append(f"{entry.name} 既不是目录也没有 {SKILL_FILE}，已跳过")
    return skills


def _make_skill(
    dir_path: Path,
    category: str | None,
    seen_real: dict[str, Skill],
    warnings: list[str],
) -> Skill:
    real_path = os.path.realpath(dir_path)
    frontmatter, read_warnings = read_skill_file(dir_path / SKILL_FILE)
    name = frontmatter.get("name", "").strip() or dir_path.name
    skill = Skill(
        dirname=dir_path.name,
        dir_path=dir_path,
        real_path=real_path,
        name=name,
        description=frontmatter.get("description", "").strip(),
        frontmatter=frontmatter,
        category=category,
        warnings=list(read_warnings),
    )
    if name != dir_path.name:
        skill.warnings.append(f"frontmatter name（{name}）与目录名不一致，链接用目录名")
    if not skill.description:
        skill.warnings.append("缺 description —— OMP 的 native 源要求它有值")

    previous = seen_real.get(real_path)
    if previous is not None:
        # 同一真身出现在仓库里两次：只保留一条记录，但要说清另一处在哪。
        previous.warnings.append(f"与 {skill.id} 是同一真身（{real_path}），已合并")
        warnings.append(f"{skill.id} 与 {previous.id} 指向同一目录，按一个 skill 处理")
    else:
        seen_real[real_path] = skill
    return skill


def _scan_target(agent: AgentTarget, skills: list[Skill], snapshot: Snapshot) -> None:
    """把一个目标目录里与仓库相关的条目读成事实。"""
    by_name = {skill.link_name: skill for skill in skills}
    matched: set[str] = set()
    try:
        entries = sorted(agent.dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        snapshot.warnings.append(
            f"{agent.id} 目录读不动（{exc.strerror or exc}）：{agent.dir}"
        )
        return

    for entry in entries:
        skill = by_name.get(entry.name)
        if skill is None:
            # 与仓库无关的条目：只有在它是指向仓库的残留链接时才报告。
            if entry.is_symlink():
                resolved = os.path.realpath(entry)
                if resolved == snapshot.store_dir or resolved.startswith(
                    str(snapshot.store_dir) + os.sep
                ):
                    snapshot.orphans.append(
                        OrphanLink(
                            agent_id=agent.id,
                            link_path=entry,
                            target=os.readlink(entry),
                            broken=not os.path.exists(entry),
                        )
                    )
            continue

        matched.add(skill.id)
        status = inspect_link(entry, skill)
        status.agent_id = agent.id
        snapshot.links.setdefault(skill.id, []).append(status)
        _record_conflict(snapshot, agent, skill, status)

    # 该目录里没有条目的 skill：显式记一条 ABSENT，界面就不用去推断"没记录=没有"
    for skill in skills:
        if skill.id in matched:
            continue
        snapshot.links.setdefault(skill.id, []).append(
            LinkStatus(agent.id, agent.dir / skill.link_name, LinkState.ABSENT)
        )


def _record_conflict(
    snapshot: Snapshot, agent: AgentTarget, skill: Skill, status: LinkStatus
) -> None:
    if status.state is LinkState.OCCUPIED:
        snapshot.conflicts.append(
            Conflict(
                kind="occupied",
                agent_id=agent.id,
                path=status.link_path,
                skill=skill.name,
                message="位置被真目录/真文件占住，不是 skm 建的链接 —— 不会覆盖",
            )
        )
    elif status.state is LinkState.ELSEWHERE:
        snapshot.conflicts.append(
            Conflict(
                kind="elsewhere",
                agent_id=agent.id,
                path=status.link_path,
                skill=skill.name,
                message=f"链接指向别处（现在 → {status.resolved}），不会覆盖",
            )
        )
    elif status.state is LinkState.BROKEN:
        inside = bool(status.resolved) and (
            status.resolved == str(snapshot.store_dir)
            or status.resolved.startswith(str(snapshot.store_dir) + os.sep)
        )
        snapshot.conflicts.append(
            Conflict(
                kind="broken",
                agent_id=agent.id,
                path=status.link_path,
                skill=skill.name,
                message=(
                    "断链，目标在仓库内，可用 skm enable 修复"
                    if inside
                    else f"断链，且目标不在仓库内（{status.resolved}）—— 不会删除"
                ),
            )
        )


def scan(config: Config) -> Snapshot:
    """读一份完整快照。不写任何东西。"""
    snapshot = Snapshot(config=config, store_dir=config.store_dir)
    snapshot.warnings.extend(config.warnings)
    snapshot.skills = _scan_store(config.store_dir, snapshot.warnings)
    _record_duplicates(snapshot)

    seen_dirs: set[str] = set()
    for agent in config.link_targets():
        if str(agent.dir) in seen_dirs:
            continue
        seen_dirs.add(str(agent.dir))
        _scan_target(agent, snapshot.skills, snapshot)

    snapshot.orphans.sort(key=lambda o: (o.agent_id, o.link_path.name))
    snapshot.conflicts.sort(key=lambda c: (c.kind, c.skill))
    return snapshot


def _record_duplicates(snapshot: Snapshot) -> None:
    """把仓库内部的两种重名说清楚 —— 它们都会让"启用"这件事无法两全。

    * **目录名相同**（跨分类）：链接名就是目录名，所以永远只有一个能启用。
    * **frontmatter name 相同**（同分类、不同目录）：OMP 按 name 判身份，
      两个不同真身用同一个名字，``skill://<name>`` 只能解析到一个。
    """
    by_dirname: dict[str, list[Skill]] = {}
    by_name: dict[str, list[Skill]] = {}
    for skill in snapshot.skills:
        by_dirname.setdefault(skill.dirname, []).append(skill)
        by_name.setdefault(skill.name, []).append(skill)

    for dirname, group in sorted(by_dirname.items()):
        if len(group) < 2:
            continue
        snapshot.conflicts.append(
            Conflict(
                kind="link_name_collision",
                agent_id="",
                path=group[0].dir_path,
                skill=dirname,
                message=(
                    f"{len(group)} 个 skill 的目录名都叫「{dirname}」"
                    f"（{'、'.join(s.id for s in group)}）："
                    "同一 agent 目录里只能存在一个同名条目，它们无法同时启用"
                ),
            )
        )

    for name, group in sorted(by_name.items()):
        dirnames = {s.dirname for s in group}
        if len(group) < 2 or len(dirnames) < 2:
            continue  # 目录名相同的情况上一条已经报过
        snapshot.conflicts.append(
            Conflict(
                kind="display_name_collision",
                agent_id="",
                path=group[0].dir_path,
                skill=name,
                message=(
                    f"{len(group)} 个 skill 的 name 都是「{name}」"
                    f"（{'、'.join(s.id for s in group)}）："
                    "OMP 按 name 判身份，skill:// 只能解析到其中一个"
                ),
            )
        )


def resolve(snapshot: Snapshot, query: str) -> list[Skill]:
    """把用户输入的标识解析成 skill：目录名 / 分类/目录名 / frontmatter name。

    支持 glob（``qt-*``、``security-skills/*``）。无匹配返回空；
    有歧义时由调用方提示候选（不猜）。
    """
    query = query.strip().strip("/")
    if not query:
        return []
    exact = [s for s in snapshot.skills if query in (s.dirname, s.id, s.name)]
    if exact:
        return _dedupe(exact)
    globbed = [
        s
        for s in snapshot.skills
        if fnmatch.fnmatchcase(s.dirname, query)
        or fnmatch.fnmatchcase(s.id, query)
        or fnmatch.fnmatchcase(s.name, query)
        or fnmatch.fnmatchcase(s.category or "", query)
    ]
    return _dedupe(globbed)


def _dedupe(skills: list[Skill]) -> list[Skill]:
    # Skill 按身份比较（``eq=False``），因此 set 去重就是"同一处真身只留一条"。
    return list(dict.fromkeys(skills))
