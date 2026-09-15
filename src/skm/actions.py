"""写操作：启用 = 建链，停用 = 删链。

安全底线（也是本工具最容易被写错的地方）：

* **绝不覆盖**：位置被真目录/真文件占住、或链接指向别的 skill 时，拒绝并报告。
  唯一的例外是 ``--force`` 显式替换**断链**。
* **只删自己的**：停用仅当链接解析后正是本 skill 的真身时才 unlink；
  别人的链接不是我们的数据，一律不动。
* **不替 agent 造目录**：目标目录不存在就跳过，不 mkdir。
* **不需要复制内容**：链接与设备无关，因此不存在跨设备问题 —— 这也是选软链而非拷贝的原因。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import AgentTarget
from .model import LinkState, Skill


class Result(str, Enum):
    CREATE = "create"      #: 刚建的链接
    REMOVE = "remove"      #: 刚删的链接
    ALREADY = "already"    #: 已经是目标状态，未改动
    SKIPPED = "skipped"    #: 目录不存在等，未改动
    REFUSED = "refused"    #: 拒绝改动（会破坏别人的数据）
    FAILED = "failed"      #: 系统调用失败


@dataclass
class Outcome:
    agent_id: str
    path: Path
    result: Result
    detail: str = ""

    @property
    def changed(self) -> bool:
        return self.result in (Result.CREATE, Result.REMOVE)


_STATE_LABEL = {
    LinkState.ABSENT: "空位",
    LinkState.LINKED: "本 skill 的链接",
    LinkState.BROKEN: "断链",
    LinkState.ELSEWHERE: "指向别处的链接",
    LinkState.OCCUPIED: "真目录/真文件",
}


def state_of(link_path: Path, skill: Skill) -> LinkState:
    """单点判断该位置相对 ``skill`` 是什么。不跟随深层、不做修改。"""
    if not os.path.lexists(link_path):
        return LinkState.ABSENT
    if not link_path.is_symlink():
        return LinkState.OCCUPIED
    if not os.path.exists(link_path):
        return LinkState.BROKEN
    return LinkState.LINKED if os.path.realpath(link_path) == skill.real_path \
        else LinkState.ELSEWHERE


def _other_claimants(skills: list[Skill], skill: Skill) -> list[Skill]:
    """仓库里与本 skill 争用同一链接名的其他 skill（跨分类同名目录）。"""
    return [s for s in skills if s is not skill and s.dirname == skill.dirname]


def enable(
    skills: list[Skill],
    skill: Skill,
    targets: list[AgentTarget],
    *,
    force: bool = False,
) -> list[Outcome]:
    """在给定目标目录里为 ``skill`` 建链。已是目标状态则跳过（幂等）。"""
    outcomes: list[Outcome] = []
    done_dirs: set[str] = set()
    claimants = _other_claimants(skills, skill)

    for target in targets:
        link_path = target.dir / skill.link_name
        key = str(target.dir)
        if key in done_dirs:
            # 多个 agent 共用同一目录：只落一次盘，但每个 agent 都要看到结果
            outcomes.append(Outcome(target.id, link_path, Result.ALREADY,
                                    detail="与前面的 agent 共用同一目录，已一次处理"))
            continue
        done_dirs.add(key)

        if not target.present:
            outcomes.append(Outcome(target.id, link_path, Result.SKIPPED,
                                    detail=f"目录不存在，未创建：{target.dir}"))
            continue

        if claimants:
            outcomes.append(Outcome(
                target.id, link_path, Result.REFUSED,
                detail="仓库里另有同名目录，链接名会撞车："
                       + "、".join(s.id for s in claimants)))
            continue

        state = state_of(link_path, skill)
        if state is LinkState.LINKED:
            outcomes.append(Outcome(target.id, link_path, Result.ALREADY, "已经启用"))
            continue
        if state is LinkState.OCCUPIED:
            outcomes.append(Outcome(target.id, link_path, Result.REFUSED,
                                    "位置被真目录/真文件占住，不覆盖"))
            continue
        if state is LinkState.ELSEWHERE:
            outcomes.append(Outcome(target.id, link_path, Result.REFUSED,
                                    "链接指向别的 skill，不覆盖"))
            continue
        if state is LinkState.BROKEN:
            if not force:
                outcomes.append(Outcome(
                    target.id, link_path, Result.REFUSED,
                    "已有断链；确认它属于本次操作后再加 --force 替换"))
                continue
            try:
                link_path.unlink()
            except OSError as exc:
                outcomes.append(Outcome(target.id, link_path, Result.FAILED,
                                        f"删除断链失败：{exc.strerror or exc}"))
                continue

        try:
            # 不用 rename：rename 会静默覆盖并发出现的内容。symlink 本身就是 no-replace 原语。
            os.symlink(skill.dir_path, link_path)
        except FileExistsError:
            outcomes.append(Outcome(target.id, link_path, Result.REFUSED,
                                    "创建瞬间出现同名条目，未覆盖"))
        except OSError as exc:
            outcomes.append(Outcome(target.id, link_path, Result.FAILED,
                                    exc.strerror or str(exc)))
        else:
            outcomes.append(Outcome(target.id, link_path, Result.CREATE,
                                    f"→ {skill.dir_path}"))
    return outcomes


def disable(skill: Skill, targets: list[AgentTarget]) -> list[Outcome]:
    """删除指向 ``skill`` 的链接。指向别处/真目录的位置一律不动。"""
    outcomes: list[Outcome] = []
    done_dirs: set[str] = set()

    for target in targets:
        link_path = target.dir / skill.link_name
        key = str(target.dir)
        if key in done_dirs:
            outcomes.append(Outcome(target.id, link_path, Result.ALREADY,
                                    "与前面的 agent 共用同一目录，已一次处理"))
            continue
        done_dirs.add(key)

        state = state_of(link_path, skill)
        if state is LinkState.ABSENT:
            outcomes.append(Outcome(target.id, link_path, Result.ALREADY, "本来就未启用"))
        elif state is LinkState.LINKED:
            try:
                # 只删链接本身：unlink 不跟随，仓库真身不受影响。
                link_path.unlink()
            except OSError as exc:
                outcomes.append(Outcome(target.id, link_path, Result.FAILED,
                                        exc.strerror or str(exc)))
            else:
                outcomes.append(Outcome(target.id, link_path, Result.REMOVE,
                                        "已停用（仓库真身保留）"))
        else:
            outcomes.append(Outcome(
                target.id, link_path, Result.REFUSED,
                f"位置是{_STATE_LABEL[state]}，不是本 skill 的链接，未改动"))
    return outcomes


def remove_orphan(link_path: Path) -> Outcome:
    """删除仓库残留链接（``status`` 报出来的孤儿）。只删链接，不递归。"""
    if not os.path.lexists(link_path):
        return Outcome("", link_path, Result.ALREADY, "已经不存在")
    if not link_path.is_symlink():
        return Outcome("", link_path, Result.REFUSED, "不是符号链接，未改动")
    try:
        link_path.unlink()
    except OSError as exc:
        return Outcome("", link_path, Result.FAILED, exc.strerror or str(exc))
    return Outcome("", link_path, Result.REMOVE, "已删除残留链接")
