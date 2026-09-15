"""skm —— 本地 skill 仓库的启用/停用管理器（只依赖标准库）。

模型（与 docs/00-需求决策记录.md 的冻结决策一致）：

* **文件系统是唯一事实来源**。某个 skill 是否启用 = 某个 agent 的 skills
  目录里有没有一条指向仓库真身的符号链接。不存任何"期望状态"。
* **仓库 = 真身**，`<base>/skills/`。分类就是仓库里的上级目录名，
  没有单独的元数据。深度固定两层：
      `<base>/skills/<skill>/SKILL.md`          —— 未分类
      `<base>/skills/<category>/<skill>/SKILL.md` —— 已分类
* **agent 目标目录来自配置**，默认只有 OMP 共享的 `~/.agents/skills`；
  以后加 Codex / Claude Code 只是往配置里加一条，不改代码。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
