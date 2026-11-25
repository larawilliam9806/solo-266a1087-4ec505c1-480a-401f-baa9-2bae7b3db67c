# 借展箱交运验封核查（Flask + SQLite + Docker Compose）

把同一只借展箱的两份分钟级 CSV 流水合并成一个偏序，判断交运环节：

- **必然合格**（`always_compliant`）
- **必然不合格**（`always_noncompliant`）
- **两者都有可能**（`ambiguous`），并分别给出一条可行的合格/不合格记录号顺序

支持在页面上补记一条**跨表记录先后关系**后重新判定。补记与分钟、行序或已有补记冲突时返回 `409`/页面错误，不写入冲突补记，原有补记和当前结论保持不变。

案例页还可以为登记员生成**核查凭据**：在同一个 SQLite 快照事务中冻结原始两份 CSV、当时已接受的补记、当前结论版本以及按文件名字段排序的 SHA-256 清单，打成 ZIP 返回凭据编号。凭据一经生成即不可变，之后案例出现新结论也不会改写旧包；生成期间若有补记提交，或打包、写入失败，整个事务回滚，不留下半包。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:5000>。健康检查：

```bash
curl http://localhost:5000/health
```

SQLite 数据库保存在 Compose 命名卷 `box-seal-data` 中，容器内路径为 `/app/data/verification.db`。

也可以同时使用 `docker-compose.yml`；`compose.yaml` 内容相同，兼容不同 Compose 调用习惯。

## 本地直接运行

```bash
pip install -r requirements.txt
python app.py
# 默认 http://127.0.0.1:5000
```

可通过 `DATABASE_PATH` 和 `PORT` 环境变量修改数据库路径与端口。

## CSV 格式

两份文件都应包含三列，推荐使用中文表头：

```csv
记录号,分钟时间,动作
W-001,2026-11-24 10:00,换签
W-002,2026-11-24 10:05,交运
```

质检：

```csv
记录号,分钟时间,动作
Q-001,2026-11-24 10:05,验封通过
```

支持的动作及常见别名：

| 表 | 标准动作 | 内部类型 |
|---|---|---|
| 库房 | `换签` | `relabel` |
| 库房 | `交运`（恰好一笔） | `shipment` |
| 质检 | `验封通过` | `pass` |
| 质检 | `验封失败` | `fail` |

时间支持 `YYYY-MM-DD HH:MM`、`YYYY/MM/DD HH:MM`、中文日期、`MM-DD HH:MM`、`HH:MM`、12 位紧凑时间；秒会被截到分钟。编码支持 UTF-8/UTF-8 BOM/GB18030，分隔符支持逗号、分号和 Tab。

## 判定规则

已知顺序：

1. 不同分钟：按分钟时间；
2. 同表同分钟：按 CSV 行序；
3. 跨表同分钟：默认未知，可由补记增加先后约束；
4. 补记还可以明确任意两条跨表记录的先后；若与偏序形成环，则视为冲突并退回。

合格条件是：交运前有验封通过，且**最后一次通过到交运之间没有换签或验封失败**。

由于所有动作只属于 `通过 / 失败 / 换签 / 交运`，在一条完整可行顺序中：

- 交运的前一条是“验封通过” ⇒ 合格；
- 交运的前一条是“换签/验封失败”，或交运是第一条 ⇒ 不合格。

系统不会枚举同分钟的排列数，而是构造偏序图并用位集计算可达关系。对候选记录 `x` 和交运 `s`，`x` 能在某条线性扩展中紧邻 `s` 当且仅当：

- `s` 不会必然在 `x` 之前；
- 不存在记录 `y` 被强制满足 `x < y < s`。

随后通过收缩 `(x, s)` 节点并进行拓扑排序，构造一条真实可行的完整记录号顺序。对每个“通过”和“坏事件”最多构造一次见证顺序，因此几百条同分钟记录也能快速完成。

## HTTP API

### 创建核查案例

```bash
CID=$(curl -sS -X POST http://localhost:5000/api/cases \
  -F box_code=BOX-017 \
  -F warehouse_file=@samples/warehouse.csv \
  -F qc_file=@samples/qc.csv \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["case"]["id"])')

echo "$CID"
curl -sS "http://localhost:5000/api/cases/$CID"
```

返回的 `conclusion` 包含：

```json
{
  "verdict": "ambiguous",
  "verdict_text": "两者都有可能",
  "compliant_possible": true,
  "noncompliant_possible": true,
  "compliant_order": ["W::W-001", "Q::Q-001", "W::W-002"],
  "noncompliant_order": ["W::W-001", "W::W-002", "Q::Q-001"],
  "compliant_order_record_numbers": ["W-001", "Q-001", "W-002"],
  "noncompliant_order_record_numbers": ["W-001", "W-002", "Q-001"]
}
```

