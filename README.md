# NPU Conquer Probe

本地 Web 服务：通过 SSH 查询昇腾 NPU 占用情况（谁在用、来自哪个 IP、模型/启动参数），支持多机 TAB（如 A2 / 310）与一键结束进程。

## 技术栈

- 后端：Python · FastAPI · Uvicorn · Paramiko
- 前端：原生 HTML / CSS / JS
- 远程探针：`remote_probe.py`（`npu-smi` + `/proc` + SSH 会话）

## 快速开始

```powershell
copy .env.example .env
# 编辑 .env，填写 NPU_HOSTS=名称|IP|用户|密码
.\run.ps1
```

浏览器打开 http://127.0.0.1:8787

## 配置示例

```env
NPU_HOSTS=A2|192.168.9.179|root|your_password,310|192.168.13.119|root|your_password
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8787
REFRESH_SECONDS=8
```

**不要**把真实密码提交进仓库；仅使用本地 `.env`（已在 `.gitignore`）。

## 目录

```
app/            # FastAPI 服务与采集逻辑
static/         # 前端页面
remote_probe.py # 上传到远端执行的探针
run.ps1         # Windows 一键启动
```
