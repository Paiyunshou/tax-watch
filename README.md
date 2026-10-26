# 财税公告监管（tax-watch）

GitHub Actions 定时抓取财政部 / 税务总局 / 外汇局等公开政策页面，筛出**新增**条目，
用 LLM 结构化提成 JSON，推送到 Hermes，再由 Hermes 组装成飞书卡片发给你。

**明确不做的事**：不自动登录任何网站、不保存账号密码、不自动写入思源笔记。
有价值的政策由你人工确认后再手动归档。

---

## 一、数据流

```
GitHub Actions (每 3 小时)
   └─ watch.py
        读 sources.yaml → 抓列表 → 和 seen.json 比对 → 只处理新增
        → 抓详情页正文 → LLM 提 JSON → 推送
                │
                ├─(通道 A，有公网地址时) POST → Hermes webhook_receiver.py → 飞书
                └─(通道 B，默认) 写仓库 outbox/*.json
                                          ↓
                             Hermes 每 30 分钟跑 pull_outbox.py → 飞书
```

**通道怎么选**：GitHub 的机器在公网，你的 NAS 在内网，Actions 直接 POST 内网地址是打不通的。
所以默认走通道 B（不需要任何穿透，最省事）：Actions 只负责把事件写进仓库的 `outbox/`，
Hermes 定时来拉。除非你以后做了内网穿透并拿到公网地址，再配 `HERMES_WEBHOOK_URL` 走通道 A。
两条通道都开着也不会重复发——`processed.json` 按 event_id 去重。

---

## 二、目录结构

```
tax-watch/
├── sources.yaml                  ★ 唯一需要你改的文件：监控哪些页面
├── watch.py                      抓取主程序
├── prompt.txt                    LLM 结构化提取的 prompt（可直接改口径）
├── requirements.txt
├── scripts/summary.py            把结果写进 Actions 运行摘要
├── seen.json                     （自动生成）已抓过的链接，脚本自己提交
├── outbox/                       （自动生成）推送失败/待拉取的事件
├── .github/workflows/tax-watch.yml
└── hermes_side/                  跑在 NAS 容器里的一套
    ├── feishu.py                 组装飞书卡片 + 发送
    ├── webhook_receiver.py       通道 A：HTTP 接收端
    └── pull_outbox.py            通道 B：从仓库拉 outbox 补发
```

---

## 三、本地测试（先在自己电脑上确认能抓）

需要 Python 3.10+。在项目目录里：

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 看配置文件里有哪些源、哪些会被跳过
python watch.py --list-sources
#   输出：✓ 表示会抓，✗ 表示 enabled:false 或 type:auth-cookie

# 3. 空跑：只抓取和解析，不调 LLM、不推送、不写状态
python watch.py --dry-run --no-llm --source 财政部-政策发布 --limit 2
#   成功时会看到：
#     → 《城乡义务教育补助经费管理办法》修订印发
#     [dry-run] 正文 3987 字 → 预览 outbox/dryrun/be08b6e1b59ca07b.json

# 4. 打开预览文件确认内容对不对（重点看 text_excerpt 是不是正文，不是导航栏）
cat outbox/dryrun/be08b6e1b59ca07b.json

