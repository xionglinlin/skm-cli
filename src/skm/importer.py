"""收编：把散落在各处的 skill 真身收进仓库，并在原位置回填链接。

与 ``actions`` 的分工：``actions`` 只动链接，绝不碰真身；这里会**移动真身**，
所以界面上默认只出计划（``--yes`` 才真的动手）。安全底线与其他写操作一致：

* **不覆盖**：仓库里已有同名条目、同一次扫描出现两处同名真身，都拒绝并报告。
* **只收真身**：符号链接不是真身，一律跳过 —— 链接归 ``actions`` 管。
* **搬走的位置补回链接**：源就在某个 agent 目录里时，搬走后原地建链，
  agent 读到的内容分毫不变 —— 否则"收编"会顺手把 agent 的技能弄没。
* **默认移动，不复制**：真身只有一份是本工具的取舍（见 README 的「设计要点」）。
  复制会在 agent 目录里留下第二份真身，正是 skm 要消除的状态；``--copy`` 只给
  "源是 git 工作区、不想动它"这种场景用，并且不会回填链接。

目录深度固定两层，与仓库布局一致：``<根>/<skill>``、``<根>/<分类>/<skill>``。
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import scan
from .config import AgentTarget, Config
from .model import SKILL_FILE, Skill


class Verdict(str, Enum):
    """一处候选真身在计划里的去向。"""

    IMPORT = "import"   #: 收进仓库
    SKIP = "skip"       #: 无需改动（已在仓库 / 同一真身）
    REFUSE = "refuse"   #: 冲突，不处理


class Action(str, Enum):
    """执行结果。"""

    MOVED = "moved"        #: 真身已搬进仓库
    COPIED = "copied"      #: 真身已复制进仓库（源保留）
    PARTIAL = "partial"    #: 真身已入库，但链接没能回填（agent 侧需人工处理）
    REFUSED = "refused"
    FAILED = "failed"


@dataclass
class Candidate:
    """扫到的一处 skill 真身。"""

    source: Path                #: 现在的位置
    skill: Skill                #: 读出来的 skill（name / description / 告警）
    origin: str                 #: 来源描述（agent id 或"指定路径"）
    category: str | None = None  #: 源的两层结构给出的分类


@dataclass
class Item:
    """计划里的一条：把 ``candidate`` 放到 ``store_path``。"""

    candidate: Candidate
    store_path: Path            #: 仓库内的目标位置（绝对）
    rel: str                    #: 仓库内的相对路径，如 ``qt-skills/qt-qml``
    verdict: Verdict = Verdict.IMPORT
    detail: str = ""
    #: 搬走后补链接的位置。**规范位置** ``<agent>/<目录名>`` —— 与 ``enable`` 建的
    #: 链接同一处，否则 skm 自己认不出来（agent 侧的分层结构与链接名无关）。
    link_path: Path | None = None
    link_dir: Path | None = None   #: 该 agent 的根目录；用于清理被掏空的分类目录
    link_agent: str = ""           #: 该位置属于哪个 agent（显示用）

    @property
    def warnings(self) -> list[str]:
        return self.candidate.skill.warnings


@dataclass
class Root:
    """一个扫描起点。"""

    path: Path
    origin: str        #: 显示用来源（agent id 或"指定路径"）
    explicit: bool      #: 是否用户直接给的路径（决定它自己算不算一个 skill）


@dataclass
class Plan:
    config: Config
    items: list[Item] = field(default_factory=list)
    ignored: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def importing(self) -> list[Item]:
        return [i for i in self.items if i.verdict is Verdict.IMPORT]

    def count(self, verdict: Verdict) -> int:
        return sum(1 for i in self.items if i.verdict is verdict)


@dataclass
class Record:
    item: Item
    action: Action
    detail: str = ""


# --------------------------------------------------------------------------
# 找：哪些目录是真身
# --------------------------------------------------------------------------


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and (path / SKILL_FILE).is_file()


def discover(
    root: Path, *, origin: str, explicit: bool, warnings: list[str]
) -> tuple[list[Candidate], list[tuple[Path, str]]]:
    """从一个根目录里找出 skill 真身，返回 ``(候选, 被忽略的条目)``。

    只认真身（普通目录）：链接要么已经归 skm 管，要么指向别处的真身，
    两种情况都不该被"搬走"。不含 ``SKILL.md`` 的目录与链接会记进被忽略列表 ——
    用户指了个目录却什么都没扫到，得能看懂为什么。

    ``explicit`` 表示这是用户直接给的路径：这时它自己也可能是 skill（``skm import
    ~/x/my-skill``）。扫 agent 目录时**不**这样认 —— agent 目录里的 ``SKILL.md``
    不构成"一个 skill"，把它当 skill 会把整个 agent 目录搬进仓库。
    """
    found: list[Candidate] = []
    ignored: list[tuple[Path, str]] = []

    if root.is_symlink():
        warnings.append(f"{root} 是符号链接（→ {os.path.realpath(root)}），不是真身，未扫描")
        return found, ignored
    if not root.is_dir():
        warnings.append(f"{root} 不是目录，未扫描")
        return found, ignored

    if explicit and _is_skill_dir(root):
        found.append(Candidate(root, scan.make_skill(root), origin))
        return found, ignored

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            ignored.append((entry, "符号链接，不是真身"))
            continue
        if not entry.is_dir():
            continue  # README、.DS_Store 之类：不值得报
        if _is_skill_dir(entry):
            found.append(Candidate(entry, scan.make_skill(entry), origin))
            continue

        # 两层：<根>/<分类>/<skill>，分类名取自中间那层目录
        members = [sub for sub in sorted(entry.iterdir(), key=lambda p: p.name)
                   if not sub.name.startswith(".") and not sub.is_symlink()
                   and _is_skill_dir(sub)]
        if members:
            found.extend(
                Candidate(sub, scan.make_skill(sub, entry.name), origin, entry.name)
                for sub in members
            )
        else:
            ignored.append((entry, f"没有 {SKILL_FILE}"))
    return found, ignored


# --------------------------------------------------------------------------
# 计划：每处真身进仓库的哪个位置
# --------------------------------------------------------------------------


def _agent_owner(config: Config, source: Path) -> AgentTarget | None:
    """``source`` 落在哪个 agent 目录里 —— 决定搬走后要不要在原地回填链接。"""
    real = os.path.realpath(source)
    for agent in config.agents:
        base = os.path.realpath(agent.dir)
        if real == base or real.startswith(base + os.sep):
            return agent
    return None


def plan(
    config: Config,
    roots: list[Root],
    *,
    category: str | None = None,
    copy: bool = False,
) -> Plan:
    """出计划：收哪些、放哪、哪些不动以及为什么。不写任何东西。"""
    result = Plan(config=config)
    snapshot = scan.scan(config)
    result.warnings.extend(snapshot.warnings)

    store = config.store_dir
    store_real = os.path.realpath(store)
    seen: dict[str, Item] = {}      # realpath -> 本次已排入计划的条目
    taken: dict[str, Item] = {}     # 仓库内目标 -> 本次已排入计划的条目
    linked: dict[str, Item] = {}    # agent 侧链接位 -> 本次已排入计划的条目

    for root in roots:
        candidates, ignored = discover(
            root.path, origin=root.origin, explicit=root.explicit,
            warnings=result.warnings,
        )
        result.ignored.extend(ignored)
        if root.explicit and candidates and _agent_owner(config, root.path) is None:
            # 用户指的路径不在任何已配置的 agent 目录里：谁在读它我们不知道，
            # 搬走后也就无法回填链接。对普通目录这是对的，但如果它其实是某个
            # 还没配进来的 agent 目录（~/.claude/skills 之类），搬走就等于让那个
            # agent 悄悄少了几个 skill —— 必须说清楚，不能默默做完。
            result.warnings.append(
                f"{root.path} 不在配置里的任何 agent 目录下：收编后不会回填链接。"
                "若它是某个 agent 的加载目录，请先把它加进配置（编辑 [[agents]]），"
                "或用 --copy 保留原目录。"
            )
        for candidate in candidates:
            source = candidate.source
            real = candidate.skill.real_path
            group = category if category is not None else candidate.category
            rel = f"{group}/{candidate.skill.dirname}" if group else candidate.skill.dirname
            item = Item(candidate, store / rel, rel=rel)
            result.items.append(item)

            if real == store_real or real.startswith(store_real + os.sep):
                item.verdict, item.detail = Verdict.SKIP, "真身已经在仓库里"
            elif real in seen:
                item.verdict = Verdict.SKIP
                item.detail = (f"与 {seen[real].rel} 是同一真身"
                               f"（{seen[real].candidate.source}）")
            elif _same_entry(item.store_path, real):
                item.verdict, item.detail = Verdict.SKIP, "仓库里已有同一真身"
            elif not _placeable(item.store_path):
                item.verdict = Verdict.REFUSE
                item.detail = f"{item.store_path} 已被占用（不覆盖）"
            elif str(item.store_path) in taken:
                item.verdict = Verdict.REFUSE
                item.detail = f"与本次扫描的 {taken[str(item.store_path)].candidate.source} 同名"
            else:
                item.verdict = Verdict.IMPORT

            seen.setdefault(real, item)
            if item.verdict is not Verdict.IMPORT:
                continue
            _plan_link(config, item, real, linked, copy=copy)
            if item.verdict is Verdict.IMPORT:
                taken[str(item.store_path)] = item

    result.items.sort(key=lambda i: i.rel)
    return result


def _placeable(dest: Path) -> bool:
    """``dest`` 能不能放下：自身不存在、父目录不存在或是目录。"""
    if os.path.lexists(dest):
        return False
    parent = dest.parent
    return not os.path.lexists(parent) or parent.is_dir()


def _same_entry(path: Path, real: str) -> bool:
    """``path`` 是否已经是同一处真身（软链过去也算）—— 是的话就无需搬动。"""
    return os.path.lexists(path) and os.path.realpath(path) == real


def _plan_link(
    config: Config, item: Item, real: str, linked: dict[str, Item], *, copy: bool
) -> None:
    """给要收编的真身安排搬走后的链接位；冲突时说明原因，不硬来。

    链接必须落在 ``<agent>/<目录名>``：agent 侧的层数（``<分类>/<名字>``）只是
    真身的摆放方式，链接没有分类这一说。链接位在搬走后必须有东西 ——
    否则"收编"会顺手把 agent 正在用的 skill 弄没。
    """
    owner = _agent_owner(config, item.candidate.source)
    if owner is None:
        return  # 源不在任何 agent 目录里：这是纯粹的"放进来"，不需要回填

    if copy:
        # 复制模式下源目录留在原地，它就是 agent 正在读的那份真身。既复制又"假装
        # 收编"会让仓库里多出一份永远不生效的副本 —— 这个命令不该制造那种状态
        # （README 的「设计要点」第一条就是真身只有一份）。
        item.verdict = Verdict.REFUSE
        item.detail = (f"源在 {owner.id} 的加载目录里：复制会留下第二份真身。"
                       "要收编请去掉 --copy（真身搬进仓库，原位置自动补链接）")
        return

    link_path = owner.dir / item.candidate.skill.link_name
    item.link_path, item.link_dir, item.link_agent = link_path, owner.dir, owner.id

    # 按 realpath 判定：agent 目录本身可能是软链，此时"源"与"链接位"其实是同一处
    # （``~/.agents/skills`` → dotfiles 是常见布局）。
    if os.path.lexists(link_path):
        if link_path.is_symlink():
            if not _same_entry(link_path, real):
                item.verdict = Verdict.REFUSE
                item.detail = (f"{owner.id} 的链接位已被占用：{link_path} 指向别处 —— "
                               "搬走后它读不到这个 skill。先处理那个链接"
                               "（skm issues 可看它是什么），再收编这一处")
                return
            # 已经是"指向这处真身"的链接：搬走后它会断掉，正好由我们重指。
            item.detail = f"已存在的链接重指到仓库（{link_path}）"
        elif os.path.realpath(link_path) == real:
            item.detail = f"搬走后在原位置补链接（{owner.id} 读到的内容不变）"
        else:
            item.verdict = Verdict.REFUSE
            item.detail = (f"{owner.id} 的链接位被真目录占住：{link_path} —— "
                           "搬走后它读不到这个 skill。先处理那个条目"
                           "（skm issues 可看它是什么），再收编这一处")
            return
        linked[str(link_path)] = item
        return

    if str(link_path) in linked:
        item.detail = (f"与 {linked[str(link_path)].rel} 同名，链接位只归它"
                       "（收编后可用 skm issues 查看冲突）")
        return

    item.detail = f"搬走后补链接：{link_path}（{owner.id} 读到的内容不变）"
    if os.path.realpath(item.candidate.source.parent) != os.path.realpath(owner.dir):
        item.detail += "；源分类目录空了会被收掉"
    linked[str(link_path)] = item


# --------------------------------------------------------------------------
# 执行：搬动 + 回填链接
# --------------------------------------------------------------------------


def _move(source: Path, dest: Path) -> None:
    """把真身搬进仓库。同设备用 ``rename``（原子）；跨设备退化为复制 + 删除。"""
    try:
        os.rename(source, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # 跨设备：先整棵复制，成功后才删源。中途失败只会留下多余副本，不会丢数据。
    shutil.copytree(source, dest, symlinks=True)
    shutil.rmtree(source)


def execute(plan: Plan, *, copy: bool = False) -> list[Record]:
    """按计划搬动真身。只处理 ``IMPORT`` 项，单项失败不拖住其余项。

    目标位置在计划之后可能被别的进程占用，因此落盘前再确认一次 —— 不覆盖。
    """
    store = plan.config.store_dir
    if not store.is_dir():
        store.mkdir(parents=True, exist_ok=True)  # 失败由调用方接 OSError

    records: list[Record] = []
    for item in plan.importing():
        source = item.candidate.source
        dest = item.store_path

        if os.path.lexists(dest) and not _same_entry(dest, item.candidate.skill.real_path):
            records.append(Record(item, Action.REFUSED, "仓库里出现了同名条目，未覆盖"))
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if copy:
                shutil.copytree(source, dest, symlinks=True)
            else:
                _move(source, dest)
        except OSError as exc:
            verb = "复制" if copy else "搬动"
            records.append(
                Record(item, Action.FAILED, f"{verb}失败：{exc.strerror or exc}")
            )
            continue

        if copy:
            records.append(Record(
                item, Action.COPIED,
                "复制模式：源目录保留（仍是真身副本），未回填链接",
            ))
            continue

        detail, ok = _relink(item, dest)
        _prune_empty(item)
        records.append(Record(item, Action.MOVED if ok else Action.PARTIAL, detail))
    return records


def _relink(item: Item, dest: Path) -> tuple[str, bool]:
    """在 agent 目录里补上指向仓库真身的链接。搬走而不补 = agent 丢掉这个 skill。"""
    link_path = item.link_path
    if link_path is None:
        return "", True

    if os.path.lexists(link_path):
        # 只替换"我们自己的旧链接"：真身刚被搬走，它解析出来正是搬走前的位置。
        # 别的条目（真目录、指向别处的链接）一律不碰 —— 那是别人的数据。
        ours = link_path.is_symlink() and (
            not os.path.exists(link_path)
            or os.path.realpath(link_path) == item.candidate.skill.real_path
        )
        if not ours:
            return (f"{link_path} 已被别的条目占住，未补链接 —— "
                    f"真身已在 {dest}，需人工处理"), False
        try:
            link_path.unlink()
        except OSError as exc:
            return (f"无法替换旧链接（{exc.strerror or exc}）—— "
                    f"真身已在 {dest}，{item.link_agent} 现在读不到它"), False

    try:
        os.symlink(dest, link_path)
    except OSError as exc:
        return (f"链接未能建立：{exc.strerror or exc} —— 真身已在 {dest}，"
                f"{item.link_agent} 现在读不到它"), False
    return f"已补链接：{link_path} → {dest}", True


def _prune_empty(item: Item) -> None:
    """源真身被搬空后，把留下的空分类目录收掉（只收空目录，绝不递归删内容）。

    agent 侧原本是 ``<根>/<分类>/<名字>`` 时，搬走会留下空壳 —— agent 会把它
    当作一个"没有 SKILL.md 的目录"白白扫一遍。只删空目录是安全的边界：
    里面还留着任何东西就不动。
    """
    if item.link_dir is None:
        return
    # 边界按 realpath 比较：agent 根目录本身可能是软链，而我们扫的是它的真身，
    # 直接比字符串会让"根"这一步判不出来，一路往上删到 agent 目录之外。
    stop = os.path.realpath(item.link_dir)
    parent = item.candidate.source.parent
    try:
        while os.path.realpath(parent) != stop:
            # 硬边界：只处理 agent 根目录**以内**的那几层，任何越界立刻停手
            if not os.path.realpath(parent).startswith(stop + os.sep):
                return
            if not parent.is_dir() or any(parent.iterdir()):
                return
            parent.rmdir()
            parent = parent.parent
    except OSError:
        pass  # 删不掉（权限、并发出现新内容）不值得报错：空目录无害
