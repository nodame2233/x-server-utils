# OSRA分子解析工具
OSRA（辅助分子解析的工具），根据图片解析，获取分子的mol参考骨架。

## 🛠️功能说明
核心功能：基于输入的单图片base64数据，解析返回分子的mol数据。

## ⬇️程序拉取
1. 前置条件：安装Python 3.11
2. 拉取分支：feature-520
```Bash
git clone -b feature-520 http://10.1.20.30/catagroup/backend/domain/domain-osra.git
```

## 🚀 程序启动
1. 根据 requirements.txt 安装依赖
2. 配置启动参数 --port 2089 --workers 8
3. 运行 osra_server.py
```Bash
pip install -r requirements.txt
python osra_server.py --p 2089 --w 8
```

## 压测
运行 stress_test.py
```Bash
python stress_test.py -u http://localhost:2089/tool -w 8 -n 100
```

## 🏗️程序部署
请阅读[后端开发规范](https://dptechnology.feishu.cn/wiki/WMvLwRSNSiMqZDkjqjtcru1xnWg)中部署流程相关内容
