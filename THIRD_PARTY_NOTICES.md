# Hydro-CPS-Sim V1.0 第三方依赖说明

本文件列出外部组件的固定来源、许可证信息及待确认事项。项目级许可证和代码权属由权利人确认；第三方源码、原生库及生成程序单独管理。

| 外部组件 | install.sh 固定来源 / 提交 | 许可证与来源情况 |
| --- | --- | --- |
| DHALSIM-epynet | https://github.com/afmurillo/DHALSIM-epynet / `4cb24f3735f6f79d95b52a7cf71a8fcaaa5ed172` | 根 LICENSE 为 MIT 文本，含 Davide Salaorni 2021 与 Abel Heinsbroek 2004 声明；setup.py 标注 Apache Licence 2.0，两处声明待上游确认 |
| OpenPLC_v3 | https://github.com/WandeF/OpenPLC_v3 / `64aa73e79692ec4a7ffd929e392b65e47eff6df8` | 固定提交的根 LICENSE 为 GPL v3 文本；内含组件按各自许可证核对 |
| ns-3-dev-git | https://github.com/WandeF/ns-3-dev-git / `76771a9b5a04d02be0612ff7aac0a212ee20aae9` | 固定提交的根 LICENSE 为 GPL v2 文本，另有 LICENSES 目录 |
| EPANET 原生库 | DHALSIM-epynet 的 `epynet/lib/` | 包含多个平台二进制，各文件的源码版本、构建来源和许可证待核实 |

DHALSIM-epynet 的包元数据版本为 1.1。EPANET 原生库的版本需根据对应源码和构建记录确认。

Python 直接依赖及精确版本见 `requirements.txt`。本机已安装包的许可证元数据保存在本地申请材料中，正式分发前需核对各包完整许可证。OpenPLC 内含 matiec、libmodbus、DNP3、Snap7 等组件，还需分别确认许可证、修改内容和分发声明要求。

分发时应保留上游 LICENSE/COPYING、版权头及必要声明。第三方原始实现和修改版本应在来源清单中独立列出，项目源码范围见本地申请材料的 `module_inventory.md`。
