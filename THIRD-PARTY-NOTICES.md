# 第三方依赖与许可

本仓库的 `LICENSE` 和商业授权说明仅适用于版权所有者有权授权的小A原创代码，不改变第三方组件各自的许可证。第三方代码不会因本仓库公开而被重新许可为 PolyForm Noncommercial。

## PDF 处理组件

- `pypdf==6.19.0`：BSD-3-Clause，见 https://github.com/py-pdf/pypdf 。
- `pymupdf==1.28.2`：GNU AGPL v3 或 Artifex 商业许可，见 https://github.com/pymupdf/PyMuPDF 和 https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright 。

小A保留 PyMuPDF 提供的 PDF 页面渲染、图片提取等必要功能。本仓库没有附带 PyMuPDF 的二进制文件，也不授予其商业许可证。

AGPL 对适用的组合作品有对应源码及许可要求，不能直接用小A的禁止商业用途条款替代。在以当前非商业许可分发含 PyMuPDF 的完整程序、发布 EXE 或提供涉及该组件的网络服务前，应先核实并满足第三方许可要求；若依靠商业许可解决兼容问题，须另行取得覆盖实际用途的 Artifex 授权。本次源码公开不代表已取得该授权。

## 其他依赖

Python 依赖列于 `backend/requirements*.txt`，Electron/Node 依赖列于 `frontend/package.json` 和 `frontend/package-lock.json`。使用和再分发时须保留各组件要求的版权、许可和声明。小A的单独商业许可不能代替第三方许可。
