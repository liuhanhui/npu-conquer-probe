# NPU Conquer Probe

本地 **Node.js** Web 服务：通过 SSH 查询昇腾 NPU 占用（谁在用、来自哪个 IP、模型/启动参数），支持多机 TAB（A2 / 310）与一键结束进程。

## 技术栈

- 本地服务：**Node.js ≥ 18** · Express · ssh2 · dotenv
- 前端：原生 HTML / CSS / JS（`static/`）
- 远程探针：`remote_probe.py`（上传到昇腾机用 `python3` 执行，机器上需有 Python / `npu-smi`）

## 快速开始

```powershell
copy .env.example .env
# 编辑 .env
.\run.ps1
```

或：

```bash
npm install
npm start
```

打开 http://127.0.0.1:8787

## 配置示例

```env
NPU_HOSTS=A2|192.168.9.179|root|your_password,310|192.168.13.119|root|your_password
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8787
REFRESH_SECONDS=8
```

不要把真实密码提交进仓库；使用本地 `.env`。

## 目录

```
server/           # Express 服务与 SSH 采集
static/           # 前端
remote_probe.py   # 远端探针（仍为 Python，跑在昇腾机上）
package.json
run.ps1
```