# 5. 想看 LLM 提取效果，配上 key 再跑（不给 --no-llm）
export LLM_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1      # 用国内兼容服务就换成对应的
export LLM_MODEL=gpt-4o-mini
python watch.py --dry-run --source 税务总局-财税文件 --limit 1
```

> `--dry-run` 不会污染 `seen.json`，可以反复跑。
> 想从头再抓一遍，删掉 `seen.json` 即可。

三个源已实测可抓（2026-10）：税务总局法规库（接口）、财政部政策发布、外汇局政策法规。
海关总署当前有 WAF + 证书链问题（返回 412），配置里按规则标成 `auth-cookie` 并跳过。

---

## 四、GitHub 仓库创建与 Secrets 配置

### 1. 建仓库

网页操作：

1. 打开 https://github.com/new
2. **Repository name** 填 `tax-watch`，选 **Private**（推荐，抓到的政策清单不必公开）
3. 点 **Create repository**
4. 在仓库页点 **uploading an existing file**，把这个目录里的所有文件拖进去
   （至少要有：`sources.yaml`、`watch.py`、`prompt.txt`、`requirements.txt`、`scripts/`、`.github/`）
5. 点 **Commit changes**

> 注意 `.github/workflows/` 必须以目录形式上传，否则 Actions 不会出现。
> Windows 资源管理器看不到以 `.` 开头的文件夹，上传时把整个 `tax-watch` 文件夹拖进去最稳。

### 2. 配 Secrets

仓库页 → **Settings** → 左侧 **Secrets and variables** → **Actions** → 右上 **New repository secret**，
逐个添加：

| Name | 必填 | 说明 |
|---|---|---|
| `LLM_KEY` | 是 | LLM 接口的 API Key |
| `LLM_BASE_URL` | 否 | 兼容 OpenAI 的接口地址，不填默认 `https://api.openai.com/v1` |
| `LLM_MODEL` | 否 | 模型名，不填默认 `gpt-4o-mini` |
| `HERMES_WEBHOOK_URL` | 否 | 通道 A 用。内网地址填了也连不上，**建议先留空** |
| `HERMES_WEBHOOK_TOKEN` | 否 | 通道 A 的校验口令，和接收端 `WATCH_TOKEN` 一致 |

**Secrets 只在这里加，任何代码里都不写密钥。**

### 3. 跑一次

仓库页 → **Actions** → 左侧 **tax-watch** → 右侧 **Run workflow** → 绿色按钮。
跑完点进去看日志，最后会有一张「本次运行」的表格，列出每个源已记录多少条。

> GitHub 的定时任务有两个坑：① 不保证准时，通常延迟 5–20 分钟；
> ② 仓库连续 60 天没有提交会被自动停用定时任务——这个项目每次运行都会提交 `seen.json`，
> 所以只要还在抓到东西就不会触发；如果长期 0 新增，手动跑一次即可。

---

## 五、新增监控页面：只改 sources.yaml

在 `sources:` 下面追加一段，然后提交。**不需要动任何代码。**

```yaml
  - name: 某某省税务局-通知公告          # 唯一，会显示在飞书消息里
    type: public                       # 公开页面就写 public
    enabled: true
    mode: html
    url: https://xxx.gov.cn/tzgg/       # 列表页地址
    base_url: https://xxx.gov.cn        # 用来拼相对链接
    list_selector: "ul.news-list li"    # 每条公告的选择器
    title_selector: "a"
    link_selector: "a"
    date_selector: "span.date"          # 没有就删掉这一行
    content_selector: "div.article"     # 正文容器，不写会自动猜
    keywords: ["税", "退税", "关税"]     # 只保留标题含这些词的
```

**选择器怎么找**（不用懂 CSS）：

1. 浏览器打开列表页，在一条公告标题上**右键 → 检查**
2. 右侧元素面板里，这条公告通常包在 `<li>` 或 `<div class="...">` 里，
   往上找一层能包住**所有**公告条目的标签，把它的 `class` 抄下来就得到 `list_selector`
3. 同一层里如果是 `<ul class="news-list">`，`list_selector` 就写 `"ul.news-list li"`
4. 用 `python watch.py --dry-run --no-llm --source 新源名称 --limit 1` 验证能不能抓到

如果列表页是 JS 动态渲染（右键查看源代码，列表内容为空），说明要用接口模式：
按 Ctrl+U 看源码里搜 `channelId` 或 `/api/`，拿到接口地址和参数，
照 `税务总局-税务规范性文件` 那段 `mode: json_api` 的写法配置。

**需要登录才能看的页面**：`type: auth-cookie` + `enabled: false`。脚本会跳过并打印一行日志，
不会尝试任何登录操作。这是硬规则，不要改。

---

## 六、Hermes 侧部署（跑在 NAS 容器里）

