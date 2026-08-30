"""本地启动入口：python run.py

会自动把 app/ 加入路径，加载 main:app，用 uvicorn 起服务。
配置通过环境变量注入；也可把配置写进同目录的 config.env（会被自动读取）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # 演示模式默认开，方便零配置先看效果；填好 key 后改成 false
    os.environ.setdefault("DEMO_MODE", "true")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
