"""数据模型：skill、链接状态、frontmatter 解析。

这里不碰文件系统写操作，也不做扫描编排 —— 只描述"看到的事实"。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: 一个 skill 由它所在目录里的这个文件定义（OMP 的约定）。
SKILL_FILE = "SKILL.md"


class LinkState(str, Enum):
    """某个 agent 目录里，指向本 skill 的那个位置现在是什么。

    刻意区分"没有"、"是我们"、"坏了"、"指向别处"、"被真目录占住"：
    后面三种都不允许被自动覆盖，否则会破坏别人的数据（模块 ``actions`` 据此拒绝）。
    """

    ABSENT = "absent"        #: 位置不存在（正常）
    LINKED = "linked"        #: 符号链接，解析后就是本 skill
    BROKEN = "broken"        #: 符号链接，但目标已不存在
    ELSEWHERE = "elsewhere"  #: 符号链接，指向别的东西
    OCCUPIED = "occupied"    #: 真目录/真文件 —— 别的工具装的，只报告不动它


@dataclass
class LinkStatus:
    """某个目标目录里，本 skill 的链接位置的实际状态。"""

    agent_id: str
    link_path: Path
    state: LinkState
    target: str | None = None    #: readlink 的原文（未解析）
    resolved: str | None = None  #: 解析后的绝对路径；断链时是"本该在"的位置
    detail: str = ""

    @property
    def enabled(self) -> bool:
        return self.state is LinkState.LINKED

    def target_note(self, store_dir: Path) -> str:
        """补充"它到底指向哪、还归不归本工具管" —— 只有出问题时才有必要说。

        ``linked`` 按定义就指向本 skill 真身，再说一遍是噪音；``absent`` 空位无话可说。
        断链的 ``resolved`` 是"本该在"的位置，所以同一句话对断链也成立。
        """
        if self.state is LinkState.OCCUPIED:
            return "真目录/真文件占位，不是 skm 建的链接"
        if self.state not in (LinkState.BROKEN, LinkState.ELSEWHERE) or not self.resolved:
            return ""

        inside = self.resolved == str(store_dir) or self.resolved.startswith(
            str(store_dir) + os.sep
        )
        if self.state is LinkState.BROKEN:
            where = ("在仓库内，skm enable --force 可重指" if inside
                     else "在仓库外，不是本工具的数据")
            return f"本该指向 {self.resolved}（{where}）"
        # ELSEWHERE：目标在仓库内时通常是**另一个 skill 的真身**，那绝不能覆盖
        where = ("在仓库内，是别的 skill 的真身" if inside
                 else "在仓库外，不是本工具的数据")
        return f"现在指向 {self.resolved}（{where}）"


@dataclass(eq=False)
class Skill:
    """仓库里的一个 skill 真身。

    ``eq=False``：两个字段完全相同的 skill 仍是**两处不同的真身**，
    按值比较会让"这个 skill 在不在筛选结果里"给出错误答案，也白费一次全字段比对。
    """

    dirname: str
    dir_path: Path
    real_path: str          #: realpath 归一 —— 跨软链去重的身份
    name: str               #: frontmatter 的 name，缺失时回退目录名
    description: str
    frontmatter: dict[str, str]
    category: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """稳定标识：仓库内相对路径（分类/目录名，未分类则只有目录名）。"""
        return f"{self.category}/{self.dirname}" if self.category else self.dirname

    @property
    def skill_file(self) -> Path:
        return self.dir_path / SKILL_FILE

    @property
    def link_name(self) -> str:
        """在 agent 目录里使用的链接名。

        用**目录名**而不是 frontmatter name：可预测，且与仓库结构一一对应。
        两者不一致时由 ``warnings`` 报告（OMP 的身份是 frontmatter name）。
        """
        return self.dirname


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------

_FM_KEY = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")
_BLOCK = {">", ">-", ">+", "|", "|-", "|+"}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 顶部的 YAML frontmatter。

    只做必要的子集：一层 ``key: 值``、引号、以及 ``>/|`` 折叠块。
    不引入 PyYAML：解析失败也只影响描述显示，不该让整个工具罢工。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}
    end = re.search(r"^---\s*$", text[3:], re.M)
    if end is None:
        return {}

    result: dict[str, str] = {}
    lines = text[3 : 3 + end.start()].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        # 顶层键之外的缩进行属于上一个键，已在下面消费掉
        if not line.strip() or line[:1] in (" ", "\t") or line.lstrip().startswith("#"):
            index += 1
            continue
        match = _FM_KEY.match(line)
        if match is None:
            index += 1
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if value in _BLOCK:
            index += 1
            chunk: list[str] = []
            while index < len(lines) and (
                not lines[index].strip() or lines[index][:1] in (" ", "\t")
            ):
                if lines[index].strip():
                    chunk.append(lines[index].strip())
                index += 1
            result[key] = " ".join(chunk)
            continue
        result[key] = _unquote(value)
        index += 1
    return result


def read_skill_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 SKILL.md 的 frontmatter，返回 ``(字段, 告警)``。读不动不抛异常。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, [f"读不到 SKILL.md：{exc.strerror or exc}"]
    return parse_frontmatter(text), []


# --------------------------------------------------------------------------
# 链接状态的读取（只读，不做任何修复）
# --------------------------------------------------------------------------


def inspect_link(link_path: Path, skill: Skill) -> LinkStatus:
    """查看 ``link_path`` 相对 ``skill`` 的状态。不跟随、不修改。"""
    agent_id = ""
    if not os.path.lexists(link_path):
        return LinkStatus(agent_id, link_path, LinkState.ABSENT)

    if not link_path.is_symlink():
        return LinkStatus(
            agent_id,
            link_path,
            LinkState.OCCUPIED,
            detail="真目录/真文件占位（不是 skm 建的链接）",
        )

    target = os.readlink(link_path)
    resolved = os.path.realpath(link_path)
    if not os.path.exists(link_path):
        return LinkStatus(
            agent_id, link_path, LinkState.BROKEN, target=target, resolved=resolved,
            detail="链接目标不存在",
        )
    if resolved == skill.real_path:
        return LinkStatus(
            agent_id, link_path, LinkState.LINKED, target=target, resolved=resolved
        )
    return LinkStatus(
        agent_id,
        link_path,
        LinkState.ELSEWHERE,
        target=target,
        resolved=resolved,
        detail="链接指向别的地方",
    )