前提：`/opt/data/.env` 里已有 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_HOME_CHANNEL`
（Hermes 自己就在用这套凭证）。脚本优先用这套凭证直发飞书，不用另外建机器人。

### 通道 B（默认，推荐）：定时拉 outbox

```bash
# 1. 一次性验证：能不能拉到、能不能发
cd /opt/data/projects/tax-watch/hermes_side
set -a; . /opt/data/.env; set +a
export GITHUB_REPO=Paiyunshou/tax-watch     # 改成你的 owner/repo
export GITHUB_TOKEN=ghp_xxx                 # 私有仓库必填（只要 contents 读权限）
python3 pull_outbox.py --dry-run            # 先看有哪些待发
python3 pull_outbox.py                      # 真发

# 2. 加定时（容器内 crontab -e）
*/30 * * * * cd /opt/data/projects/tax-watch/hermes_side && set -a && . /opt/data/.env && set +a && GITHUB_REPO=Paiyunshou/tax-watch GITHUB_TOKEN=ghp_xxx /usr/local/bin/python3 pull_outbox.py >> /opt/data/logs/tax-watch.log 2>&1
```

### 通道 A（可选）：开 HTTP 接收端

```bash
export WATCH_TOKEN='一串随机字符'          # 和 GitHub Secret 的 HERMES_WEBHOOK_TOKEN 一致
cd /opt/data/projects/tax-watch/hermes_side
python3 webhook_receiver.py --port 8010
# 自测（不起服务，直接把一个事件文件走一遍）：
python3 webhook_receiver.py --replay ../outbox/xxx.json --dry-run
```

只有 NAS 有公网可达地址（内网穿透 / 端口映射）时才值得开这个。

### 自检清单

```bash
curl -s localhost:8010/health          # {"ok": true, ...}
python3 webhook_receiver.py --replay ../outbox/xxx.json --dry-run   # 打印卡片 JSON，看一眼排版
```

`hermes_side/data/` 下会留两份记录：`events.jsonl`（每条收到的事件）和
`processed.json`（已发送的 event_id，用于去重）。删掉 `processed.json` 会导致历史事件重发。

---

## 七、推给 Hermes 的 JSON 字段（固定）

```json
{
  "schema_version": "1.0",
  "event_id": "e28cccce8a0c2c97",
  "source": "税务总局-财税文件",
  "source_name": "税务总局-财税文件",
  "type": "public",
  "url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5252906/content.html",
  "title": "财政部 税务总局关于调整部分产品出口退税政策的公告",
  "published_at": "2026-10-12",
  "fetched_at": "2026-10-26T20:43:59+08:00",
  "meta": { "发文字号": "...", "发文单位": "...", "成文日期": "...", "时效性": "全文有效", "效力等级": "财税文件" },
  "doc": {
    "title_clean": "", "doc_no": "", "issuer": "", "doc_type": "",
    "signed_date": "", "effective_date": "", "expire_date": "", "abolish_note": "",
    "business_tags": [], "summary": "", "key_changes": [], "scenarios": [],
    "affected": "", "is_relevant": true, "risk_level": "低", "need_read_full": false
  },
  "text_excerpt": "正文前 3000 字",
  "attachments": [{"name": "新能源汽车产品清单.pdf", "url": "https://..."}],
  "llm_model": "gpt-4o-mini",
  "llm_ok": true
}
```

`meta` 是法规库接口自带的官方元数据（发文字号、成文日期、时效性等），
比 LLM 抽取更准；代码里在 LLM 字段为空时用它回填。`attachments` 是正文里的
PDF/Word/Excel 附件链接（公告常写「具体清单见附件」），飞书卡片里会列成可点的链接。

---

## 八、已知限制

1. **Actions 在境外**，抓中国政务站可能比国内慢或被拦。已在三个源上验证可抓，
   新增源时务必先本地 `--dry-run` 验证一次。
2. **海关总署**当前 412 + 证书链异常，已按规则跳过，需要换公开镜像栏目才能接。
3. **正文在附件里**（PDF/Word）的公告抓不到正文，`doc.need_read_full` 会提醒你点原文。
4. **首次运行会一次性推最多 15×N 条**（每源 15 条上限）。想安静上线，
   先把 `max_new_per_source` 调成 1，或者先在本地跑一次把 `seen.json` 提交上去。
5. **不做正文 OCR、不做附件下载**。要这些再加，成本会明显上升。
