---
title: 汇率监控
emoji: 💱
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
short_description: 中国银行实时外汇牌价监控（加元/美元/英镑等 10 种）
---

# 外币 → CNY 汇率监控

数据来源：[中国银行外汇牌价](https://www.boc.cn/sourcedb/whpj/) 实时 + [Frankfurter (ECB)](https://www.frankfurter.app/) 一年历史回填。

支持：加元 / 美元 / 英镑 / 欧元 / 日元 / 韩元 / 港币 / 新台币 / 澳元 / 卢布。

## 本地运行

```bash
pip install -r requirements.txt
python web.py     # 网页版 http://localhost:5000
python money_monitor.py   # 桌面 tkinter 版
```
