# Nix 外部项目集成

外部 Godot 项目通过 Nix flake inputs 管理，锁定提交号和内容哈希确保可重现构建。

## 当前项目

| 项目 | 来源 | 提交 | 许可 | 路径 |
|------|------|------|------|------|
| Truck Town | [godot-demo-projects](https://github.com/godotengine/godot-demo-projects) | `ad5bb61` | MIT | `3d/truck_town` |

环境变量 `CHONGMING_EXTERNAL_TRUCK_TOWN` 在 `nix develop` 后自动设置。

## 添加新项目

1. 计算内容哈希：

```bash
nix-prefetch-url --unpack https://github.com/owner/repo/archive/<commit>.tar.gz
```

2. 在 `flake.nix` 的 `inputs` 添加：

```nix
inputs.new-project = {
  url = "github:owner/repo/<commit>";
  flake = false;
};
```

3. 在 `runtimeEnv` 导出路径：

```nix
CHONGMING_EXTERNAL_NEW = "${inputs.new-project}/path";
```

4. 更新锁文件：

```bash
nix flake update
```

## 实现细节

Nix store 文件只读，系统启动时：
1. 复制外部项目到临时目录（排除 `.godot` 和 `project.godot`）
2. 递归设置文件和目录为可写（`topdown=False` 先文件后目录）
3. 叠加本地 worker 项目文件
4. Godot 重新导入资源

相关代码：`src/chongming/rendering/godot.py:536-554`
