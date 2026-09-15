"""配置：仓库位置与 agent 加载路径。

配置放 ``<base>/config.toml``（base 默认 ``~/.skill-manager``）。

**只存文件系统推导不出来的东西**：agent 的加载目录、显示名、是否参与管理。
刻意不存"某个 skill 是否启用" —— 链接在不在就是真相，存了必然产生
"配置说该有、文件系统说没有"的分裂，而本工具存在的意义正是消除这种分裂。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: 配置 schema 版本。
CONFIG_VERSION = 1

#: 内置默认：OMP 当前唯一需要铺链接的目录，也是后续新增 agent 的样板。
#: ``~/.agents/skills`` 是 OMP 直读的规范位置（discovery 的 agents provider）。
DEFAULT_AGENTS: tuple[dict[str, object], ...] = (
    {"id": "omp", "name": "Oh My Pi (OMP)", "dir": "~/.agents/skills",
     "shared": True, "enabled": True},
)

_CONFIG_HEADER = """\
# skm 配置 —— skill 仓库位置与各 agent 的加载目录
#
# 「启用 / 停用」= 在 agent 目录里增加 / 删除一条指向仓库真身的符号链接。
# 这里只描述 agent 的加载目录，不记录任何 skill 的启用状态
# —— 链接在不在就是唯一真相。
#
# 新增一个 agent：复制一段 [[agents]]，改 id / name / dir 即可。
#   dir      该 agent 读取 skill 的目录（支持 ~ 与 $VAR 展开）
#   enabled  false = 不再向它铺链接（不会删除已有链接）
#   shared   true = 与其他 agent 共用同一目录，动一处影响全部
#
# 已核实的加载路径：
#   OMP          ~/.agents/skills    （OMP 直读的共享目录，多个工具共用）
#   Codex        ~/.codex/skills
#   Claude Code  ~/.claude/skills
"""


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


@dataclass
class AgentTarget:
    """一个 agent 的 skill 加载目录 —— 也就是铺链接的目标。"""

    id: str
    name: str
    dir: Path
    shared: bool = False
    #: 是否参与管理。关掉只是不再向它铺链接，不会去删已有链接。
    enabled: bool = True

    @property
    def present(self) -> bool:
        """目录在不在。不在就跳过 —— skm 不替 agent 创建它的目录。"""
        return self.dir.is_dir()


@dataclass
class Config:
    base_dir: Path
    agents: list[AgentTarget] = field(default_factory=list)
    path: Path | None = None            #: 实际读到的配置文件；None = 无文件，用内置默认
    warnings: list[str] = field(default_factory=list)

    @property
    def store_dir(self) -> Path:
        """仓库（真身）目录。"""
        return self.base_dir / "skills"

    def agent(self, agent_id: str) -> AgentTarget | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def link_targets(self) -> list[AgentTarget]:
        """真正可写的目标：参与管理、且目录已存在。"""
        return [a for a in self.agents if a.enabled and a.present]


def default_base_dir() -> Path:
    """``SKM_BASE_DIR`` 可覆盖（测试 / 多仓库用）。"""
    override = os.environ.get("SKM_BASE_DIR")
    return _expand(override) if override else Path.home() / ".skill-manager"


def config_path(base_dir: Path) -> Path:
    override = os.environ.get("SKM_CONFIG")
    return _expand(override) if override else base_dir / "config.toml"


def _agent_from_table(raw: dict, index: int, warnings: list[str]) -> AgentTarget | None:
    agent_id = str(raw.get("id", "")).strip()
    directory = str(raw.get("dir", "")).strip()
    if not agent_id or not directory:
        warnings.append(f"agents[{index}]：缺少 id 或 dir，已忽略")
        return None
    return AgentTarget(
        id=agent_id,
        name=str(raw.get("name", agent_id)),
        dir=_expand(directory),
        shared=bool(raw.get("shared", False)),
        enabled=bool(raw.get("enabled", True)),
    )


def _builtin_agents() -> list[AgentTarget]:
    return [
        AgentTarget(
            id=str(item["id"]),
            name=str(item["name"]),
            dir=_expand(str(item["dir"])),
            shared=bool(item["shared"]),
            enabled=bool(item["enabled"]),
        )
        for item in DEFAULT_AGENTS
    ]


def load(base_dir: Path | None = None) -> Config:
    """读配置。无文件时用内置默认（不自动落盘；``skm config init`` 才创建）。"""
    base = Path(base_dir) if base_dir else default_base_dir()
    path = config_path(base)
    warnings: list[str] = []
    raw: dict = {}
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            # 配置坏了**不能猜**：退回内置默认，并把原因如实说出来。
            warnings.append(f"配置文件读不动（{exc}），本次使用内置默认值：{path}")
            raw = {}
    # 没有配置文件是正常状态：内置默认就是正确的 OMP 目标。
    # 不在这里告警 —— 每个命令都刷一遍"你没配过"只是噪音；`skm agents` 会如实显示。

    base_value = raw.get("base_dir")
    if isinstance(base_value, str) and base_value.strip():
        base = _expand(base_value)

    version = raw.get("version", CONFIG_VERSION)
    if isinstance(version, int) and version > CONFIG_VERSION:
        warnings.append(
            f"配置版本 {version} 高于本程序支持的 {CONFIG_VERSION}，"
            "更新的字段会被忽略（不猜测其含义）"
        )

    agents: list[AgentTarget] = []
    raw_agents = raw.get("agents")
    if isinstance(raw_agents, list) and raw_agents:
        for index, item in enumerate(raw_agents):
            if not isinstance(item, dict):
                warnings.append(f"agents[{index}]：不是表，已忽略")
                continue
            agent = _agent_from_table(item, index, warnings)
            if agent is None:
                continue
            if any(existing.id == agent.id for existing in agents):
                warnings.append(f"agents[{index}]：id 重复（{agent.id}），已忽略")
                continue
            agents.append(agent)
    else:
        agents = _builtin_agents()

    seen: dict[str, str] = {}
    for agent in agents:
        key = str(agent.dir)
        if key in seen:
            # 共用目录必须显式标出来：一次写入影响全部共用者，界面不能装作只动了一个。
            agent.shared = True
            warnings.append(
                f"{agent.id} 与 {seen[key]} 共用同一目录 {agent.dir}（改一处影响两者）"
            )
        else:
            seen[key] = agent.id

    return Config(base, agents, path if path.is_file() else None, warnings)


def render_default(base_dir: Path) -> str:
    """生成默认配置文件文本（``skm config init`` 用）。"""
    parts = [_CONFIG_HEADER, f"version = {CONFIG_VERSION}", f'base_dir = "{base_dir}"', ""]
    for item in DEFAULT_AGENTS:
        parts += [
            "[[agents]]",
            f'id = "{item["id"]}"',
            f'name = "{item["name"]}"',
            f'dir = "{item["dir"]}"',
            f'shared = {str(item["shared"]).lower()}',
            f'enabled = {str(item["enabled"]).lower()}',
            "",
        ]
    parts += [
        "# 其他 agent 的样板：确认目录确实存在后，把下面几行前的 # 去掉即可。",
        "# 目录不存在时 skm 会跳过，不会替 agent 创建目录。",
        "#",
        "# [[agents]]",
        '# id = "codex"',
        '# name = "Codex CLI"',
        '# dir = "~/.codex/skills"',
        "# shared = false",
        "# enabled = true",
        "#",
        "# [[agents]]",
        '# id = "claude"',
        '# name = "Claude Code"',
        '# dir = "~/.claude/skills"',
        "# shared = false",
        "# enabled = true",
        "",
    ]
    return "\n".join(parts)
