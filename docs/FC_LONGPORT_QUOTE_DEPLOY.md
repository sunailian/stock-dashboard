# FC 官方实时行情部署

股票行情现在按以下顺序获取：

1. Longbridge Python SDK 实时报价，包括美股盘前、正常交易、盘后和隔夜时段；
2. SDK 缺失、连接失败或单个标的无报价时，仅对缺失标的降级到新浪/腾讯；
3. 页面用 `LIVE` 表示全部来自 Longbridge，用 `LIVE*` 表示存在降级行情。

## 为什么不能只上传 app.py

Longbridge 行情使用 WebSocket/Protobuf，由官方 SDK 处理。账户、成本和订单使用的是 HTTP API，所以此前单独上传 `app.py` 可以运行，但不会拥有官方行情能力。

Longport 4.x Linux wheel 当前要求 glibc 2.39。为兼容常规阿里云 FC Python 运行时，部署包固定使用官方 `longport==3.0.18` 的 manylinux2014 wheel。

## 构建

先在 FC 控制台确认 Python 版本和处理器架构，然后运行：

```bash
python3 build_fc_bundle.py --python-version 3.12 --architecture x86_64
```

生成文件：`dist/stock-dashboard-fc.zip`。

如果 FC 使用 arm64：

```bash
python3 build_fc_bundle.py --python-version 3.12 --architecture aarch64
```

将 ZIP 作为函数代码包上传，不能再只上传其中的 `app.py`。原有 Longbridge 和登录环境变量保持不变。

中国内地 FC 建议设置 `LONGBRIDGE_REGION=cn`，让 SDK 优先使用 `https://openapi.longbridge.cn`；美国区账号会根据 `us_` 前缀自动使用 `.com` 域名。

## 验证

部署后检查：

- `/health` 中 `quote_sdk_available` 应为 `true`；
- `/account?force=1` 中 `price_source_status` 应为 `live`；
- `price_details` 应显示每个标的的 `session`、时间和 `longbridge_quote_sdk` 来源；
- 页面右上角应显示 `LIVE`。如果是 `LIVE*`，查看 `optional_source_errors.longbridge_quote_sdk` 判断是 SDK、权限还是连接问题。
