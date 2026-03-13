from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="x-server-utils",  # PyPI 上的包名
    version="0.2.2",
    author="Xuan",
    author_email="786625468@qq.com",
    description="A collection of FastAPI Server Utilities and Stress Tester",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/nodame2233/x-server-utils",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "fastapi",
        "uvicorn",
        "requests>=2.32.4",
        "loguru",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",  # 或你选择的许可证
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.11",
)