---
name: "release-builder"
description: "Automates semantic versioning, build, and release steps for this repo. Invoke when user asks for versioned build/release workflow or packaging guidance."
---

# 版本构建发布

适用于需要在仓库中进行语义化版本管理、构建产物、发布流程的任务。

## 触发场景
- 用户要求“版本号输入/更新后构建”
- 需要一键构建与发布脚本
- 需要校验语义化版本并写入项目版本位置

## 操作步骤
1. 识别版本来源与写入位置（优先 `pyproject.toml`，其次 `revpty/__init__.py`）。
2. 添加或更新构建脚本，要求支持：
   - 传入语义化版本号
   - 校验版本格式（SemVer）
   - 更新版本文件
   - 构建 sdist 与 wheel
3. 若存在发布流程：
   - 生成/更新变更记录（如已有约定）
   - 打包产物并输出到 `dist/`
4. 运行测试与 lint/typecheck（若仓库定义了命令）

## 语义化版本校验
默认规则：`MAJOR.MINOR.PATCH`，可选预发布与构建元数据  
示例：
- `1.2.3`
- `1.2.3-alpha.1`
- `1.2.3+build.7`

## 产物输出
默认输出到 `dist/`，支持自定义目录。

## 注意事项
- 不创建无关文档文件
- 优先修改已有脚本
- 不在日志中输出密钥
