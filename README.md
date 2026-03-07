# Twitter → Binance Square 自动搬运工具

自动采集 Twitter/X 推文，转换内容格式后发布到币安广场（Binance Square）。

## 目录结构

```
twitter-to-binance-square/
├── SKILL.md                        # Skill 定义（入口文件）
├── README.md                       # 使用说明（本文件）
├── mirror_config.example.json      # 配置模板
└── scripts/
    └── auto_mirror.py              # 自动化脚本
```

## 快速开始

### 1. 准备凭证

| 凭证 | 获取方式 |
|------|---------|
| `TWITTER_TOKEN` | 访问 https://6551.io/mcp 注册并获取 API Token |
| `SQUARE_API_KEY` | 登录币安后访问 [创作者中心](https://www.binance.com/zh-CN/square/creator-center/home)，在页面右侧点击「查看 API」申请 OpenAPI Key |

### 2. 设置环境变量

```bash
export TWITTER_TOKEN="your_6551_token_here"
export SQUARE_API_KEY="your_square_api_key_here"
```

Windows (PowerShell):
```powershell
$env:TWITTER_TOKEN = "your_6551_token_here"
$env:SQUARE_API_KEY = "your_square_api_key_here"
```

### 3. 运行

以下命令均从 `twitter-to-binance-square/` 目录下执行。

#### 方式一：监控指定账号

```bash
python scripts/auto_mirror.py --mode account --accounts VitalikButerin,elonmusk --interval 300
```

#### 方式二：监控关键词话题

```bash
python scripts/auto_mirror.py --mode search --keywords "bitcoin ETF" --min-likes 100 --interval 600
```

#### 方式三：监控 Hashtag

```bash
python scripts/auto_mirror.py --mode hashtag --hashtag bitcoin --min-likes 500 --interval 600
```

#### 方式四：使用配置文件

```bash
cp mirror_config.example.json mirror_config.json
# 编辑 mirror_config.json 修改配置
python scripts/auto_mirror.py --config mirror_config.json
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--config`, `-c` | JSON 配置文件路径 |
| `--mode` | 监控模式：`account` / `search` / `hashtag` |
| `--accounts` | 逗号分隔的 Twitter 用户名 |
| `--keywords` | 搜索关键词 |
| `--hashtag` | 监控的 Hashtag（不含 #） |
| `--interval` | 轮询间隔（秒），默认 300 |
| `--min-likes` | 最低点赞数阈值 |
| `--max-posts` | 每轮最大发帖数 |
| `--translate` | 开启翻译 |
| `--translate-to` | 翻译目标语言代码（默认 zh） |
| `--dry-run` | 预览模式，只输出不发帖 |
| `--once` | 只执行一轮后退出 |
| `--state-file` | 状态文件路径 |

## 典型用法

### 先预览，再正式运行

```bash
# 第一步：干跑预览，确认内容格式
python scripts/auto_mirror.py --mode account --accounts VitalikButerin --dry-run --once

# 第二步：正式运行
python scripts/auto_mirror.py --mode account --accounts VitalikButerin --interval 300
```

### 后台常驻运行

Linux/Mac:
```bash
nohup python scripts/auto_mirror.py --config mirror_config.json > mirror.log 2>&1 &
```

Windows (PowerShell):
```powershell
Start-Process python -ArgumentList "scripts/auto_mirror.py", "--config", "mirror_config.json" -NoNewWindow -RedirectStandardOutput "mirror.log"
```

### 定时任务 (Cron)

每小时执行一次单轮：
```cron
0 * * * * cd /path/to/twitter-to-binance-square && TWITTER_TOKEN=xxx SQUARE_API_KEY=xxx python scripts/auto_mirror.py --config mirror_config.json --once >> mirror.log 2>&1
```

## 状态文件

脚本会自动创建 `mirror_state.json` 用于：
- 记录已发布的推文 ID（防止重复发帖）
- 跟踪每日发帖数量
- 保存发帖日志

## 注意事项

1. **币安广场每日最多发帖 100 条**，脚本会自动检测并停止
2. **内容敏感词**会被币安过滤，被拦截的推文会自动跳过
3. **务必标注来源**，模板中默认包含 `Source: @username on X`
4. **首次运行建议使用 `--dry-run`** 确认内容格式
5. 无外部依赖，仅使用 Python 标准库
