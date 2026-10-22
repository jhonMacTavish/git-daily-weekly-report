# Git 日报周报 Skill

一个 Codex skill：根据本地 Git 提交生成中文日报和周报，支持按日期、作者、分支及多个仓库统计。

## 功能

- 日报默认统计今天，周报默认统计本周一至当前时刻
- 默认只统计当前 Git 用户，支持合并姓名和邮箱变体
- 自动汇总提交数、涉及文件数、代码行数和时间跨度
- 自动识别 Vue、React、Next.js、Nuxt、Angular 和 Svelte 等技术栈
- 输出面向主管的成果汇报，保留短哈希用于追溯
- 报告末尾附带无文件名的功能型复制代码块

## 使用

```text
$git-daily-weekly-report 生成今天的日报
$git-daily-weekly-report 生成本周周报
```

采集脚本也可以独立运行：

```bash
python3 scripts/collect_commits.py \
  --period weekly \
  --repo /path/to/repository \
  --project-name "项目名称"
```

常用参数：

```text
--period daily|weekly
--date YYYY-MM-DD
--since YYYY-MM-DD --until YYYY-MM-DD
--repo PATH
--author EMAIL_OR_NAME
--all-authors
--author-alias NAME_OR_EMAIL
--all-refs
--include-merges
```

## 安装

将本仓库放入 Codex 用户级 skills 目录：

```bash
mkdir -p ~/.codex/skills
git clone <repository-url> ~/.codex/skills/git-daily-weekly-report
```

依赖 Python 3.9+ 和 Git。