UID 使用 `W::记录号` 和 `Q::记录号`，即使两份 CSV 中存在相同记录号也能区分。

### 补记跨表先后

让交运 `W-002` 早于质检通过 `Q-001`：

```bash
curl -i -X POST "http://localhost:5000/api/cases/$CID/supplements" \
  -H 'Content-Type: application/json' \
  -d '{"before_uid":"W::W-002","after_uid":"Q::Q-001"}'
```

再提交反方向会得到 `409 Conflict`，响应中的 `state` 是未被修改的旧状态；重新查询也会看到补记和结论历史均未增加冲突项。

页面表单使用：

- `warehouse_uid`
- `qc_uid`
- `direction=w_before_q|q_before_w`

普通 HTML 提交地址为 `POST /cases/<case_id>/supplements`。

### 生成与下载核查凭据

```bash
# 生成（同一 SQLite 快照事务，返回凭据编号和校验摘要）
curl -sS -X POST "http://localhost:5000/api/cases/$CID/credentials"
# {
#   "id": "CR-20261125-104EDD70",
#   "case_id": "...",
#   "conclusion_id": 1,
#   "package_sha256": "4e6764...",
#   "manifest_sha256": "3c16ed...",
#   "created_at": "2026-11-25T00:08:38+00:00"
# }

# 按凭据编号下载当时冻结的原始快照（与页面生成时间无关，始终返回旧包）
curl -OJ "http://localhost:5000/api/credentials/CR-20261125-104EDD70/download"
```

ZIP 内固定包含五个条目（顺序固定、时间戳固定，可重复构建出相同字节）：

| 条目 | 内容 |
|---|---|
| `warehouse.csv` | 快照时的原始库房 CSV（UTF-8） |
| `qc.csv` | 快照时的原始质检 CSV（UTF-8） |
| `supplements.json` | 快照时已接受的全部补记及来源文件名 |
| `conclusion.json` | 快照时的结论版本（含结论编号、判定与见证顺序） |
| `MANIFEST.sha256` | 前四个文件按文件名字段排序的 SHA-256 清单 |

可直接用系统工具复核：

```bash
unzip -p CR-20261125-104EDD70.zip MANIFEST.sha256 \
  | (cd "$(mktemp -d)" && unzip -q - && sha256sum -c MANIFEST.sha256)
```

其他凭据接口：

- `GET /api/cases/<case_id>/credentials`：列出该案例全部凭据的元数据（不含 ZIP 本体）；
- `GET /api/credentials/<credential_id>`：按编号查询单个凭据的结论编号与校验摘要；
- 页面表单地址为 `POST /cases/<case_id>/credentials`，下载地址为
  `GET /cases/<case_id>/credentials/<credential_id>/download`。

生成失败（打包异常或数据库写入异常）返回 `500` 且事务整体回滚，`credentials` 表不会出现半包记录；案例不存在时返回 `404`。

## SQLite 表

- `cases`：箱号、原始 CSV 文本、当前结论指针；
- `records`：解析后的流水、表别、记录号、分钟键、行序、动作类型；
- `supplements`：已接受的跨表先后补记；
- `conclusions`：初次上传及每次成功补记后的结论 JSON 历史；
- `credentials`：不可变凭据，保存结论编号、整包与清单 SHA-256、清单文本和 ZIP 二进制（`bundle_blob`），仅追加、不更新不删除。

## 测试

```bash
python tests/test_algorithm_exhaustive.py   # 与全排列暴力判定随机对照
python tests/test_performance.py            # 320 条同分钟记录性能检查
python tests/test_app.py                    # Flask/SQLite/冲突退回测试（需安装 Flask）
python tests/test_evidence.py               # 凭据打包、清单排序、防篡改与可重复构建
python tests/test_credentials.py            # 凭据生成/下载/不可变/失败回滚/并发快照（需安装 Flask）
```

## 目录

```text
app.py          Flask 路由、上传、持久化、凭据事务与页面/API
verifier.py     CSV 解析、偏序、可达性、三态判定和见证顺序
evidence.py     凭据 ZIP 打包、固定排序 SHA-256 清单与校验
database.py     SQLite schema/连接辅助
templates/      服务端渲染页面
static/style.css
samples/        一组可直接上传的歧义样例
tests/          算法、性能、接口与凭据测试
Dockerfile
compose.yaml
docker-compose.yml
```
