from setuptools import setup, find_packages

# 读取 README.md 作为长描述，展示在 PyPI 或 GitHub 上
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="x-server-utils",
    version="0.1.0",
    author="Xuan",
    author_email="786625468@qq.com",
    description="A collection of FastAPI Server Utilities and Stress Tester",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/nodame2233/x-server-utils",  # 你的 GitHub 仓库地址
    packages=find_packages(),
    include_package_data=True,         # 允许引入 MANIFEST.in 中的非代码文件
    install_requires=[                 # 你的代码所需的第三方依赖
        "fastapi",
        "uvicorn",
        "requests",
        "loguru",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "No license",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.11",
)
